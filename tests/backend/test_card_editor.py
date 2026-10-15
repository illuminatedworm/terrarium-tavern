import asyncio
import base64
import copy
import io
import json
import zipfile

import pytest
from httpx import ASGITransport, AsyncClient
from kohakuterrarium.llm.base import NativeToolCall
from kohakuterrarium.testing.llm import ScriptedLLM
from PIL import Image, PngImagePlugin

from tavern.application.service import RoleplayRuntime
from tavern.character.card_editor import CardEditorService
from tavern.character.card_editor_store import EditorStore
from tavern.character.card_workflow import validate_document, validation_report
from tavern.character.cards import export_card, parse_card
from tavern.conversation.context import compile_context
from tavern.core.models import CardEditorRequest, SessionCreate, Settings
from tavern.scripting.variables import analyze_variables
from tavern.storage.store import Conflict, Store


def fixture():
    return {"name": "测试卡", "description": "原人物设定", "first_mes": "开场一", "alternate_greetings": ["开场二"],
            "extensions": {"unknown": {"keep": True}}, "character_book": {"entries": [
                {"id": "story", "comment": "人物", "content": "普通世界设定", "constant": True, "enabled": True},
                {"id": "rules", "comment": "变量规则", "content": "变量秘密规则 {{user}}", "constant": True, "enabled": True,
                 "extensions": {"terrarium": {"audience": "tracker_mvu"}}},
                {"id": "init", "comment": "默认值", "content": "hp: 5", "enabled": True,
                 "extensions": {"terrarium": {"audience": "runtime_init"}}},
            ]}}


def import_fixture(store):
    return store.import_card(json.dumps(fixture()).encode(), "fixture.json")


def test_adaptation_validation_leaves_opening_mvu_out_of_scope():
    document = fixture()
    document["first_mes"] += (
        "<UpdateVariable><initvar>hp: 8</initvar></UpdateVariable>"
        "<StatusPlaceHolderImpl/>"
    )
    report = validation_report(document)
    assert report["valid"]
    assert report["adapted"]
    assert report["warnings"] == []


class ToolLLM(ScriptedLLM):
    def __init__(self, steps):
        super().__init__([""])
        self.steps, self.native, self.schemas = steps, [], []

    @property
    def last_tool_calls(self):
        return self.native

    async def chat(self, messages, **kwargs):
        self.call_log.append(messages)
        self.schemas.append(kwargs.get("tools", []))
        index = self.call_count
        self.call_count += 1
        if index < len(self.steps):
            name, arguments = self.steps[index]
            self.native = [NativeToolCall(id=f"call-{index}", name=name, arguments=json.dumps(arguments))]
            yield ""
        else:
            self.native = []
            yield "草稿修改完成，请审阅。"


@pytest.mark.parametrize("container", ["json", "png", "charx"])
def test_copy_export_resources_and_session_isolation(tmp_path, container):
    store = Store(tmp_path)
    raw = json.dumps(fixture()).encode()
    if container == "png":
        info = PngImagePlugin.PngInfo()
        info.add_text("chara", base64.b64encode(raw).decode())
        buf = io.BytesIO()
        Image.new("RGB", (2, 2)).save(buf, "PNG", pnginfo=info)
        raw = buf.getvalue()
    elif container == "charx":
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as archive:
            archive.writestr("card.json", raw)
            archive.writestr("assets/a.txt", "preserved")
        raw = buf.getvalue()
    card = store.import_card(raw, f"fixture.{container}")
    session = store.new_session(SessionCreate(card_id=card["id"]))
    copied = store.copy_card(card["id"])
    copied2 = store.copy_card(card["id"])
    assert copied["id"] != card["id"]
    assert copied2["name"].endswith(" 2")
    document = copy.deepcopy(card["document"])
    document["description"] = "新设定"
    store.edit_card(card["id"], document, 0)
    assert store.session_card(session["id"])["document"]["description"] == "原人物设定"
    with pytest.raises(Conflict):
        store.edit_card(card["id"], document, 0)
    store.delete_card(card["id"])
    assert store.session_card(session["id"])["document"]["description"] == "原人物设定"
    original = (store.root / "blobs" / copied["original"]).read_bytes()
    resources = {k: (store.root / "blobs" / v).read_bytes() for k, v in copied["resources"].items()}
    exported, _, ext = export_card(copied["document"], container, original, resources)
    parsed = parse_card(exported, f"copy.{ext}")
    assert parsed[0] == copied["document"]
    assert parsed[0]["extensions"]["unknown"] == {"keep": True}


