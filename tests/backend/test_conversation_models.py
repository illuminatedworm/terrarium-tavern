"""Composer selections persist and cannot change the conversation channel."""
import json

import httpx
import pytest
from kohakuterrarium.testing.llm import ScriptedLLM, ScriptEntry

from tavern.application.api import create_app
from tavern.character.editor_runtime_config import editor_runtime_config
from tavern.core.models import SessionCreate, TurnRequest
from tavern.search.retrieval_config import ModelProfile, Provider
from tests.backend.test_card_editor import ToolLLM
from tests.backend.test_full_agent import Model, ScriptHost


def configured_app(path):
    app = create_app(path)
    store = app.state.store
    doc = store.settings_document()
    doc.shared.providers[0].api_key = "fixture-default"
    doc.shared.providers += [Provider(id="second", name="Second", base_url="https://second.test", api_key="fixture-second"),
                             Provider(id="official", name="Official", auth_mode="chatgpt_subscription"),
                             Provider(id="official-two", name="Official two", auth_mode="chatgpt_subscription")]
    for profile in (doc.full_agent.model, doc.half_agent.foreground, doc.half_agent.background, doc.shared.card_editor):
        profile.model = "configured"
    doc.full_agent.official_model = ModelProfile(provider_id="official", model="official-model")
    doc.half_agent.official_foreground = doc.full_agent.official_model.model_copy(deep=True)
    doc.half_agent.official_background = doc.full_agent.official_model.model_copy(deep=True)
    doc.full_agent.tools.auto_retrieval = False
    doc.half_agent.chunk_mode = "off"
    doc.half_agent.retrieval.channels = doc.full_agent.retrieval.channels = ["keyword"]
    store.save_settings(doc)
    card = store.import_card(b'{"name":"Fixture","first_mes":"Opening"}', "fixture.json")
    return app, card


