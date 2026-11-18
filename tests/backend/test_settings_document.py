"""Configuration isolation is asserted at persistence, runtime and API boundaries."""

import json
import sqlite3
from copy import deepcopy

import pytest
from httpx import ASGITransport, AsyncClient

from tavern.application.api import create_app
from tavern.core.models import SessionCreate, Settings
from tavern.core.settings import SettingsDocument, from_runtime, migrate_settings
from tavern.storage.legacy_story import OLD_STORY_KEY
from tavern.storage.store import Store


async def test_story_model_undo_only_retires_cache_for_changed_connections(tmp_path):
    from tavern.storage.story_cache import story_cache_window

    app = create_app(tmp_path)
    store = app.state.store
    doc = store.settings_document()
    doc.story.model.provider_id, doc.story.model.model = "default", "A"
    store.save_settings(doc)
    card = store.import_card(b'{"name":"fixture","first_mes":"Opening"}')
    options = SessionCreate(card_id=card["id"])
    inherited, pinned = store.new_session(options), store.new_session(options)
    usage = {"prompt_tokens": 100, "cached_tokens": 70}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        (
            await client.put(
                f"/api/sessions/{pinned['id']}/model",
                json={"selection": {"provider_id": "default", "model": "A"}, "expected_revision": 0},
            )
        ).raise_for_status()
        for session in (inherited, pinned):
            sid = session["id"]
            cfg = store.session_settings(sid).for_role("story")
            window = story_cache_window(session["snapshot"], cfg.context_strategy)
            store.story_cache.begin(sid, "turn", cfg, session["snapshot"])
            with store.connection() as db:
                store.story_cache.record(db, sid, "turn", window, usage)
                store.story_cache.finish(db, sid, "turn", "committed", snapshot=session["snapshot"])
        opened = (await client.post("/api/settings/edit-sessions")).json()
        updated = await client.patch(
            "/api/settings/story",
            json={
                "edit_session": opened["id"],
                "expected_revision": opened["settings"]["revisions"]["story"],
                "changes": {"model.model": "B"},
            },
        )
        updated.raise_for_status()
        (
            await client.post(
                f"/api/settings/edit-sessions/{opened['id']}/undo",
                json={"page": "story.model", "expected_revision": updated.json()["revisions"]["story"]},
            )
        ).raise_for_status()
        for session, expected in ((inherited, None), (pinned, usage)):
            preview = await client.post(f"/api/sessions/{session['id']}/preview", json={})
            preview.raise_for_status()
            assert preview.json()["cache_usage"] == expected


async def test_composer_shortcut_persistence_validation_and_page_undo(tmp_path):
    app = create_app(tmp_path)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        opened = (await client.post("/api/settings/edit-sessions", json={})).json()
        original = opened["settings"]
        assert original["shared"]["send_shortcut"] == "ctrl_enter"
        pages = [page["key"] for page in original["pages"] if page["scope"] == "shared"]
        assert pages[-1] == "misc"
        assert pages.index("preference_presets") == pages.index("editor") + 1
        revision = original["revisions"]["shared"]
        for shortcut in ("enter", "shift_enter", "button_only"):
            response = await client.patch(
                "/api/settings/shared",
                json={
                    "expected_revision": revision,
                    "edit_session": opened["id"],
                    "changes": {"send_shortcut": shortcut},
                },
            )
            assert response.status_code == 200, response.text
            revision = response.json()["revisions"]["shared"]
            assert Store(tmp_path).settings_document().shared.send_shortcut == shortcut
        invalid = await client.patch(
            "/api/settings/shared",
            json={
                "expected_revision": revision,
                "changes": {"send_shortcut": "unknown"},
            },
        )
        assert invalid.status_code == 400
        assert app.state.store.settings_document().shared.send_shortcut == "button_only"
        undo = await client.post(
            f"/api/settings/edit-sessions/{opened['id']}/undo",
            json={
                "page": "shared.misc",
                "expected_revision": revision,
            },
        )
        assert undo.status_code == 200, undo.text
        assert undo.json()["shared"]["send_shortcut"] == "ctrl_enter"
        assert undo.json()["story"] == original["story"]
        assert undo.json()["story"] == original["story"]


