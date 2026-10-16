import json

import httpx
import pytest
from pydantic import ValidationError

from tavern.application.api import create_app
from tavern.application.settings_api import invalidate_capabilities
from tavern.core.cache_policy import endpoint
from tavern.core.settings import SettingsDocument, migrate_settings
from tavern.search.retrieval_config import ModelProfile, Provider
from tavern.storage.store import Store


@pytest.mark.parametrize("mode,path,expected", [
    ("chat_completions", "v1", ""), ("responses", "gateway/v1", "gateway/v1"),
    ("anthropic", "gateway", "gateway/v1"), ("gemini", "v1", ""),
    ("gemini", "gateway/v1alpha", "gateway/v1alpha"),
])
def test_migrates_provider_protocol_and_root_to_all_profiles(tmp_path, mode, path, expected):
    raw = SettingsDocument().model_dump()
    raw["shared"]["providers"][0].update(base_url=f"https://fixture.test/{path}", api_mode=mode)
    for scope, name in (("full_agent", "model"), ("half_agent", "foreground"),
                        ("half_agent", "background"), ("shared", "card_editor")):
        raw[scope][name].pop("api_mode")
        raw[scope][name].pop("api_path")
    migrated = migrate_settings(raw)
    assert migrated.shared.providers[0].base_url == "https://fixture.test"
    assert "api_mode" not in migrated.shared.providers[0].model_dump()
    for role in ("full_agent", "foreground", "background", "card_editor"):
        cfg = migrated.runtime().for_role(role)
        assert cfg.api_mode == mode
        assert cfg.api_path == expected
        assert endpoint(cfg.base_url, mode, "fixture", api_path=cfg.api_path) == endpoint(
            f"https://fixture.test/{path}", mode, "fixture")
    assert migrate_settings(migrated.model_dump()) == migrated
    store = Store(tmp_path)
    with store.connection() as db:
        db.execute("UPDATE meta SET value=? WHERE key='settings'", (json.dumps(raw),))
    assert Store(tmp_path).settings_document() == migrated


def test_flat_legacy_config_and_explicit_profile_overrides():
    doc = migrate_settings({"providers": [{"id": "default", "api_mode": "gemini",
                                          "base_url": "https://fixture.test/v1beta"}],
                            "foreground": {"api_mode": "responses", "api_path": "v2"}})
    assert doc.half_agent.foreground.api_mode == "responses"
    assert doc.half_agent.foreground.api_path == "v2"
    assert doc.half_agent.background.api_mode == "gemini"
    assert doc.shared.card_editor.api_mode == "gemini"


@pytest.mark.parametrize("mode,path,suffix", [
    ("chat_completions", "v2", "/v2/chat/completions"),
    ("responses", "", "/v1/responses"),
    ("anthropic", "v9", "/v9/messages"),
    ("gemini", "v1", "/v1/models/fixture:generateContent"),
    ("gemini", "", "/v1beta/models/fixture:generateContent"),
])
def test_runtime_uses_each_models_path_exactly(mode, path, suffix):
    doc = SettingsDocument()
    doc.full_agent.model = ModelProfile(api_mode=mode, api_path=path, model="fixture")
    cfg = doc.runtime("full_agent")
    assert endpoint(cfg.base_url, cfg.api_mode, cfg.model, api_path=cfg.api_path) == "https://api.openai.com" + suffix
    assert doc.runtime().api_mode == "chat_completions"


@pytest.mark.parametrize("path", ["https://host/v1", "v1?key=x", "../v1", "v1/#x", "v1\\test"])
def test_invalid_model_paths_are_rejected(path):
    with pytest.raises(ValidationError):
        ModelProfile(api_path=path)


def test_provider_root_validation_and_profile_invalidation():
    with pytest.raises(ValidationError):
        Provider(id="bad", base_url="https://fixture.test/v1")
    assert ModelProfile(api_path=" /v1beta/ ").api_path == "v1beta"
    old = SettingsDocument()
    old.full_agent.model.capability = old.half_agent.foreground.capability = "accepted"
    new = old.model_copy(deep=True)
    new.full_agent.model.api_path = "v2"
    invalidate_capabilities(old, new)
    assert new.full_agent.model.capability == "unknown"
    assert new.half_agent.foreground.capability == "accepted"


