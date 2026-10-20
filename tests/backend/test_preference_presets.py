"""Reusable preferences stay shared, revision checked, and separate from session text."""

from copy import deepcopy

import pytest
from httpx import ASGITransport, AsyncClient

from tavern.application.api import create_app
from tavern.core.settings import SettingsDocument, from_runtime, migrate_settings
from tavern.storage.store import Store


def test_existing_settings_default_to_empty_and_runtime_saves_keep_presets():
    raw = SettingsDocument().model_dump()
    del raw["shared"]["preference_presets"]
    migrated = migrate_settings(raw)
    assert migrated.shared.preference_presets == []
    assert migrated.revisions == raw["revisions"]
    current = migrated.model_dump()
    current["shared"]["preference_presets"] = [
        {"id": "slow", "name": "放缓节奏", "content": "  多用对白。\n保留行动空间。\n"}
    ]
    migrated = SettingsDocument.model_validate(current)
    for mode in ("full_agent", "half_agent"):
        runtime = migrated.runtime(mode)
        assert runtime.preference_presets == migrated.shared.preference_presets
        runtime.temperature = 0.7
        assert from_runtime(runtime).shared.preference_presets == migrated.shared.preference_presets
        runtime.preference_presets[0].content = "不同内容"
        assert migrated.shared.preference_presets[0].content == "  多用对白。\n保留行动空间。\n"


async def test_shared_presets_crud_revisions_persistence_and_page_undo(tmp_path):
    app = create_app(tmp_path)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        opened = (await client.post("/api/settings/edit-sessions", json={})).json()
        original = opened["settings"]
        page = next(page for page in original["pages"] if page["id"] == "shared.preference_presets")
        assert page["paths"] == ["preference_presets"]
        assert original["shared"]["preference_presets"] == []
        presets = [{"id": "slow", "name": "放缓节奏", "content": " 多用对白。\n保留行动空间。\n"}]

        async def patch(items, revision):
            return await client.patch(
                "/api/settings/shared",
                json={
                    "expected_revision": revision,
                    "edit_session": opened["id"],
                    "changes": {"preference_presets": items},
                },
            )

        added = await patch(presets, original["revisions"]["shared"])
        assert added.status_code == 200, added.text
        saved = added.json()
        assert saved["shared"]["preference_presets"] == presets
        assert saved["full_agent"] == original["full_agent"]
        assert saved["half_agent"] == original["half_agent"]
        assert saved["revisions"]["shared"] == original["revisions"]["shared"] + 1
        assert (
            Store(tmp_path).settings_document().shared.preference_presets[0].content == presets[0]["content"]
        )
        assert (await patch([], original["revisions"]["shared"])).status_code == 409
        presets[0].update(name="对白优先", content="多用对白推进剧情。")
        edited = await patch(presets, saved["revisions"]["shared"])
        assert edited.status_code == 200
        assert edited.json()["shared"]["preference_presets"] == presets
        deleted = await patch([], edited.json()["revisions"]["shared"])
        assert deleted.status_code == 200
        assert Store(tmp_path).settings_document().shared.preference_presets == []
        await patch(presets, deleted.json()["revisions"]["shared"])
        current = (await client.get("/api/settings")).json()
        undo = await client.post(
            f"/api/settings/edit-sessions/{opened['id']}/undo",
            json={
                "page": "shared.preference_presets",
                "expected_revision": current["revisions"]["shared"],
            },
        )
        assert undo.status_code == 200, undo.text
        assert undo.json()["shared"]["preference_presets"] == []
        assert Store(tmp_path).settings_document().shared.preference_presets == []


@pytest.mark.parametrize(
    "presets",
    [
        [{"id": "a", "name": " ", "content": "有效内容"}],
        [{"id": "a", "name": "有效名称", "content": "\n\t"}],
        [{"id": " ", "name": "有效名称", "content": "有效内容"}],
        [{"id": "a", "name": "a", "content": "a"}, {"id": "a", "name": "b", "content": "b"}],
    ],
)
async def test_invalid_presets_do_not_write_partial_settings(tmp_path, presets):
    app = create_app(tmp_path)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        before = (await client.get("/api/settings")).json()
        changes = {"preference_presets": deepcopy(presets), "log_retention_days": 30}
        response = await client.patch(
            "/api/settings/shared",
            json={
                "expected_revision": before["revisions"]["shared"],
                "changes": changes,
            },
        )
        assert response.status_code == 400
        assert (await client.get("/api/settings")).json() == before