async def test_novel_delivery_defaults_share_main_mapping_and_page_undo(tmp_path):
    """Both global entry points must save and undo one mapping without changing existing stories."""
    app = create_app(tmp_path)
    store = app.state.store
    card = store.import_card(b'{"name":"fixture","first_mes":"Opening"}')
    existing = store.new_session(SessionCreate(card_id=card["id"]))
    before = store.message_adapter("story", existing["id"], "story")["config"]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        opened = (await client.post("/api/settings/edit-sessions", json={})).json()
        original = opened["settings"]
        page = next(page for page in original["pages"] if page["id"] == "story.novel")
        assert page["paths"] == ["novel_research", "model.message_adapter"]
        mapping = {
            "version": 1,
            "mappings": {"gemini": {"novel_research": {"role": "assistant", "wrap": False}}},
        }
        changed = await client.patch(
            "/api/settings/story",
            json={
                "expected_revision": original["revisions"]["story"],
                "edit_session": opened["id"],
                "changes": {"model.message_adapter": mapping},
            },
        )
        assert changed.status_code == 200, changed.text
        assert store.settings_document().story.model.message_adapter.model_dump() == mapping
        created = store.new_session(SessionCreate(card_id=card["id"]))
        assert store.message_adapter("story", created["id"], "story")["config"] == mapping
        undo = await client.post(
            f"/api/settings/edit-sessions/{opened['id']}/undo",
            json={"page": page["id"], "expected_revision": changed.json()["revisions"]["story"]},
        )
        assert undo.status_code == 200, undo.text
        assert undo.json()["story"] == original["story"]
        assert store.message_adapter("story", created["id"], "story")["config"] == mapping
        assert store.message_adapter("story", existing["id"], "story")["config"] == before


async def test_sidebar_retention_persists_with_revision_and_scope_isolation(tmp_path):
    scope, path = "story", "model"
    app = create_app(tmp_path)
    store = app.state.store
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        before = (await client.get("/api/settings")).json()
        assert before[scope][path]["cache"]["retention"] == "default"
        payload = {
            "expected_revision": before["revisions"][scope],
            "changes": {f"{path}.cache.retention": "1h"},
        }
        changed = await client.patch(f"/api/settings/{scope}", json=payload)
        assert changed.status_code == 200
        after = changed.json()
        assert after[scope][path]["cache"]["retention"] == "1h"
        assert after["shared"] == before["shared"]
        assert (await client.patch(f"/api/settings/{scope}", json=payload)).status_code == 409
        payload["expected_revision"] = after["revisions"][scope]
        payload["changes"][f"{path}.cache.retention"] = "forever"
        assert (await client.patch(f"/api/settings/{scope}", json=payload)).status_code == 400
    restored = Store(store.root).settings_document()
    assert getattr(getattr(restored, scope), path).cache.retention == "1h"
    assert Store(store.root).settings().cache.retention == "1h"


def test_removed_reply_locate_switch_is_dropped_from_strict_v17_settings():
    # Saved and exported v17 documents carry the switch; the strict model must still load them.
    raw = SettingsDocument().model_dump()
    raw["version"] = 17
    raw["shared"]["reply_locate_start"] = False
    migrated = migrate_settings(raw)
    assert migrated.version == 22 and "reply_locate_start" not in migrated.shared.model_dump()
    assert migrated.revisions["shared"] == raw["revisions"]["shared"] + 1


def test_migration_renames_only_legacy_siwc_defaults():
    raw = SettingsDocument().model_dump()
    original = raw["shared"]["providers"]
    original[0]["name"] = "OpenAI Official"
    original.extend(
        {
            **original[0],
            "id": f"siwc-{index}",
            "name": name,
            "auth_mode": "chatgpt_subscription",
            "chatgpt_account_id": f"{index:032x}",
            "use_proxy": True,
        }
        for index, name in enumerate(("OpenAI Official", "ChatGPT 订阅", "我的工作区"), start=1)
    )
    migrated = migrate_settings(raw)
    expected = deepcopy(original)
    for provider in expected[1:3]:
        provider["name"] = "Sign In With ChatGPT"
    assert migrated.model_dump()["shared"]["providers"] == expected
    assert migrated.revisions["shared"] == raw["revisions"]["shared"] + 1
    assert migrate_settings(migrated.model_dump()) == migrated