@pytest.mark.parametrize("mode", ["full_agent", "half_agent", "editor"])
async def test_selection_persists_and_actual_run_uses_connection(tmp_path, monkeypatch, mode):
    app, card = configured_app(tmp_path)
    store, service, editor = app.state.store, app.state.service, app.state.editor
    settings = store.settings_document()
    for profile in (settings.full_agent.model, settings.half_agent.foreground, settings.shared.card_editor):
        profile.api_mode, profile.api_path = "responses", "v2"
    store.save_settings(settings)
    configured = store.settings_document().model_dump()
    conversation = (editor.edits.create_conversation() if mode == "editor" else
                    store.new_session(SessionCreate(card_id=card["id"], agent_mode=mode)))
    cid = conversation["id"]
    base = f"/api/card-editor/conversations/{cid}" if mode == "editor" else f"/api/sessions/{cid}"
    selection = {"provider_id": "second", "model": "chosen"}
    configs, messages, half_models = [], [], []

    async def make(cfg, *args, **kwargs):
        configs.append((cfg.base_url, cfg.api_key, cfg.model, cfg.api_mode, cfg.channel_origin))
        if mode == "editor":
            class Capture(ToolLLM):
                async def chat(self, items, **options):
                    messages.extend(items)
                    async for item in super().chat(items, **options):
                        yield item
            return Capture([])
        if mode == "full_agent":
            class Capture(Model):
                async def native_complete(self, items, **options):
                    messages.extend(items)
                    return await super().native_complete(items, **options)
            return Capture("Completed")
        model = ScriptedLLM([ScriptEntry(response="Completed")])
        half_models.append(model)
        return model

    target = {"editor": "tavern.character.card_editor.make_provider", "full_agent": "tavern.full_agent.agent.make_provider",
              "half_agent": "tavern.scripting.agent.make_provider"}[mode]
    monkeypatch.setattr(target, make)
    service.full.runtime = ScriptHost()
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as api:
            response = await api.put(base + "/model", json={"expected_revision": 0, "selection": selection})
            assert response.status_code == 200, response.text
            assert response.json()["model_selection"] == selection
            assert response.json()["channel_origin"] == "standard"
            if mode == "editor":
                assert (await api.get("/api/card-editor/conversations")).json()[0]["channel_origin"] == "standard"
            assert response.json()["revision"] == 0
            rejected = await api.put(base + "/model", json={"expected_revision": 0, "selection": {**selection, "provider_id": "official"}})
            assert rejected.status_code == 400 and "渠道" in rejected.text
            rejected = await api.put(base + "/model", json={"expected_revision": 99, "selection": selection})
            assert rejected.status_code == 409
            if mode == "editor":
                response = await api.post(base + "/runs", json={"text": "Complete content 尾部", "expected_revision": 0, "operation_id": "test"})
                assert response.status_code == 200, response.text
                task = editor.tasks.get(response.json()["id"])
                if task:
                    await task
                assert editor.edits.run(response.json()["id"])["status"] == "completed"
            else:
                turn = service.submit(cid, TurnRequest(text="Complete content 尾部", operation_id="test", expected_revision=0))
                await service.tasks[turn["id"]]
                assert store.turn(turn["id"])["status"] == "committed", store.turn(turn["id"])["error"]
            assert configs and all(cfg == ("https://second.test/v2", "fixture-second", "chosen", "responses", "standard") for cfg in configs)
            if half_models:
                messages.extend(half_models[0].call_log[0])
            if messages:
                # The fake provider receives local cache metadata too; check all
                # business fields that become model messages, not transport tags.
                encoded = json.dumps([{key: value for key, value in message.items() if not key.startswith("_")}
                                      for message in messages], ensure_ascii=False)
                assert "Complete content 尾部" in encoded
                assert "fixture-second" not in encoded and cid not in encoded
            assert store.settings_document().model_dump() == configured
            if mode == "half_agent":
                assert store.session_settings(cid).for_role("background").model == "configured"
            if mode != "editor":
                saved = store.session(cid)
                fork = store.fork_session(cid, saved["head"], saved["revision"])
                assert fork["model_selection"] == selection
        restarted = create_app(tmp_path)
        if mode == "editor":
            assert restarted.state.editor.edits.conversation(cid)["model_selection"] == selection
            assert editor_runtime_config(restarted.state.store, cid, {}).model == "chosen"
        else:
            assert restarted.state.store.session_settings(cid).model == "chosen"
    finally:
        await editor.close()
        await service.close()


async def test_editor_legacy_classification_and_official_provider_boundary(tmp_path):
    app, _ = configured_app(tmp_path)
    store, editor = app.state.store, app.state.editor
    workspace = editor.edits.create_conversation()
    wid = workspace["id"]
    with store.connection() as db:
        db.execute("UPDATE card_editor_conversations SET channel_origin=NULL,model_selection='{}' WHERE id=?", (wid,))
    base = f"/api/card-editor/conversations/{wid}"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as api:
        assert (await api.post(base + "/stats", json={})).status_code == 400
        result = await api.put(base + "/channel-origin", json={"expected_revision": 0, "channel_origin": "chatgpt"})
        assert result.status_code == 200
        assert (await api.get("/api/card-editor/conversations")).json()[0]["channel_origin"] == "chatgpt"
        result = await api.put(base + "/channel-origin", json={"expected_revision": 0, "channel_origin": "standard"})
        assert result.status_code == 409
        for provider in ("official", "official-two"):
            result = await api.put(base + "/model", json={"expected_revision": 0, "selection": {"provider_id": provider, "model": "official-choice"}})
            assert result.status_code == 200
            cfg = editor_runtime_config(store, wid, {})
            assert cfg.channel_origin == "chatgpt" and cfg.auth_mode == "chatgpt_subscription" and cfg.api_mode == "responses"
        result = await api.put(base + "/model", json={"expected_revision": 0, "selection": {"provider_id": "second", "model": "no"}})
        assert result.status_code == 400
        # A global editor setting change cannot move this established chat.
        assert editor_runtime_config(store, wid, {}).auth_mode == "chatgpt_subscription"


