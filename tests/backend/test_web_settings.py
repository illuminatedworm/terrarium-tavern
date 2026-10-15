"""Web settings migration, secret handling and agent-level permissions."""
import json
import sqlite3

import httpx
import pytest

from tavern.application.api import create_app
from tavern.core.settings import SettingsDocument, migrate_settings
from tavern.storage.store import Store
from tavern.storage.transfer import ExportSelection, export_package, import_package, read_package


def test_legacy_web_settings_migrate_once_with_backup_and_preserve_off(tmp_path):
    store = Store(tmp_path)
    raw = store.settings_document().model_dump()
    raw["shared"].pop("web")
    raw["shared"]["advisor"]["web"] = {"enabled": False, "backend": "searxng", "endpoint": "http://127.0.0.1:9001", "timeout_seconds": 47}
    with store.connection() as db:
        db.execute("UPDATE meta SET value=? WHERE key='settings'", (json.dumps(raw),))
    reopened = Store(tmp_path)
    doc = reopened.settings_document()
    assert doc.shared.web.order == ["exa", "tavily", "searxng", "duckduckgo"]
    assert doc.shared.web.searxng.endpoint == "http://127.0.0.1:9001" and doc.shared.web.timeout_seconds == 47
    assert doc.shared.advisor.web.mode == "off" and doc.shared.card_editor_config.web.mode == "auto"
    assert migrate_settings(doc.model_dump()) == doc
    files = list(tmp_path.glob("settings-before-web-*.sqlite3"))
    assert len(files) == 1
    with sqlite3.connect(files[0]) as db:
        assert json.loads(db.execute("SELECT value FROM meta WHERE key='settings'").fetchone()[0]) == raw
    assert Store(tmp_path).settings_document() == doc
    assert len(list(tmp_path.glob("settings-before-web-*.sqlite3"))) == 1


async def test_web_key_redaction_preservation_explicit_clear_and_undo(tmp_path):
    app = create_app(tmp_path)
    store = app.state.store
    doc = store.settings_document()
    doc.shared.web.exa.api_key = "private-search-key"
    store.save_settings(doc)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        opened = (await client.post("/api/settings/edit-sessions", json={})).json()
        value = opened["settings"]
        assert "private-search-key" not in json.dumps(opened)
        assert value["shared"]["web"]["exa"]["has_api_key"]
        async def patch(changes):
            nonlocal value
            response = await client.patch("/api/settings/shared", json={"expected_revision": value["revisions"]["shared"],
                "edit_session": opened["id"], "changes": changes})
            assert response.status_code == 200, response.text
            value = response.json()
            assert "private-search-key" not in response.text
            return value
        shared = value["shared"]["web"]
        shared["exa"]["auth_mode"] = "api_key"
        await patch({"web": shared})
        assert store.settings_document().shared.web.exa.api_key == "private-search-key"
        await patch({"web.exa.api_key": ""})
        assert not value["shared"]["web"]["exa"]["has_api_key"]
        assert not store.settings_document().shared.web.exa.api_key
        response = await client.post(f"/api/settings/edit-sessions/{opened['id']}/undo", json={"page": "shared.web", "expected_revision": value["revisions"]["shared"]})
        assert response.status_code == 200, response.text
        assert "private-search-key" not in response.text
        assert store.settings_document().shared.web.exa.api_key == "private-search-key"
        pages = {p["id"]: p for p in response.json()["pages"]}
        assert "card_editor_config" in pages["shared.editor"]["paths"]


@pytest.mark.parametrize("include", [False, True])
def test_search_keys_export_only_when_requested_and_import_preserves_local_keys(tmp_path, include):
    source, target = Store(tmp_path / "source"), Store(tmp_path / "target")
    for store, key in ((source, "source-search-secret"), (target, "target-search-secret")):
        doc = store.settings_document()
        doc.shared.web.exa.api_key = doc.shared.web.tavily.api_key = key
        store.save_settings(doc)
    package, blobs = read_package(export_package(source, ExportSelection(settings=True, include_api_keys=include)), target)
    assert ("source-search-secret" in json.dumps(package)) == include
    import_package(target, package, blobs, restore_settings=True, expected_revisions=target.settings_document().revisions)
    for name in ("exa", "tavily"):
        assert getattr(target.settings_document().shared.web, name).api_key == ("source-search-secret" if include else "target-search-secret")


def test_invalid_order_and_credential_bearing_endpoint_are_rejected():
    for web in ({"order": ["exa", "exa", "tavily", "duckduckgo"]}, {"searxng": {"endpoint": "https://user:secret@example.org"}}):
        with pytest.raises(ValueError):
            SettingsDocument(shared={"web": web})
