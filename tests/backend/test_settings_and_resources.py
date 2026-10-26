import asyncio
import hashlib
import io
import json
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

from tavern.application.api import create_app
from tavern.core.models import PersonaEdit, SessionCreate, Settings
from tavern.search.local_models import LocalModelManager, _download
from tavern.storage.store import Conflict, Store


def dictionary_fixture(monkeypatch):
    from tavern.search import japanese_dictionary as resource
    from tavern.search.retrieval_config import Network

    data = b"pinned compiled dictionary fixture"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(resource.DIC_MEMBER, data)
        archive.writestr("do-not-install.py", "raise AssertionError('must not execute')")
    wheel = buffer.getvalue()
    for name, value in {"DIC": data, "WHEEL": wheel}.items():
        monkeypatch.setattr(resource, name + "_BYTES", len(value))
        monkeypatch.setattr(resource, name + "_SHA256", hashlib.sha256(value).hexdigest())
    return resource, Network(proxy="http://proxy.example:8080", hf_proxy=True), data, wheel


async def test_dictionary_shared_download_failure_cancel_and_retry(tmp_path, monkeypatch):
    resource, network, data, wheel = dictionary_fixture(monkeypatch)
    manager = resource.JapaneseDictionary(tmp_path, lambda: network)
    received = asyncio.Event()
    release = asyncio.Event()
    requests = []
    payload = wheel[:-1]

    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            received.set()
            await release.wait()
            yield payload

    def serve(request):
        requests.append(request)
        assert str(request.url) == resource.URL
        return httpx.Response(200, stream=Body())

    client = httpx.AsyncClient

    def factory(**kwargs):
        assert kwargs["proxy"] == network.proxy and kwargs["trust_env"] is False
        return client(transport=httpx.MockTransport(serve))

    monkeypatch.setattr(resource.httpx, "AsyncClient", factory)
    first = asyncio.create_task(manager.ensure())
    await received.wait()
    second = asyncio.create_task(manager.ensure())
    task = manager.task
    assert manager.start() is task and manager.state == "downloading"
    # One story cancellation leaves the shared resource job alive.
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert not task.done()
    release.set()
    with pytest.raises(resource.DictionaryUnavailable):
        await second
    assert manager.state == "error" and "SHA-256" in manager.error
    assert not manager.path.exists() and not list(manager.root.glob("*.part"))
    with pytest.raises(resource.DictionaryUnavailable):
        await manager.ensure()
    assert len(requests) == 1  # No implicit retry loop after failure.

    received.clear()
    release.clear()
    manager.start(retry=True)
    await received.wait()
    await manager.cancel()
    assert manager.state == "cancelled" and not manager.path.exists()
    assert not list(manager.root.glob("*.part"))
    payload = wheel
    release.set()
    await manager.start(retry=True)
    assert manager.is_ready() and manager.path.read_bytes() == data
    assert len(requests) == 3 and not (manager.root / "do-not-install.py").exists()
    await manager.ensure()
    assert len(requests) == 3
    await manager.close()


async def test_dictionary_offline_import_atomic_integrity_and_restart(tmp_path, monkeypatch):
    resource, network, data, wheel = dictionary_fixture(monkeypatch)
    manager = resource.JapaneseDictionary(tmp_path, lambda: network)
    # Interrupted files from another run must never count as the active dictionary.
    manager.root.mkdir(parents=True)
    (manager.root / "system.dic.part").write_bytes(data[:4])
    assert not manager.is_ready()

    async def stream(value):
        yield value

    async def disconnected():
        yield data[:4]
        raise resource.ClientDisconnect()

    await manager.start(retry=True, stream=disconnected(), size=len(data), kind="dic")
    assert manager.state == "cancelled" and "连接已断开" in manager.error
    assert not manager.path.exists() and not list(manager.root.glob("*.part"))
    await manager.start(retry=True, stream=stream(b"x" * len(wheel)), size=len(wheel))
    assert manager.state == "error" and not manager.path.exists()
    await manager.start(retry=True, stream=stream(wheel), size=len(wheel))
    assert manager.is_ready() and manager.path.read_bytes() == data
    with pytest.raises(ValueError, match="当前文件未导入"):
        manager.start(retry=True, stream=stream(b"invalid"), size=7)
    assert manager.path.read_bytes() == data
    await manager.close()

    restarted = resource.JapaneseDictionary(tmp_path, lambda: network)
    assert restarted.state == "unchecked"
    monkeypatch.setattr(resource.httpx, "AsyncClient", Mock(side_effect=AssertionError("offline")))
    await restarted.ensure()
    assert restarted.is_ready()
    await restarted.close()
    # A same-size corrupt final file also fails on restart; only a verified import replaces it.
    manager.path.write_bytes(b"x" * len(data))
    repair = resource.JapaneseDictionary(tmp_path, lambda: network)
    with pytest.raises(resource.DictionaryUnavailable):
        await repair.ensure()
    await repair.start(retry=True, stream=stream(data), size=len(data), kind="dic")
    assert repair.is_ready() and repair.path.read_bytes() == data
    observer = repair.events()
    assert (await anext(observer))["state"] == "ready"
    repair.stop()
    with pytest.raises(StopAsyncIteration):
        await anext(observer)
    await repair.close()