def test_upgrade_keeps_custom_prompt_wording_and_story_data_verbatim(tmp_path):
    store = Store(tmp_path)
    card = store.import_card(json.dumps({"name": "向导", "first_mes": "玩家走进旧港。"}).encode())
    session = store.new_session(SessionCreate(card_id=card["id"], persona="玩家甲"))
    raw = store.settings_document().model_dump()
    raw["version"] = 9
    raw["story"]["tools"].pop("max_duration_seconds")
    raw["story"]["retrieval"].pop("index_timeout")
    raw["revisions"] = {"story": 3, "shared": 7}
    raw["story"]["system_prompt"] = "自定义开头：玩家扮演人类角色；Player 的选择由 {{user}} 决定。"
    raw["story"]["preference_prompt"] = "玩家偏好："
    raw["story"]["regenerate_prompt"] = "本次 player 要求："
    raw["story"]["local_runtime"]["instruction_prompt"] = "Retrieve the player's setting."
    original = deepcopy(raw)
    migrated = migrate_settings(raw)
    assert migrated.version == 22 and migrated.story.tools.max_duration_seconds == 900
    assert migrated.story.retrieval.index_timeout == 60
    assert raw == original
    assert migrated.revisions == {"story": 4, "shared": 8}
    assert migrate_settings(migrated.model_dump()) == migrated
    custom_timeout = migrated.model_copy(deep=True)
    custom_timeout.story.retrieval.index_timeout = 180
    assert migrate_settings(custom_timeout.model_dump()) == custom_timeout
    with store.connection() as db:
        db.execute(f"ALTER TABLE sessions ADD COLUMN agent_mode TEXT NOT NULL DEFAULT '{OLD_STORY_KEY}'")
        db.execute("UPDATE meta SET value='11' WHERE key='schema_version'")
        db.execute("UPDATE meta SET value=? WHERE key='settings'", (json.dumps(raw),))
    reopened = Store(tmp_path)
    assert reopened.settings_document() == migrated
    assert Store(tmp_path).settings_document() == migrated
    assert len(list(tmp_path.glob("before-upgrade-schema11-settings9-*.sqlite3"))) == 1
    settings = reopened.settings()
    assert settings.story_system_prompt == "自定义开头：玩家扮演人类角色；Player 的选择由 {{user}} 决定。"
    assert settings.preference_prompt == "玩家偏好："
    assert settings.regenerate_prompt == "本次 player 要求："
    assert settings.local_models.instruction_prompt == "Retrieve the player's setting."
    assert reopened.session(session["id"])["persona"] == "玩家甲"
    assert reopened.session(session["id"])["messages"][0]["raw"] == "玩家走进旧港。"


