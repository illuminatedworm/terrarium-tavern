"""Configuration isolation is asserted at persistence, runtime and API boundaries."""
import json
import sqlite3
from copy import deepcopy
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from tavern.application.api import create_app
from tavern.core.models import SessionCreate, Settings
from tavern.core.settings import SettingsDocument, migrate_settings
from tavern.storage.store import Store


@pytest.mark.parametrize("scope,path", [("full_agent", "model"), ("half_agent", "foreground")])
async def test_sidebar_retention_persists_with_revision_and_mode_isolation(tmp_path, scope, path):
    app = create_app(tmp_path)
    store = app.state.store
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        before = (await client.get("/api/settings")).json()
        assert before[scope][path]["cache"]["retention"] == "default"
        payload = {"expected_revision": before["revisions"][scope],
                   "changes": {f"{path}.cache.retention": "1h"}}
        changed = await client.patch(f"/api/settings/{scope}", json=payload)
        assert changed.status_code == 200
        after = changed.json()
        assert after[scope][path]["cache"]["retention"] == "1h"
        other = "half_agent" if scope == "full_agent" else "full_agent"
        assert after[other] == before[other]
        assert (await client.patch(f"/api/settings/{scope}", json=payload)).status_code == 409
        payload["expected_revision"] = after["revisions"][scope]
        payload["changes"][f"{path}.cache.retention"] = "forever"
        assert (await client.patch(f"/api/settings/{scope}", json=payload)).status_code == 400
    restored = Store(store.root).settings_document()
    assert getattr(getattr(restored, scope), path).cache.retention == "1h"
    assert Store(store.root).settings(scope).cache.retention == "1h"


def test_existing_prompt_terminology_is_migrated_once_without_rewriting_story_data(tmp_path):
    store = Store(tmp_path)
    card = store.import_card(json.dumps({"name": "向导", "first_mes": "玩家走进旧港。"}).encode(), "card.json")
    session = store.new_session(SessionCreate(card_id=card["id"], persona="玩家甲"))
    raw = store.settings_document().model_dump()
    raw["revisions"] = {"full_agent": 3, "half_agent": 8, "shared": 7}
    for scope in ("full_agent", "half_agent"):
        raw[scope]["system_prompt"] = "自定义开头：玩家扮演人类角色；Player 的选择由 {{user}} 决定。"
        raw[scope]["preference_prompt"] = "玩家偏好："
        raw[scope]["regenerate_prompt"] = "本次 player 要求："
        raw[scope]["local_runtime"]["instruction_prompt"] = "Retrieve the player's setting."
    raw["half_agent"]["maintenance_system_prompt"] = "完成玩家明确提出的修改。"
    original = deepcopy(raw)
    migrated = migrate_settings(raw)
    assert raw == original
    assert migrated.revisions == {"full_agent": 4, "half_agent": 9, "shared": 7}
    assert migrate_settings(migrated.model_dump()) == migrated
    with store.connection() as db:
        db.execute("UPDATE meta SET value=? WHERE key='settings'", (json.dumps(raw),))
    reopened = Store(tmp_path)
    assert reopened.settings_document() == migrated
    assert Store(tmp_path).settings_document() == migrated
    for mode in ("full_agent", "half_agent"):
        settings = reopened.settings(mode)
        assert settings.system_prompt == "自定义开头：用户扮演人类角色；user 的选择由 {{user}} 决定。"
        assert settings.preference_prompt == "用户偏好："
        assert settings.regenerate_prompt == "本次 user 要求："
        assert settings.local_models.instruction_prompt == "Retrieve the user's setting."
    assert reopened.session(session["id"])["persona"] == "玩家甲"
    assert reopened.session(session["id"])["messages"][0]["raw"] == "玩家走进旧港。"