async def test_editor_tools_save_undo_and_recovery(tmp_path):
    store = Store(tmp_path)
    card = import_fixture(store)
    service = CardEditorService(store)
    workspace = service.edits.create(card["id"])
    provider = ToolLLM([
        ("read_skill", {}),
        ("patch_draft", {"operations": [{"op": "replace", "path": "/description", "value": "新的角色描述"}], "comment": "修改描述"}),
        ("set_opening", {"index": 0, "narrative": "干净开场\n<initvar>hp: 8</initvar>"}),
        ("validate_draft", {}),
    ])
    service.provider_factory = lambda: provider
    req = CardEditorRequest(text="适配并修改描述", operation_id="one", expected_revision=0, adapt=True)
    try:
        run = service.submit(workspace["id"], req)
        assert service.submit(workspace["id"], req)["id"] == run["id"]
        task = service.tasks[run["id"]]
        await task
        final = service.edits.run(run["id"])
        assert final["status"] == "completed", final["error"]
        assert store.card(card["id"])["document"]["description"] == "原人物设定"
        current = service.edits.get(workspace["id"])
        assert current["document"]["description"] == "新的角色描述"
        assert current["document"]["first_mes"] == "干净开场\n<initvar>hp: 8</initvar>"
        events = service.edits.activity(run["id"])["events"]
        starts = [e for e in events if e["kind"] == "tool_started"]
        ends = [e for e in events if e["kind"] == "tool_finished"]
        assert [e["payload"]["tool"] for e in starts] == ['read_skill', 'patch_draft', 'set_opening', 'validate_draft']
        assert {e["payload"]["action_id"] for e in starts} == {e["payload"]["action_id"] for e in ends}
        assert all(e["payload"]["status"] == 'completed' for e in ends)
        assert all(len(e["payload"]["result"]) <= 4000 for e in ends)
        assert "terrarium" not in current["document"]["extensions"]
        names = {tool.name for batch in provider.schemas for tool in (batch or [])}
        assert names == {"overview", "read_field", "search_entries", "patch_draft", "set_text_field", "set_opening", "validate_draft", "read_skill",
                         "create_draft", "configure_card", "revert_draft", "test_draft", "write_handoff", "new_context", "search_history", "read_history",
                             "list_personas", "read_persona", "create_persona", "update_persona", "web_search", "read_webpage"}
        saved = service.edits.save(current["id"], current["revision"], "copy", "copy-once")
        assert service.edits.save(current["id"], current["revision"], "copy", "copy-once")["id"] == saved["id"]
        current = service.edits.get(current["id"])
        doc = copy.deepcopy(current["document"])
        doc["description"] = "手工草稿"
        current = service.edits.update(current["id"], doc, current["revision"])
        current = service.edits.undo(current["id"], current["revision"])
        assert current["document"]["description"] == "新的角色描述"
        assert EditorStore(Store(tmp_path)).get(current["id"])["document"] == current["document"]
    finally:
        await service.close()