def test_migration_copies_effective_values_and_keeps_secrets(tmp_path):
    store = Store(tmp_path)
    legacy = Settings().model_dump()
    legacy["providers"][0]["api_key"] = "migration-secret"
    legacy["story"] = None
    legacy["foreground"] = {"model": "story-before"}
    legacy["background"] = {"model": "editor-before"}
    legacy.pop("card_author")
    legacy["retrieval"]["chunk_tokens"] = 768
    legacy["local_models"]["instruction_prompt"] = "custom-local"
    legacy["story_system_prompt"] = "custom-full"
    with store.connection() as db:
        db.execute("UPDATE meta SET value='1' WHERE key='schema_version'")
        db.execute("UPDATE meta SET value=? WHERE key='settings'", (json.dumps(legacy),))
    store = Store(tmp_path)
    doc = store.settings_document()
    assert doc.story.model.model == "story-before"
    assert doc.shared.card_author.model == "editor-before"
    assert doc.story.local_runtime.instruction_prompt == "custom-local"
    assert doc.story.retrieval.chunk_tokens == 768
    assert doc.shared.providers[0].api_key == "migration-secret"
    assert doc.story.system_prompt.current == "custom-full"
    backups = list(tmp_path.glob("before-upgrade-*.sqlite3"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as db:
        assert db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == "1"
    doc.story.retrieval.chunk_tokens = 1024
    store.save_settings(doc)
    reopened = Store(tmp_path).settings_document()
    assert reopened.story.retrieval.chunk_tokens == 1024
    assert len(list(tmp_path.glob("before-upgrade-*.sqlite3"))) == 1


def test_explicit_story_model_and_operation_snapshot_are_independent():
    old = Settings(story={"model": "full-before"})
    doc = migrate_settings(old.model_dump())
    assert doc.story.model.model == "full-before"
    doc.story.model.context_tokens = 6000
    doc.story.model.reply_tokens = 1024
    doc.story.lore_max_entries = 2
    doc.story.local_runtime.reranker_prompt = "full-ranking"
    choices = doc.story.system_prompt
    choices.active = 2
    choices.replace_current("本次使用的规则")
    choices.options[2].text = "保留的备用规则"
    full = doc.runtime().for_role("story")
    doc.story.model.model = "changed-later"
    assert full.model == "full-before"
    assert full.story_system_prompt == "本次使用的规则"
    full.story_system_prompt = "内部更新后的规则"
    restored = from_runtime(full).story.system_prompt
    assert restored.active == 2 and restored.current == "内部更新后的规则"
    assert restored.options[2].text == "保留的备用规则"
    assert full.context_tokens == 6000
    assert full.lore_max_entries == 2
    assert full.local_models.reranker_prompt == "full-ranking"
    full.retrieval.channels.append("embedding")
    assert "embedding" not in doc.story.retrieval.channels


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
            return await client.patch(
                f"/api/settings/{scope}",
                json={
                    "changes": changes,
                    "edit_session": token,
                    "expected_revision": current["revisions"][scope] if revision is None else revision,
                },
            )

        response = await patch(
            "story",
            {
                "model.model": "new-full",
                "lore_max_entries": 9,
                "system_prompt.options.0.text": "new-story-prompt",
            },
        )
        assert response.status_code == 200, response.text
        saved = response.json()
        assert saved["shared"] == original["shared"]
        assert app.state.store.settings().story_system_prompt == "new-story-prompt"
        invalid = await patch("story", {"model.context_tokens": 2048})
        assert invalid.status_code == 400
        assert (await client.get("/api/settings")).json() == saved
        stale = await patch("story", {"model.model": "stale"}, revision=0)
        assert stale.status_code == 409
        undo = await client.post(
            f"/api/settings/edit-sessions/{token}/undo",
            json={"page": "story.model", "expected_revision": saved["revisions"]["story"]},
        )
        assert undo.status_code == 200, undo.text
        assert undo.json()["story"]["model"] == original["story"]["model"]
        assert undo.json()["story"]["system_prompt"]["options"][0]["text"] == "new-story-prompt"
        providers = deepcopy(original["shared"]["providers"])
        providers[0].update(name="changed-name", api_key="")
        assert (await patch("shared", {"providers": providers})).status_code == 200
        assert app.state.store.settings_document().shared.providers[0].api_key == "old-secret"
        providers[0]["api_key"] = "new-secret"
        result = await patch("shared", {"providers": providers})
        assert result.status_code == 200
        assert "new-secret" not in result.text
        undo = await client.post(
            f"/api/settings/edit-sessions/{token}/undo",
            json={"page": "shared.providers", "expected_revision": result.json()["revisions"]["shared"]},
        )
        assert undo.status_code == 200, undo.text
        assert app.state.store.settings_document().shared.providers[0].api_key == "old-secret"
        assert "old-secret" not in undo.text
        await client.delete(f"/api/settings/edit-sessions/{token}")
        assert (await patch("story", {"model.model": "expired"})).status_code == 409
        assert (await client.put("/api/settings", json={})).status_code == 405


async def test_other_window_changes_cannot_be_undone_and_capabilities_cannot_be_forged(tmp_path):
    app = create_app(tmp_path)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        a = (await client.post("/api/settings/edit-sessions", json={})).json()
        b = (await client.post("/api/settings/edit-sessions", json={})).json()
        result = await client.patch(
            "/api/settings/story",
            json={
                "expected_revision": 0,
                "edit_session": b["id"],
                "changes": {"model.model": "other-window"},
            },
        )
        assert result.status_code == 200
        undo = await client.post(
            f"/api/settings/edit-sessions/{a['id']}/undo",
            json={"page": "story.model", "expected_revision": 1},
        )
        assert undo.status_code == 409
        fake = await client.patch(
            "/api/settings/story",
            json={"expected_revision": 1, "changes": {"model.capability": "accepted"}},
        )
        assert fake.status_code == 400
        assert app.state.store.settings().model == "other-window"


def test_unsupported_version_is_rejected_without_touching_database(tmp_path):
    store = Store(tmp_path)
    with store.connection() as db:
        db.execute("UPDATE meta SET value='999' WHERE key='schema_version'")
    before = store.path.read_bytes()
    with pytest.raises(ValueError):
        Store(tmp_path)
    assert store.path.read_bytes() == before
