import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

from tavern.application.api import create_app
from tavern.core.models import PersonaEdit, SessionCreate, Settings
from tavern.search.local_models import LocalModelManager, _download
from tavern.storage.store import Conflict, Store


async def test_model_listing_uses_saved_key_and_proxy(tmp_path, monkeypatch):
    app = create_app(tmp_path)
    settings = app.state.store.settings()
    settings.providers[0].api_key = 'secret'
    app.state.store.save_settings(settings)
    real_client = httpx.AsyncClient
    calls = []

    def client(**kwargs):
        calls.append(kwargs)

        def respond(request):
            assert str(request.url) == 'https://api.openai.com/v1/models'
            assert request.headers['authorization'] == 'Bearer secret'
            return httpx.Response(200, json={'data': [{'id': 'b'}, {'id': 'a'}, {'id': 'b'}, {}]})

        return real_client(transport=httpx.MockTransport(respond))

    monkeypatch.setattr(httpx, 'AsyncClient', client)
    async with real_client(transport=httpx.ASGITransport(app=app), base_url='http://testserver') as api:
        response = await api.post('/api/settings/models', json={'provider_id': 'default'})
        assert response.status_code == 200
        assert response.json()['models'] == ['a', 'b']
        assert response.json()['updated_at'] is not None
    assert calls[0]['proxy'] is None
    assert calls[0]['trust_env'] is False


def test_personas_only_keep_latest_and_delete_physically(tmp_path):
    store = Store(tmp_path)
    persona = store.save_persona(None, PersonaEdit(name='玩家', description='旧内容'))
    persona = store.save_persona(persona['id'], PersonaEdit(name='玩家', description='最新内容', expected_version=1))
    with pytest.raises(Conflict):
        store.save_persona(persona['id'], PersonaEdit(name='覆盖', description='', expected_version=1))
    assert store.personas() == [persona]
    card = store.import_card(Path('samples/雾港来信.json').read_bytes(), 'card.json')
    session = store.new_session(SessionCreate(card_id=card['id'], persona_id=persona['id']))
    store.delete_persona(persona['id'])
    assert Store(tmp_path).personas() == []
    assert store.session(session['id'])['persona_data']['description'] == '最新内容'
    with store.connection() as db:
        assert db.execute('SELECT count(*) FROM personas').fetchone()[0] == 0


async def test_cached_download_never_starts_network_process(tmp_path, monkeypatch):
    manager = LocalModelManager(Store(tmp_path))
    settings = Settings()
    target = manager.path(settings.local_models.embedding)
    target.mkdir(parents=True)
    (target / 'tavern-ready.json').write_text(json.dumps({'revision': 'cached'}))
    spawn = Mock(side_effect=AssertionError('must not spawn a download'))
    monkeypatch.setattr('tavern.search.local_models.mp.get_context', spawn)
    job = manager.download('embedding', settings)
    await manager.tasks[job['id']]
    assert job['status'] == 'complete'
    assert job['phase'] == 'cached'
    assert job['progress'] == 1
    spawn.assert_not_called()


async def test_download_metadata_is_progress_not_completion(tmp_path, monkeypatch):
    manager = LocalModelManager(Store(tmp_path))
    settings = Settings()
    target = manager.path(settings.local_models.embedding)
    target.mkdir(parents=True)
    (target / 'model.safetensors').write_bytes(b'x' * 40)
    messages = [{'total_bytes': 100, 'phase': 'downloading'}]
    parent = SimpleNamespace(poll=lambda: bool(messages), recv=lambda: messages.pop(0), close=lambda: None)
    process = SimpleNamespace(start=lambda: None, is_alive=lambda: True, terminate=lambda: None, join=lambda **_: None)
    context = SimpleNamespace(Pipe=lambda: (parent, SimpleNamespace(close=lambda: None)), Process=lambda **_: process)
    monkeypatch.setattr('tavern.search.local_models.mp.get_context', lambda *_: context)
    job = manager.download('embedding', settings)
    await asyncio.sleep(.01)
    assert job['status'] == 'running'
    assert job['downloaded_bytes'] == 40
    assert job['progress'] == .4
    messages.append({'result': {'revision': 'complete'}})
    await manager.tasks[job['id']]
    assert job['status'] == 'complete'
    assert job['downloaded_bytes'] == 100


def test_download_reports_metadata_without_real_network(tmp_path, monkeypatch):
    pipe = Mock()
    info = SimpleNamespace(sha='pinned', siblings=[
        SimpleNamespace(rfilename='model.safetensors', size=1000),
        SimpleNamespace(rfilename='config.json', size=100),
        SimpleNamespace(rfilename='README.md', size=500),
    ])
    def snapshot(*args, **kwargs):
        kwargs['local_dir'].mkdir(parents=True)

    monkeypatch.setitem(sys.modules, 'huggingface_hub', SimpleNamespace(
        HfApi=lambda: SimpleNamespace(model_info=lambda *a, **k: info), snapshot_download=snapshot))
    # Restore proxy environment after the isolated worker function runs in this test process.
    for key in ('HF_HUB_DISABLE_XET', 'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy'):
        monkeypatch.setenv(key, '')
    _download(pipe, tmp_path, {'model': 'fixture/model', 'revision': 'main'}, {'proxy': 'http://proxy.example:8080', 'hf_proxy': True})
    assert pipe.send.call_args_list[0].args[0] == {'total_bytes': 1100, 'phase': 'downloading'}
    assert pipe.send.call_args_list[-1].args[0] == {'result': {'revision': 'pinned'}}