async def test_mvu_routing_and_opening_initialization(tmp_path):
    store = Store(tmp_path)
    doc = fixture()
    doc["first_mes"] += "\n<initvar>hp: 8</initvar>"
    doc["alternate_greetings"] = ["开场二\n<initvar>hp: 10</initvar>"]
    validate_document(doc)
    card = store.import_card(json.dumps(doc).encode(), "fixture.json")
    session = store.new_session(SessionCreate(card_id=card["id"], greeting=1))
    service = RoleplayRuntime(store)
    try:
        session = await service.initialize_session(session["id"])
        assert session["snapshot"]["state"]["stat_data"]["hp"] == 10
        assert session["messages"][0]["raw"] == "开场二\n<initvar>hp: 10</initvar>"
        reopened = await service.initialize_session(session["id"])
        assert reopened["snapshot"]["state"]["stat_data"]["hp"] == 10
        snapshot = copy.deepcopy(session["snapshot"])
        snapshot["lore_injections"] = [{"id": "rules", "anchor_id": session["head"], "content": "变量秘密规则", "source_ids": []}]
        compiled = compile_context(doc, session, session["messages"], snapshot, "继续", store.settings(), "test")
        wire = json.dumps(compiled["messages"], ensure_ascii=False)
        assert "变量秘密规则" not in wire
        assert "普通世界设定" in wire
        provider = ScriptedLLM(['[]'])
        events = []
        class Capture:
            def event(self, *args):
                events.append(args)
        await analyze_variables(Capture(), "test", Settings(), [], snapshot["state"], "继续", "剧情",
                                lambda: provider, document=doc, persona="测试玩家")
        request = json.dumps(provider.last_messages, ensure_ascii=False)
        assert "变量秘密规则 测试玩家" in request
        assert "普通世界设定" not in request
        assert "hp: 5" not in request
        assert events[0][1] == "variable_rules"
    finally:
        await service.close()


async def test_cancel_rolls_back_partial_tools(tmp_path):
    store = Store(tmp_path)
    card = import_fixture(store)
    service = CardEditorService(store)
    ws = service.edits.create(card["id"])
    started = asyncio.Event()
    class Waiting(ToolLLM):
        async def chat(self, messages, **kwargs):
            if self.call_count:
                started.set()
                await asyncio.Event().wait()
            async for chunk in super().chat(messages, **kwargs):
                yield chunk
    service.provider_factory = lambda: Waiting([("patch_draft", {
        "operations": [{"op": "replace", "path": "/description", "value": "未完成"}], "comment": "临时",
    })])
    try:
        run = service.submit(ws["id"], CardEditorRequest(text="修改", operation_id="cancel", expected_revision=0))
        await asyncio.wait_for(started.wait(), 10)
        await service.cancel(run["id"])
        assert service.edits.get(ws["id"])["document"] == card["document"]
        assert service.edits.run(run["id"])["status"] == "cancelled"
        assert service.edits.context(ws["id"]) is None
    finally:
        await service.close()


async def test_editor_api_conflicts_reload_and_session_lore(tmp_path):
    from tavern.application.api import create_app
    app = create_app(tmp_path)
    store = app.state.store
    card = import_fixture(store)
    session = store.new_session(SessionCreate(card_id=card['id']))
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://testserver') as client:
            response = await client.post(f"/api/cards/{card['id']}/edits", json={'expected_revision': 0})
            assert response.status_code == 200
            ws = response.json()
            draft = copy.deepcopy(ws['document'])
            draft['description'] = '草稿描述'
            ws = (await client.put(f"/api/card-edits/{ws['id']}", json={
                'document': draft, 'expected_revision': ws['revision']})).json()
            changed = copy.deepcopy(card['document'])
            changed['description'] = '外部修改'
            store.edit_card(card['id'], changed, 0)
            result = await client.post(f"/api/card-edits/{ws['id']}/save", json={
                'expected_revision': ws['revision'], 'operation_id': 'conflict', 'mode': 'overwrite'})
            assert result.status_code == 409
            assert app.state.editor.edits.get(ws['id'])['document'] == draft
            result = await client.post(f"/api/card-edits/{ws['id']}/reload", json={'expected_revision': ws['revision']})
            assert result.status_code == 200
            ws = result.json()
            assert ws['document'] == changed
            result = await client.post(f"/api/card-edits/{ws['id']}/undo", json={'expected_revision': ws['revision']})
            assert result.status_code == 200
            ws = result.json()
            assert ws['document'] == draft
            result = await client.post(f"/api/card-edits/{ws['id']}/save", json={
                'expected_revision': ws['revision'], 'operation_id': 'save', 'mode': 'overwrite'})
            # Undoing reload also restores the old base revision, so it must
            # not silently grant permission to overwrite the externally edited card.
            assert result.status_code == 409
            ws = (await client.post(f"/api/card-edits/{ws['id']}/reload", json={'expected_revision': ws['revision']})).json()
            ws = (await client.put(f"/api/card-edits/{ws['id']}", json={
                'document': draft, 'expected_revision': ws['revision']})).json()
            result = await client.post(f"/api/card-edits/{ws['id']}/save", json={
                'expected_revision': ws['revision'], 'operation_id': 'save-after-review', 'mode': 'overwrite'})
            assert result.status_code == 200, result.text
            assert store.session_card(session['id'])['document'] == card['document']
            changed = copy.deepcopy(card['document'])
            changed['character_book']['entries'][0]['content'] = '对话私有设定'
            result = await client.put(f"/api/sessions/{session['id']}/lorebook", json={
                'document': changed, 'expected_revision': 0, 'operation_id': 'lore'})
            assert result.status_code == 200, result.text
            assert result.json()['snapshot']['content']['document']['character_book'] == changed['character_book']
            assert store.card(card['id'])['document']['character_book'] == draft['character_book']
            ws = (await client.get(f"/api/card-edits/{ws['id']}")).json()
            assert (await client.request("DELETE", f"/api/card-edits/{ws['id']}", json={'expected_revision': ws['revision']})).status_code == 200
    finally:
        await app.state.editor.close()
        await app.state.service.close()