@pytest.mark.parametrize("mode", ["full_agent", "half_agent", "editor"])
async def test_selection_is_locked_while_work_runs(tmp_path, mode):
    app, card = configured_app(tmp_path)
    store, editor = app.state.store, app.state.editor
    conversation = (editor.edits.create_conversation() if mode == "editor" else
                    store.new_session(SessionCreate(card_id=card["id"], agent_mode=mode)))
    cid = conversation["id"]
    base = f"/api/card-editor/conversations/{cid}" if mode == "editor" else f"/api/sessions/{cid}"
    with store.connection() as db:
        if mode == "editor":
            db.execute("INSERT INTO card_edit_runs(id,workspace,operation,request,status,created) VALUES ('run',?,'test','{}','running',0)", (cid,))
        else:
            db.execute("INSERT INTO turns(id,session,operation,request,status,created) VALUES ('run',?,'test','{}','awaiting_review',0)", (cid,))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as api:
        result = await api.put(base + "/model", json={"expected_revision": 0, "selection": {"provider_id": "second", "model": "other"}})
        assert result.status_code == 409


@pytest.mark.parametrize("mode", ["full_agent", "half_agent"])
async def test_official_story_can_change_provider_but_not_channel(tmp_path, mode):
    app, card = configured_app(tmp_path)
    store = app.state.store
    session = store.new_session(SessionCreate(card_id=card["id"], agent_mode=mode, channel_origin="chatgpt"))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as api:
        base = f"/api/sessions/{session['id']}/model"
        selected = {"expected_revision": 0, "selection": {"provider_id": "official-two", "model": "new-official"}}
        assert (await api.put(base, json=selected)).status_code == 200
        cfg = store.session_settings(session["id"])
        assert cfg.model == "new-official" and cfg.auth_mode == "chatgpt_subscription"
        selected["selection"]["provider_id"] = "second"
        assert (await api.put(base, json=selected)).status_code == 400
        assert store.session(session["id"])["channel_origin"] == "chatgpt"


async def test_full_agent_resume_uses_new_provider_without_replaying_accepted_work(tmp_path, monkeypatch):
    app, card = configured_app(tmp_path)
    store, service = app.state.store, app.state.service
    service.full.runtime = ScriptHost()
    session = store.new_session(SessionCreate(card_id=card["id"], agent_mode="full_agent"))
    models = [Model(("write_handoff", {"content": "完整保留已完成的工作"}), ValueError("interrupted")), Model("完成正文")]
    connections = []

    async def make(cfg, *args, **kwargs):
        connections.append((cfg.base_url, cfg.model))
        return models[len(connections) - 1]

    monkeypatch.setattr("tavern.full_agent.agent.make_provider", make)
    try:
        first = service.submit(session["id"], TurnRequest(text="继续故事", operation_id="first", expected_revision=0))
        await service.tasks[first["id"]]
        assert store.turn(first["id"])["status"] == "error"
        progress = store.full_agent_progress(first["id"])
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as api:
            result = await api.put(f"/api/sessions/{session['id']}/model", json={"expected_revision": 0,
                                  "selection": {"provider_id": "second", "model": "resume-model"}})
            assert result.status_code == 200
        assert store.full_agent_progress(first["id"]) == progress
        resumed = service.submit(session["id"], TurnRequest(text="继续故事", operation_id="resumed", expected_revision=0,
                                                           resume_turn_id=first["id"]))
        await service.tasks[resumed["id"]]
        assert store.turn(resumed["id"])["status"] == "committed"
        assert connections[-1] == ("https://second.test/v1", "resume-model")
        assert "完整保留已完成的工作" in json.dumps(models[-1].requests, ensure_ascii=False)
        assert len(store.session(session["id"])["messages"]) == 3
    finally:
        await service.close()