@pytest.mark.parametrize("mode,path,header", [
    ("chat_completions", "v1", "authorization"), ("responses", "v2", "authorization"),
    ("anthropic", "v9", "x-api-key"), ("gemini", "v1", "x-goog-api-key"),
])
async def test_catalog_cache_survives_restart_and_failed_refresh(tmp_path, monkeypatch, mode, path, header):
    app = create_app(tmp_path)
    doc = app.state.store.settings_document()
    doc.shared.providers[0].api_key = "fixture-secret"
    app.state.store.save_settings(doc)
    real_client = httpx.AsyncClient
    calls = []
    fail = False

    def client(**kwargs):
        if "transport" in kwargs:
            return real_client(**kwargs)  # SDK observer forwards through the configured proxy client.
        assert kwargs["proxy"] is None
        assert kwargs["trust_env"] is False

        def route(request):
            calls.append(str(request.url))
            assert str(request.url) == f"https://api.openai.com/{path}/models"
            assert request.headers[header].endswith("fixture-secret")
            if fail:
                return httpx.Response(503)
            return httpx.Response(200, json={"models": [{"name": "models/a"}], "data": [{"id": "a"}]})
        return real_client(transport=httpx.MockTransport(route))

    monkeypatch.setattr(httpx, "AsyncClient", client)
    query = {"provider_id": "default", "api_mode": mode, "api_path": path}
    async with real_client(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as api:
        assert (await api.get("/api/settings/models", params=query)).json() == {"models": [], "updated_at": None}
        assert not calls
        response = await api.post("/api/settings/models", json=query)
        assert response.status_code == 200, response.text
        cached = response.json()
        assert cached["models"] == ["a"]
        assert cached["updated_at"]
        fail = True
        assert (await api.post("/api/settings/models", json=query)).status_code == 400
        assert (await api.get("/api/settings/models", params=query)).json() == cached
        assert len(calls) == 2
        assert (await api.get("/api/settings/models", params={**query, "api_path": "v3"})).json()["updated_at"] is None

    restarted = create_app(tmp_path)
    async with real_client(transport=httpx.ASGITransport(app=restarted), base_url="http://testserver") as api:
        assert (await api.get("/api/settings/models", params=query)).json() == cached
        doc = restarted.state.store.settings_document()
        doc.shared.providers[0].api_key = "new-secret"
        restarted.state.store.save_settings(doc)
        assert (await api.get("/api/settings/models", params=query)).json()["updated_at"] is None
    assert len(calls) == 2


async def test_catalogs_are_provider_isolated_and_shared_across_openai_protocols(tmp_path, monkeypatch):
    app = create_app(tmp_path)
    doc = app.state.store.settings_document()
    doc.shared.providers = [Provider(id=name, name=name, api_key="same-key") for name in ("one", "two")]
    for profile in (doc.full_agent.model, doc.half_agent.foreground, doc.half_agent.background, doc.shared.card_editor):
        profile.provider_id = "one"
    app.state.store.save_settings(doc)
    real_client = httpx.AsyncClient
    responses = iter((["one-a", "one-b"], ["two-a"]))
    def client(**kwargs):
        return real_client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"data": [{"id": value} for value in next(responses)]})))
    monkeypatch.setattr(httpx, "AsyncClient", client)
    async with real_client(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as api:
        for provider in ("one", "two"):
            result = await api.post("/api/settings/models", json={"provider_id": provider, "api_mode": "responses"})
            assert result.status_code == 200
    restarted = create_app(tmp_path)
    async with real_client(transport=httpx.ASGITransport(app=restarted), base_url="http://testserver") as api:
        for provider, expected in (("one", ["one-a", "one-b"]), ("two", ["two-a"])):
            for mode in ("chat_completions", "responses"):
                result = await api.get("/api/settings/models", params={"provider_id": provider, "api_mode": mode})
                assert result.json()["models"] == expected
        with restarted.state.store.connection() as db:
            values = [row[0] for row in db.execute("SELECT value FROM meta WHERE key LIKE 'provider_models:%'")]
        assert len(values) == 2 and all("same-key" not in value for value in values)


async def test_legacy_provider_catalog_migrates_without_network(tmp_path):
    from tavern.core.cache_policy import fingerprint
    app = create_app(tmp_path)
    provider = app.state.store.settings_document().shared.providers[0]
    url = endpoint(provider.base_url, "responses", resource="models", api_path="")
    with app.state.store.connection() as db:
        for mode, timestamp in (("chat_completions", 1), ("responses", 2)):
            key = "provider_models:" + fingerprint([provider.id, url, mode, fingerprint(provider.api_key)])
            db.execute("INSERT INTO meta VALUES (?,?)", (key, json.dumps({"models": [mode], "updated_at": timestamp})))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as api:
        result = await api.get("/api/settings/models", params={"provider_id": provider.id})
        assert result.json() == {"models": ["responses"], "updated_at": 2}