def test_edit_draft_backup_recovery_and_resource_protection(tmp_path):
    from tavern.storage.backup import backup, restore
    from tavern.storage.maintenance import cleanup
    store = Store(tmp_path / 'source')
    card = import_fixture(store)
    edits = EditorStore(store)
    ws = edits.create(card['id'])
    draft = copy.deepcopy(ws['document'])
    draft['description'] = '未保存草稿'
    ws = edits.update(ws['id'], draft, 0)
    store.delete_card(card['id'])
    assert (store.root / 'blobs' / card['original']).exists()
    cleanup(store)
    backup(store, tmp_path / 'backup.zip')
    restored = restore(tmp_path / 'backup.zip', tmp_path / 'restored')
    edits = EditorStore(restored)
    assert edits.get(ws['id'])['document'] == draft
    saved = edits.save(ws['id'], ws['revision'], 'copy', 'saved')
    assert saved['document']['description'] == '未保存草稿'
    assert (restored.root / 'blobs' / saved['original']).exists()
    with restored.connection() as db:
        db.execute("INSERT INTO card_edit_runs VALUES ('interrupted',?,'restart','{}','running','','',0)", (ws['id'],))
    restored.recover()
    assert edits.run('interrupted')['status'] == 'interrupted'
    assert edits.get(ws['id'])['document'] == saved['document']


async def test_editor_model_configuration_and_secret_redaction(tmp_path):
    from tavern.application.api import create_app
    app = create_app(tmp_path)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://testserver') as client:
            payload = {'providers': [{'id': 'default', 'api_key': 'private-key'}],
                       'card_editor': {'provider_id': 'default', 'model': 'editor', 'reply_tokens': 4096}}
            result = await client.patch('/api/settings/shared', json={'expected_revision':0, 'changes':payload})
            assert result.status_code == 200, result.text
            assert 'private-key' not in result.text
            assert app.state.store.settings().for_role('card_editor').model == 'editor'
            result = await client.patch('/api/settings/shared', json={'expected_revision':1, 'changes':{'card_editor':None}})
            assert result.status_code == 400
            assert app.state.store.settings().for_role('card_editor').model == 'editor'

    finally:
        await app.state.service.close()
        await app.state.editor.close()


