"""Composer choices affect the real run and resume; statistics remain read-only."""
import copy
import json

from httpx import ASGITransport, AsyncClient

from tavern.application.api import create_app
from tavern.storage.maintenance import cleanup
from tests.backend.test_card_author import ToolLLM


async def test_model_effort_and_context_stats_follow_execution_and_resume(tmp_path, monkeypatch):
    app = create_app(tmp_path)
    store, editor = app.state.store, app.state.editor
    settings = store.settings_document()
    settings.shared.card_author.model = 'configured-model'
    settings.shared.card_author.reasoning_effort = 'low'
    settings.shared.card_author.context_tokens = 131072
    store.save_settings(settings)
    configured = store.settings_document().model_dump()
    connections, executions = [], []

    class Interrupted(ToolLLM):
        @property
        def last_usage(self):
            return {'input_tokens': 1200, 'input_tokens_details': {'cached_tokens': 900}}

        async def chat(self, messages, **kwargs):
            if self.call_count:
                raise ValueError('fixture interruption after tool result')
            async for chunk in super().chat(messages, **kwargs):
                yield chunk

    provider = Interrupted([('overview', {})])

    def check_connection(cfg):
        connections.append((cfg.model, cfg.reasoning_effort))

    async def make(cfg, callback):
        executions.append((cfg.model, cfg.reasoning_effort))
        return provider

    monkeypatch.setattr('tavern.character.card_author.require_model_connection', check_connection)
    monkeypatch.setattr('tavern.character.card_author.make_provider', make)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://testserver') as client:
            created = (await client.post('/api/card-author/conversations', json={})).json()
            wid = created['id']
            base = '/api/card-author/conversations/' + wid
            options = {'model': 'selected-model', 'reasoning_effort': 'high'}
            stats = (await client.post(base + '/stats', json={**options, 'text': '查看资料'})).json()
            preview = (await client.post(base + '/preview', json={**options, 'text': '查看资料'})).json()
            assert stats['estimated_tokens'] == preview['estimated_tokens'] > 0
            assert stats['context_limit'] == 131072 and stats['model'] == 'selected-model'
            assert stats['usage'] is None and stats['token_counting']['estimated']
            assert stats['cache_usage'] is None
            assert editor.edits.messages(wid) == []
            run = (await client.post(base + '/runs', json={**options, 'text': '查看资料', 'expected_revision': 0, 'operation_id': 'initial'})).json()
            if task := editor.tasks.get(run['id']):
                await task
            assert editor.edits.run(run['id'])['status'] == 'error'
            assert connections == executions == [('selected-model', 'high')]
            progress = copy.deepcopy(editor.edits.progress(wid))
            next_options = {'model': 'replacement-model', 'reasoning_effort': 'medium'}
            stats = (await client.post(base + '/stats', json=next_options)).json()
            assert stats['usage'] == provider.last_usage
            assert stats['cache_usage'] == {'prompt_tokens': 1200, 'cached_tokens': 900}
            assert stats['model'] == 'replacement-model'
            assert editor.edits.progress(wid) == progress
            call_stats = [event['payload'] for event in editor.edits.events(run['id']) if event['kind'] == 'context_stats']
            assert len(call_stats) == 3 and call_stats[1]['usage'] == provider.last_usage
            assert call_stats[-1]['estimated_tokens'] > call_stats[0]['estimated_tokens']
            # The SSE snapshot is absolute, so reconnect/replay cannot add usage twice.
            stream = (await client.get(f"/api/card-edit-runs/{run['id']}/events")).text
            snapshots = [json.loads(line[6:])['payload'] for line in stream.splitlines()
                         if line.startswith('data: ') and json.loads(line[6:]).get('kind') == 'context_stats']
            assert len(snapshots) == 3 and all(s['cache_usage'] == stats['cache_usage'] for s in snapshots)
            class ReportedUsage(ToolLLM):
                @property
                def usage_history(self):
                    return [{'prompt_tokens': 800, 'cached_tokens': 100}]

            provider = ReportedUsage([])
            resumed = (await client.post(base + '/runs', json={**next_options, 'resume_run_id': run['id'], 'expected_revision': 0, 'operation_id': 'resume'})).json()
            if task := editor.tasks.get(resumed['id']):
                await task
            assert editor.edits.run(resumed['id'])['status'] == 'completed'
            stats = (await client.post(base + '/stats', json=next_options)).json()
            total = {'prompt_tokens': 2000, 'cached_tokens': 1000}
            assert stats['cache_usage'] == total  # completed must not repeat the last 800/100.
            with store.connection() as db:
                db.execute('UPDATE card_edit_runs SET created=0 WHERE workspace=?', (wid,))
            cleanup(store)
            assert (await client.post(base + '/stats', json=next_options)).json()['cache_usage'] == total
            class MissingUsage(ToolLLM):
                @property
                def usage_history(self):
                    return [{'prompt_tokens': 1200, 'cached_tokens': None}]

                @property
                def last_usage(self):
                    return {'prompt_tokens': 1200, 'cached_tokens': 0}

            provider = MissingUsage([])
            missing = (await client.post(base + '/runs', json={**next_options, 'text': '继续', 'expected_revision': 0, 'operation_id': 'missing'})).json()
            if task := editor.tasks.get(missing['id']):
                await task
            assert editor.edits.run(missing['id'])['status'] == 'completed'
            assert executions[-1] == ('replacement-model', 'medium')
            stats = (await client.post(base + '/stats', json=next_options)).json()
            assert stats['usage']['cached_tokens'] is None  # Neither recycled hits nor accounting zeros.
            assert stats['cache_usage'] == {'prompt_tokens': 3200, 'cached_tokens': None}
            assert store.settings_document().model_dump() == configured
            inherited = (await client.post(base + '/stats', json={})).json()
            assert inherited['model'] == 'configured-model'
            assert (await client.post(base + '/stats', json={'model': '  '})).status_code == 422
            assert (await client.post(base + '/stats', json={'reasoning_effort': 'invalid'})).status_code == 422
            # A historical completed event alone cannot reconstruct all its calls.
            with store.connection() as db:
                db.execute("DELETE FROM card_edit_events WHERE run=? AND kind='context_stats'", (resumed['id'],))
            assert (await client.post(base + '/stats', json=next_options)).json()['cache_usage'] == {
                'prompt_tokens': None, 'cached_tokens': None}
    finally:
        await editor.close()
        await app.state.service.close()
