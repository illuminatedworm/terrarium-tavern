from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from kohakuterrarium.testing.llm import ScriptedLLM

from tavern.application.api import create_app
from tavern.core.models import SessionCreate, TurnRequest
from tavern.storage.store import Conflict, Store, dump


def test_deleted_card_disappears_from_shelf_but_existing_progress_survives(tmp_path):
    store = Store(tmp_path)
    card = store.import_card(Path('samples/雾港来信.json').read_bytes(), 'card.json')
    session = store.new_session(SessionCreate(card_id=card['id']))
    store.delete_card(card['id'])
    assert store.cards() == []
    assert store.session(session['id'])['messages'] == session['messages']
    with pytest.raises(KeyError):
        store.card(card['id'])
    assert store.session_card(session['id'])['document'] == card['document']
    with store.connection() as db:
        assert db.execute('SELECT count(*) FROM cards').fetchone()[0] == 0
    fork = store.fork_session(session['id'], session['head'], 0)
    assert fork['messages'][0]['raw'] == session['messages'][0]['raw']
    with pytest.raises(KeyError):
        store.new_session(SessionCreate(card_id=card['id']))
    assert Store(tmp_path).cards() == []
    assert len(store.sessions()) == 2


def test_deletion_prunes_unique_blobs_but_keeps_shared_imports(tmp_path):
    store = Store(tmp_path)
    data = Path('samples/雾港来信.json').read_bytes()
    first = store.import_card(data, 'one.json')
    second = store.import_card(data, 'two.json')
    unique = store.blob(b'unique asset')
    with store.connection() as db:
        db.execute('UPDATE cards SET resources=? WHERE id=?', (dump({'unique.png': unique}), first['id']))
    store.delete_card(first['id'])
    assert not (tmp_path / 'blobs' / unique).exists()
    assert (tmp_path / 'blobs' / second['original']).exists()
    store.delete_card(second['id'])
    assert not (tmp_path / 'blobs' / second['original']).exists()


def test_deleted_card_resources_survive_fork_and_are_removed_after_last_story(tmp_path):
    store = Store(tmp_path)
    card = store.import_card(Path('samples/雾港来信.json').read_bytes(), 'card.json')
    asset = store.blob(b'cached dependency')
    bundle = store.blob(b'compiled script')
    source_hash = 'a' * 64
    manifest = {'resources': {'https://cards.example/asset.js': {'hash': asset}},
                'scripts': {source_hash: bundle}}
    directory = tmp_path / 'dependencies'
    directory.mkdir(exist_ok=True)
    (directory / f"{card['id']}.json").write_text(dump(manifest), encoding='utf-8')
    (directory / f'{source_hash}.js').write_text('compiled', encoding='utf-8')
    session = store.new_session(SessionCreate(card_id=card['id']))
    store.delete_card(card['id'])
    assert not (directory / f"{card['id']}.json").exists()
    owned = store.session_card(session['id'])
    assert store.card_manifest(owned) == manifest
    newer = store.blob(b'new dependency after deletion')
    manifest['resources']['https://cards.example/new.js'] = {'hash': newer}
    store.card_manifest_path(owned).write_text(dump(manifest), encoding='utf-8')
    fork = store.fork_session(session['id'], session['head'], 0)
    store.delete_session(session['id'])
    fork_card = store.session_card(fork['id'])
    assert fork_card['session_id'] == fork['id']
    assert store.card_manifest(fork_card) == manifest
    assert all((tmp_path / 'blobs' / digest).exists() for digest in (asset, bundle, newer, card['original']))
    store.delete_session(fork['id'])
    assert all(not (tmp_path / 'blobs' / digest).exists() for digest in (asset, bundle, newer, card['original']))
    assert not (directory / f'{source_hash}.js').exists()


@pytest.mark.parametrize('status', ['queued', 'running', 'awaiting_review'])
def test_delete_card_rejects_active_story_without_partial_changes(tmp_path, status):
    store = Store(tmp_path)
    card = store.import_card(Path('samples/雾港来信.json').read_bytes(), 'card.json')
    session = store.new_session(SessionCreate(card_id=card['id']))
    tid, _ = store.create_turn(session['id'], TurnRequest(operation_id='active', expected_revision=0))
    store.set_turn(tid, status=status)
    with pytest.raises(Conflict):
        store.delete_card(card['id'])
    assert store.card(card['id']) == card
    assert store.session(session['id'])['card_data'] == session['card_data']


async def test_card_delete_endpoint_and_reimport(tmp_path):
    app = create_app(tmp_path)
    store = app.state.store
    card = store.import_card(Path('samples/雾港来信.json').read_bytes(), 'card.json')
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://testserver') as client:
        deleted = await client.delete(f"/api/cards/{card['id']}")
        assert deleted.status_code == 200
        assert (await client.get('/api/cards')).json() == []
        assert (await client.get(f"/api/cards/{card['id']}")).status_code == 404
        assert (await client.delete(f"/api/cards/{card['id']}")).status_code == 404
    added = store.import_card(Path('samples/雾港来信.json').read_bytes(), 'new.json')
    assert [entry['id'] for entry in store.cards()] == [added['id']]


async def test_story_can_preview_and_continue_after_card_is_physically_deleted(tmp_path):
    app = create_app(tmp_path)
    store, service = app.state.store, app.state.service
    card = store.import_card(Path('samples/雾港来信.json').read_bytes(), 'card.json')
    session = store.new_session(SessionCreate(card_id=card['id']))
    service.provider_factory = lambda: ScriptedLLM(['故事继续。'])
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://testserver') as client:
            assert (await client.delete(f"/api/cards/{card['id']}")).status_code == 200
            assert (await client.get(f"/api/cards/{card['id']}")).status_code == 404
            owned = (await client.get(f"/api/sessions/{session['id']}/card")).json()
            assert owned['document'] == card['document'] and 'archived' not in owned
            assert (await client.post(f"/api/sessions/{session['id']}/preview", json={'text': '继续'})).status_code == 200
            response = await client.post(f"/api/sessions/{session['id']}/turns", json={
                'text': '继续', 'operation_id': 'continue', 'expected_revision': 0,
            })
            assert response.status_code == 202
            tid = response.json()['id']
            await service.tasks[tid]
            assert store.turn(tid)['status'] == 'committed', store.turn(tid)['error']
            assert store.session(session['id'])['messages'][-1]['raw'] == '故事继续。'
    finally:
        await service.close()