async def test_twelve_call_limit_and_full_event_replay(tmp_path):
    from tavern.application.api import create_app
    app = create_app(tmp_path)
    card = import_fixture(app.state.store)
    editor = app.state.editor
    ws = editor.edits.create(card['id'])
    provider = ToolLLM([('overview', {})] * 11)
    editor.provider_factory = lambda: provider
    try:
        run = editor.submit(ws['id'], CardEditorRequest(text='检查资料', expected_revision=0, operation_id='limit'))
        await editor.tasks[run['id']]
        final = editor.edits.run(run['id'])
        assert final['status'] == 'completed', final['error']
        assert provider.call_count == 12
        assert provider.schemas[-1]
        for previous, current in zip(provider.call_log, provider.call_log[1:]):
            assert current[:len(previous)] == previous
        assert not any("本轮最后一次调用" in str(messages)
                       or "tavern_last_call_notice" in str(messages) for messages in provider.call_log)
        for index in range(230):
            editor.edits.event(run['id'], 'diagnostic', {'index': index})
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://testserver') as client:
            result = await client.get(f"/api/card-edit-runs/{run['id']}/events")
            assert '"index": 229' in result.text
            ids = [int(line[4:]) for line in result.text.splitlines() if line.startswith('id: ')]
            assert ids == sorted(set(ids))
            assert 'event: end' in result.text
            resumed = await client.get(f"/api/card-edit-runs/{run['id']}/events", headers={'last-event-id': str(ids[-2])})
            assert f'id: {ids[-1]}' in resumed.text
            assert f'id: {ids[-2]}\n' not in resumed.text
    finally:
        await editor.close()
        await app.state.service.close()


async def test_failed_tool_action_is_recorded_without_a_draft_change(tmp_path):
    store = Store(tmp_path)
    card = import_fixture(store)
    editor = CardEditorService(store)
    ws = editor.edits.create(card['id'])
    editor.provider_factory = lambda: ToolLLM([('read_field', {'path': '/missing'})])
    try:
        run = editor.submit(ws['id'], CardEditorRequest(text='读取字段', expected_revision=0, operation_id='failed-tool'))
        await editor.tasks[run['id']]
        page = editor.edits.activity(run['id'])
        starts = [event for event in page['events'] if event['kind'] == 'tool_started']
        ends = [event for event in page['events'] if event['kind'] == 'tool_finished']
        assert len(starts) == len(ends) == 1
        assert starts[0]['payload']['action_id'] == ends[0]['payload']['action_id']
        assert ends[0]['payload']['status'] == 'error'
        assert 'missing' in ends[0]['payload']['detail']
        assert not any(event['kind'] in ('provider_request', 'provider_response') for event in page['events'])
        assert any(event['kind'] == 'text' for event in page['events'])
        assert editor.edits.get(ws['id'])['document'] == card['document']
    finally:
        await editor.close()


async def test_multiple_contexts_keep_drafts_and_model_history_independent(tmp_path):
    store = Store(tmp_path)
    card = import_fixture(store)
    editor = CardEditorService(store)
    first = editor.edits.create(card['id'])
    editor.provider_factory = lambda: ToolLLM([('patch_draft', {
        'operations': [{'op': 'replace', 'path': '/description', 'value': '只属于第一份草稿'}], 'comment': '修改',
    })])
    try:
        run = editor.submit(first['id'], CardEditorRequest(
            text='OLD_CONTEXT_MARKER_9381', expected_revision=0, operation_id='old'))
        await editor.tasks[run['id']]
        first = editor.edits.get(first['id'])
        carried = editor.edits.new_context(first['id'], first['revision'])
        fresh = editor.edits.create(card['id'])
        assert len({first['id'], carried['id'], fresh['id']}) == 3
        assert carried['document'] == first['document']
        assert fresh['document'] == card['document']
        assert carried['base_revision'] == first['base_revision']
        assert carried['runs'] == fresh['runs'] == []
        assert not carried['can_undo'] and not fresh['can_undo']
        provider = ScriptedLLM(['独立上下文的答复'])
        editor.provider_factory = lambda: provider
        for context in (carried, fresh):
            run = editor.submit(context['id'], CardEditorRequest(text='新上下文问题', expected_revision=0, operation_id='new'))
            await editor.tasks[run['id']]
            assert editor.edits.run(run['id'])['status'] == 'completed'
        assert 'OLD_CONTEXT_MARKER_9381' not in json.dumps(provider.call_log)
        summaries = editor.edits.list(card['id'])
        assert len(summaries) == 3
        assert next(row for row in summaries if row['id'] == first['id'])['title'] == 'OLD_CONTEXT_MARKER_9381'
        assert editor.edits.get(first['id'])['document']['description'] == '只属于第一份草稿'
        editor.edits.save(fresh['id'], 0, 'overwrite', 'save-new')
        with pytest.raises(Conflict):
            editor.edits.save(carried['id'], 0, 'overwrite', 'old-base')
    finally:
        await editor.close()