async def test_proxy_defaults_are_direct_and_explicit_settings_survive(monkeypatch):
    from tavern.chatgpt.service import ChatGPTAccounts
    from tavern.core.provider import make_provider
    from tavern.core.settings import SettingsDocument, migrate_settings
    from tavern.scripting.model_transport import ModelTransport
    from tavern.web.client import WebTools

    doc = SettingsDocument()
    assert doc.shared.network.proxy == Settings().proxy == ""
    assert not any(
        (doc.shared.network.api_proxy, doc.shared.network.hf_proxy, doc.shared.network.resources_proxy)
    )
    assert not doc.shared.providers[0].use_proxy and not Settings().use_proxy
    saved_proxy = "http://proxy.example:8080"
    doc.shared.network.proxy = saved_proxy
    doc.shared.network.api_proxy = doc.shared.network.hf_proxy = doc.shared.network.resources_proxy = True
    doc.shared.providers[0].use_proxy = True
    doc.shared.send_shortcut = "enter"
    # Saved values from an earlier installation remain authoritative after defaults change.
    doc.half_agent.foreground.context_tokens, doc.half_agent.foreground.reply_tokens = 32768, 2048
    doc.half_agent.chunk_mode, doc.half_agent.chunk_max_tokens = "messages", 6000
    doc.half_agent.memory_output_tokens, doc.half_agent.memory_capacity_tokens = 4096, 4096
    assert migrate_settings(doc.model_dump()) == doc
    assert doc.runtime().use_proxy and doc.runtime().reply_tokens == 2048
    calls = []
    real_client = httpx.AsyncClient

    class Client(real_client):
        def __init__(self, **kwargs):
            calls.append(kwargs)
            super().__init__(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", Client)
    # Environment variables must not silently override the visible app settings.
    monkeypatch.setenv("HTTPS_PROXY", "http://environment.example:8080")
    for address, enabled, expected in (
        ("", True, None),
        (saved_proxy, True, saved_proxy),
        (saved_proxy, False, None),
    ):
        cfg = Settings(model="fixture", api_key="fixture-key", proxy=address, use_proxy=enabled)
        cfg.network.proxy, cfg.network.api_proxy = address, enabled
        for provider in (await make_provider(cfg), ModelTransport(cfg, "fixture-key")):
            await provider.close()
        async with WebTools(cfg.web, cfg.network).client():
            pass
        accounts = SimpleNamespace(network=lambda: cfg.network, http_transport=None)
        async with ChatGPTAccounts.client(accounts):
            pass
        assert len(calls) == 4
        assert all(call["proxy"] == expected and call["trust_env"] is False for call in calls)
        calls.clear()


async def test_model_listing_uses_saved_key_and_proxy(tmp_path, monkeypatch):
    app = create_app(tmp_path)
    settings = app.state.store.settings()
    settings.providers[0].api_key = "secret"
    settings.network.proxy = "http://proxy.example:8080"
    settings.network.api_proxy = settings.providers[0].use_proxy = True
    app.state.store.save_settings(settings)
    real_client = httpx.AsyncClient
    calls = []

    def client(**kwargs):
        calls.append(kwargs)

        def respond(request):
            assert str(request.url) == "https://api.openai.com/v1/models"
            assert request.headers["authorization"] == "Bearer secret"
            return httpx.Response(200, json={"data": [{"id": "b"}, {"id": "a"}, {"id": "b"}, {}]})

        return real_client(transport=httpx.MockTransport(respond))

    monkeypatch.setattr(httpx, "AsyncClient", client)
    async with real_client(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as api:
        response = await api.post("/api/settings/models", json={"provider_id": "default"})
        assert response.status_code == 200
        assert response.json()["models"] == ["a", "b"]
        assert response.json()["updated_at"] is not None
    assert calls[0]["proxy"] == "http://proxy.example:8080"
    assert calls[0]["trust_env"] is False


def test_personas_only_keep_latest_and_delete_physically(tmp_path):
    store = Store(tmp_path)
    persona = store.save_persona(None, PersonaEdit(name="玩家", description="旧内容"))
    persona = store.save_persona(
        persona["id"], PersonaEdit(name="玩家", description="最新内容", expected_version=1)
    )
    with pytest.raises(Conflict):
        store.save_persona(persona["id"], PersonaEdit(name="覆盖", description="", expected_version=1))
    assert store.personas() == [persona]
    card = store.import_card(Path("samples/雾港来信.json").read_bytes(), "card.json")
    session = store.new_session(SessionCreate(card_id=card["id"], persona_id=persona["id"]))
    store.delete_persona(persona["id"])
    assert Store(tmp_path).personas() == []
    assert store.session(session["id"])["persona_data"]["description"] == "最新内容"
    with store.connection() as db:
        assert db.execute("SELECT count(*) FROM personas").fetchone()[0] == 0


async def test_cached_download_never_starts_network_process(tmp_path, monkeypatch):
    manager = LocalModelManager(Store(tmp_path))
    settings = Settings()
    target = manager.path(settings.local_models.embedding)
    target.mkdir(parents=True)
    (target / "tavern-ready.json").write_text(json.dumps({"revision": "cached"}))
    spawn = Mock(side_effect=AssertionError("must not spawn a download"))
    monkeypatch.setattr("tavern.search.local_models.mp.get_context", spawn)
    job = manager.download("embedding", settings)
    await manager.tasks[job["id"]]
    assert job["status"] == "complete"
    assert job["phase"] == "cached"
    assert job["progress"] == 1
    spawn.assert_not_called()


async def test_download_metadata_is_progress_not_completion(tmp_path, monkeypatch):
    manager = LocalModelManager(Store(tmp_path))
    settings = Settings()
    target = manager.path(settings.local_models.embedding)
    target.mkdir(parents=True)
    (target / "model.safetensors").write_bytes(b"x" * 40)
    messages = [{"total_bytes": 100, "phase": "downloading"}]
    parent = SimpleNamespace(poll=lambda: bool(messages), recv=lambda: messages.pop(0), close=lambda: None)
    process = SimpleNamespace(
        start=lambda: None, is_alive=lambda: True, terminate=lambda: None, join=lambda **_: None
    )
    context = SimpleNamespace(
        Pipe=lambda: (parent, SimpleNamespace(close=lambda: None)), Process=lambda **_: process
    )
    monkeypatch.setattr("tavern.search.local_models.mp.get_context", lambda *_: context)
    job = manager.download("embedding", settings)
    await asyncio.sleep(0.01)
    assert job["status"] == "running"
    assert job["downloaded_bytes"] == 40
    assert job["progress"] == 0.4
    messages.append({"result": {"revision": "complete"}})
    await manager.tasks[job["id"]]
    assert job["status"] == "complete"
    assert job["downloaded_bytes"] == 100


def test_download_reports_metadata_without_real_network(tmp_path, monkeypatch):
    pipe = Mock()
    info = SimpleNamespace(
        sha="pinned",
        siblings=[
            SimpleNamespace(rfilename="model.safetensors", size=1000),
            SimpleNamespace(rfilename="config.json", size=100),
            SimpleNamespace(rfilename="README.md", size=500),
        ],
    )

    def snapshot(*args, **kwargs):
        kwargs["local_dir"].mkdir(parents=True)

    monkeypatch.setitem(
        sys.modules,
        "huggingface_hub",
        SimpleNamespace(
            HfApi=lambda: SimpleNamespace(model_info=lambda *a, **k: info), snapshot_download=snapshot
        ),
    )
    # Restore proxy environment after the isolated worker function runs in this test process.
    for key in (
        "HF_HUB_DISABLE_XET",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ):
        monkeypatch.setenv(key, "")
    _download(
        pipe,
        tmp_path,
        {"model": "fixture/model", "revision": "main"},
        {"proxy": "http://proxy.example:8080", "hf_proxy": True},
    )
    assert pipe.send.call_args_list[0].args[0] == {"total_bytes": 1100, "phase": "downloading"}
    assert pipe.send.call_args_list[-1].args[0] == {"result": {"revision": "pinned"}}