def test_migration_copies_effective_values_and_keeps_secrets(tmp_path):
    store = Store(tmp_path)
    legacy = Settings()
    legacy.providers[0].api_key = "migration-secret"
    legacy.foreground.model = "writer-before"
    legacy.background.model = "background-before"
    legacy.full_agent = None
    legacy.retrieval.chunk_tokens = 768
    legacy.local_models.instruction_prompt = "custom-local"
    legacy.system_prompt = "custom-half"
    legacy.full_agent_system_prompt = "custom-full"
    with store.connection() as db:
        db.execute("UPDATE meta SET value='1' WHERE key='schema_version'")
        db.execute("UPDATE meta SET value=? WHERE key='settings'", (legacy.model_dump_json(),))
    store = Store(tmp_path)
    doc = store.settings_document()
    assert doc.full_agent.model.model == doc.half_agent.foreground.model == "writer-before"
    assert doc.shared.card_editor.model == "background-before"
    assert doc.full_agent.local_runtime.instruction_prompt == doc.half_agent.local_runtime.instruction_prompt == "custom-local"
    assert doc.shared.providers[0].api_key == "migration-secret"
    assert doc.full_agent.system_prompt == "custom-full"
    assert doc.half_agent.system_prompt == "custom-half"
    backups = list(tmp_path.glob("settings-before-v2-*.sqlite3"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as db:
        assert db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == "1"
    doc.full_agent.retrieval.chunk_tokens = 1024
    store.save_settings(doc)
    reopened = Store(tmp_path).settings_document()
    assert reopened.full_agent.retrieval.chunk_tokens == 1024
    assert reopened.half_agent.retrieval.chunk_tokens == 768
    assert len(list(tmp_path.glob("settings-before-v2-*.sqlite3"))) == 1


def test_explicit_full_model_and_operation_snapshot_are_independent():
    old = Settings()
    old.full_agent = old.foreground.model_copy(update={"model": "full-before"})
    doc = migrate_settings(old.model_dump())
    assert doc.full_agent.model.model == "full-before"
    doc.full_agent.model.context_tokens = 6000
    doc.full_agent.model.reply_tokens = 1024
    doc.full_agent.lore_max_entries = 2
    doc.full_agent.local_runtime.reranker_prompt = "full-ranking"
    full = doc.runtime().for_role("full_agent")
    half = doc.runtime()
    doc.full_agent.model.model = "changed-later"
    assert full.model == "full-before"
    assert full.context_tokens == 6000
    assert full.for_role("background").model == "full-before"
    assert full.lore_max_entries == 2 and half.lore_max_entries == 5
    assert full.local_models.reranker_prompt == "full-ranking"
    assert half.local_models.reranker_prompt != "full-ranking"
    full.retrieval.channels.append("embedding")
    assert "embedding" not in doc.full_agent.retrieval.channels


async def test_scoped_updates_undo_and_secret_preservation(tmp_path):
    app = create_app(tmp_path)
    doc = app.state.store.settings_document()
    doc.shared.providers[0].api_key = "old-secret"
    app.state.store.save_settings(doc)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        opened = (await client.post("/api/settings/edit-sessions", json={})).json()
        token, original = opened["id"], opened["settings"]
        assert "old-secret" not in json.dumps(opened)
        async def patch(scope, changes, revision=None):
            current = (await client.get("/api/settings")).json()
            return await client.patch(f"/api/settings/{scope}", json={"changes": changes, "edit_session": token,
                "expected_revision": current["revisions"][scope] if revision is None else revision})
        response = await patch("full_agent", {"model.model": "new-full", "lore_max_entries": 9, "system_prompt": "new-full-prompt"})
        assert response.status_code == 200, response.text
        saved = response.json()
        assert saved["half_agent"] == original["half_agent"]
        assert app.state.store.settings("full_agent").system_prompt == "new-full-prompt"
        assert app.state.store.settings("full_agent").full_agent_system_prompt == "new-full-prompt"
        invalid = await patch("full_agent", {"model.context_tokens": 2048})
        assert invalid.status_code == 400
        assert (await client.get("/api/settings")).json() == saved
        stale = await patch("full_agent", {"model.model": "stale"}, revision=0)
        assert stale.status_code == 409
        undo = await client.post(f"/api/settings/edit-sessions/{token}/undo", json={"page":"full_agent.model", "expected_revision":saved["revisions"]["full_agent"]})
        assert undo.status_code == 200, undo.text
        assert undo.json()["full_agent"]["model"] == original["full_agent"]["model"]
        assert undo.json()["full_agent"]["system_prompt"] == "new-full-prompt"
        providers = deepcopy(original["shared"]["providers"])
        providers[0].update(name="changed-name", api_key="")
        assert (await patch("shared", {"providers":providers})).status_code == 200
        assert app.state.store.settings_document().shared.providers[0].api_key == "old-secret"
        providers[0]["api_key"] = "new-secret"
        result = await patch("shared", {"providers":providers})
        assert result.status_code == 200
        assert "new-secret" not in result.text
        undo = await client.post(f"/api/settings/edit-sessions/{token}/undo", json={"page":"shared.providers", "expected_revision":result.json()["revisions"]["shared"]})
        assert undo.status_code == 200, undo.text
        assert app.state.store.settings_document().shared.providers[0].api_key == "old-secret"
        assert "old-secret" not in undo.text
        await client.delete(f"/api/settings/edit-sessions/{token}")
        assert (await patch("full_agent", {"model.model":"expired"})).status_code == 409
        assert (await client.put("/api/settings", json={})).status_code == 405


async def test_other_window_changes_cannot_be_undone_and_capabilities_cannot_be_forged(tmp_path):
    app = create_app(tmp_path)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        a = (await client.post("/api/settings/edit-sessions", json={})).json()
        b = (await client.post("/api/settings/edit-sessions", json={})).json()
        result = await client.patch("/api/settings/full_agent", json={"expected_revision":0,"edit_session":b["id"],"changes":{"model.model":"other-window"}})
        assert result.status_code == 200
        undo = await client.post(f"/api/settings/edit-sessions/{a['id']}/undo", json={"page":"full_agent.model","expected_revision":1})
        assert undo.status_code == 409
        fake = await client.patch("/api/settings/full_agent", json={"expected_revision":1,"changes":{"model.capability":"accepted"}})
        assert fake.status_code == 400
        assert app.state.store.settings("full_agent").model == "other-window"


def test_modes_inherit_their_own_policy_and_card_editor_stays_independent(tmp_path):
    store = Store(tmp_path)
    card = store.import_card(Path("samples/雾港来信.json").read_bytes(), "card.json")
    full = store.new_session(SessionCreate(card_id=card["id"], agent_mode="full_agent"))
    half = store.new_session(SessionCreate(card_id=card["id"], agent_mode="half_agent"))
    doc = store.settings_document()
    doc.full_agent.policy = "review"
    doc.half_agent.background.model = "changed-background"
    store.save_settings(doc)
    assert store.session(full["id"])["policy_effective"] == "review"
    assert store.session(half["id"])["policy_effective"] == "automatic"
    assert store.settings().for_role("card_editor").model != "changed-background"


def test_unsupported_version_is_rejected_without_touching_database(tmp_path):
    store = Store(tmp_path)
    with store.connection() as db:
        db.execute("UPDATE meta SET value='999' WHERE key='schema_version'")
    before = store.path.read_bytes()
    with pytest.raises(ValueError):
        Store(tmp_path)
    assert store.path.read_bytes() == before


def test_full_agent_small_valid_budget_does_not_validate_half_memory_budget():
    doc = SettingsDocument()
    doc.full_agent.model.context_tokens = 2200
    doc.full_agent.model.reply_tokens = 128
    assert doc.runtime("full_agent").context_tokens == 2200