async def test_new_context_does_not_cancel_running_source(tmp_path):
    store = Store(tmp_path)
    card = import_fixture(store)
    editor = CardEditorService(store)
    source = editor.edits.create(card['id'])
    started, release = asyncio.Event(), asyncio.Event()
    class Waiting(ScriptedLLM):
        async def chat(self, messages, **kwargs):
            started.set()
            await release.wait()
            async for part in super().chat(messages, **kwargs):
                yield part
    editor.provider_factory = lambda: Waiting(['原对话继续完成'])
    try:
        run = editor.submit(source['id'], CardEditorRequest(text='继续工作', expected_revision=0, operation_id='running'))
        await asyncio.wait_for(started.wait(), 10)
        new = editor.edits.new_context(source['id'], 0)
        assert new['runs'] == []
        assert editor.edits.run(run['id'])['status'] == 'running'
        assert new['document'] == source['document']
        release.set()
        await editor.tasks[run['id']]
        assert editor.edits.run(run['id'])['status'] == 'completed'
        assert editor.edits.get(new['id'])['runs'] == []
    finally:
        await editor.close()


@pytest.mark.parametrize('save_first', [False, True])
def test_deleting_conversation_removes_its_draft_but_not_saved_card_or_other_context(tmp_path, save_first):
    store = Store(tmp_path)
    card = import_fixture(store)
    edits = EditorStore(store)
    current = edits.create(card['id'])
    other = edits.create(card['id'])
    document = copy.deepcopy(current['document'])
    document['description'] = '当前对话的修改'
    current = edits.update(current['id'], document, 0)
    if save_first:
        edits.save(current['id'], current['revision'], 'overwrite', 'preserve')
        current = edits.get(current['id'])
    with store.connection() as db:
        db.execute("INSERT INTO card_edit_runs VALUES ('deleted-run',?,'one','{}','completed','reply',NULL,0)", (current['id'],))
    edits.event('deleted-run', 'completed', {'calls': 1})
    edits.delete(current['id'], current['revision'])
    with pytest.raises(KeyError):
        edits.get(current['id'])
    assert store.card(card['id'])['document'] == (document if save_first else card['document'])
    assert edits.get(other['id'])['document'] == other['document']
    with store.connection() as db:
        for table in ('card_edit_runs', 'card_edit_versions', 'card_edit_saves'):
            assert db.execute(f'SELECT count(*) FROM {table} WHERE workspace=?', (current['id'],)).fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM card_edit_events WHERE run='deleted-run'").fetchone()[0] == 0


def test_delete_rejects_stale_and_running_context_and_preserves_shared_draft_assets(tmp_path):
    store = Store(tmp_path)
    card = import_fixture(store)
    edits = EditorStore(store)
    current = edits.create(card['id'])
    other = edits.new_context(current['id'], 0)
    with pytest.raises(Conflict):
        edits.delete(current['id'], 99)
    with store.connection() as db:
        db.execute("INSERT INTO card_edit_runs VALUES ('active-run',?,'active','{}','running','',NULL,0)", (current['id'],))
    with pytest.raises(Conflict):
        edits.delete(current['id'], 0)
    assert edits.get(current['id'])['document'] == current['document']
    with store.connection() as db:
        db.execute("UPDATE card_edit_runs SET status='cancelled' WHERE id='active-run'")
    store.delete_card(card['id'])
    edits.delete(current['id'], 0)
    assert (store.root / 'blobs' / card['original']).exists()
    edits.delete(other['id'], 0)
    assert not (store.root / 'blobs' / card['original']).exists()
