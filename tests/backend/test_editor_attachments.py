"""Browser uploads remain readable, scoped, bounded and removable."""
import json

import pytest
from httpx import ASGITransport, AsyncClient

from tavern.application.api import create_app
from tavern.character.card_author import CardAuthorService
from tavern.character.editor_attachments import attachment_path
from tavern.core.models import CardAuthorRequest
from tavern.storage.backup import backup, restore
from tavern.storage.store import Store
from tests.backend.test_card_author import ToolLLM, import_fixture
from tests.backend.test_card_authoring import run
from tests.backend.test_editor_files import make_docx


async def test_upload_read_rollover_restore_and_remove(tmp_path):
    app = create_app(tmp_path / "source")
    editor = app.state.editor
    wid = editor.edits.create_conversation()["id"]
    try:
        async with AsyncClient(transport=ASGITransport(app), base_url="http://testserver") as client:
            endpoint = f"/api/card-author/conversations/{wid}/files"
            response = await client.post(endpoint, data={"expected_revision": 0}, files={"file": ("../../资料.md", "世界设定\n完整尾部".encode())})
            assert response.status_code == 200, response.text
            workspace = response.json()
            item = workspace["files"][0]
            assert item["name"] == "资料.md"
            path = attachment_path(editor.store, wid, item)
            assert path.read_text(encoding="utf-8").endswith("完整尾部")
            doc = make_docx(tmp_path / "资料.docx")
            response = await client.post(endpoint, data={"expected_revision": workspace["revision"]}, files={"file": (doc.name, doc.read_bytes())})
            assert response.status_code == 200, response.text
            second = response.json()["files"][1]
            card = import_fixture(editor.store)
            linked = editor.edits.attach(wid, card["id"], response.json()["revision"])
            editor.edits.remove_card(wid, linked["cards"][0]["ref"], linked["revision"])
            assert path.exists() and attachment_path(editor.store, wid, second).exists()
            provider = ToolLLM([("read_text_file", {"path": item["ref"]}),
                                ("new_context", {"content": "继续阅读资料"}),
                                ("read_document", {"path": second["ref"]})])
            result = await run(editor, wid, provider)
            assert result["status"] == "completed", result["error"]
            events = editor.edits.events(result["id"])
            results = [e["payload"] for e in events if e["kind"] == "tool_finished"]
            assert all(p["status"] == "completed" for p in results)
            assert "完整尾部" in results[0]["result"]
            assert "文档完整尾部" in results[-1]["result"]
            assert str(path) not in json.dumps(provider.call_log, ensure_ascii=False)
            assert item["ref"] in json.dumps(provider.call_log[-1], ensure_ascii=False)
            archive = tmp_path / "backup.zip"
            backup(editor.store, archive)
            restored = restore(archive, tmp_path / "restored")
            assert attachment_path(restored, wid, item).read_bytes() == path.read_bytes()
            workspace = editor.edits.conversation(wid)
            assert len(workspace["messages"][0]["attachments"]) == 2
            assert all(f["message_ref"] == workspace["messages"][0]["ref"] for f in workspace["files"])
            removed = await client.request("DELETE", endpoint + "/" + item["ref"], json={"expected_revision": workspace["revision"]})
            assert removed.status_code == 400
            assert path.exists()
            next_provider = ToolLLM([])
            await run(editor, wid, next_provider, "谢谢，接下来讨论别的事情")
            outbound = next_provider.call_log[0]
            assert sum("本条用户消息的附件" in m.get("content", "") for m in outbound) == 1
            assert not any(m.get("message_type") == "editor_files" for m in outbound)
            latest_user = [m for m in outbound if m["role"] == "user"][-1]
            assert "F1" not in latest_user["content"]
            workspace = editor.edits.conversation(wid)
            assert workspace["messages"][-2]["attachments"] == []
            response = await client.post(endpoint, data={"expected_revision": workspace["revision"]}, files={"file": ("待发送.txt", b"draft")})
            unsent = response.json()["files"][-1]
            removed = await client.request("DELETE", endpoint + "/" + unsent["ref"], json={"expected_revision": response.json()["revision"]})
            assert removed.status_code == 200
            assert not attachment_path(editor.store, wid, unsent).exists()
            workspace = editor.edits.conversation(wid)
            editor.edits.delete(wid, workspace["revision"])
            assert not attachment_path(editor.store, wid, second).exists()
    finally:
        await editor.close()
        await app.state.service.close()


async def test_submit_atomicity_cancel_resume_and_legacy_migration(tmp_path):
    from tavern.character.editor_attachments import attach_file
    editor = CardAuthorService(Store(tmp_path))
    wid = editor.edits.create_conversation()["id"]
    workspace = attach_file(editor.edits, wid, 0, "PS.txt", b"fixture")
    editor.provider_factory = lambda: ToolLLM([])
    with pytest.raises(ValueError, match="请输入"):
        editor.submit(wid, CardAuthorRequest(text="", expected_revision=workspace["revision"], operation_id="invalid"))
    assert editor.edits.conversation(wid)["files"][0]["message_ref"] is None
    request = CardAuthorRequest(text="读取附件", expected_revision=workspace["revision"], operation_id="first")
    first = editor.submit(wid, request)
    assert editor.submit(wid, request)["id"] == first["id"]
    await editor.cancel(first["id"])
    workspace = editor.edits.conversation(wid)
    assert workspace["files"][0]["message_ref"] == workspace["messages"][0]["ref"]
    provider = ToolLLM([("read_text_file", {"path": workspace["files"][0]["ref"]})])
    assert (await run(editor, wid, provider, "", resume=first["id"]))["status"] == "completed"
    assert len(editor.edits.messages(wid)[0]["attachments"]) == 1
    # Reproduce the previous conversation-scoped representation for a read file.
    with editor.store.connection() as db:
        workspace = editor.edits.load(db, wid)
        original_ref = workspace["files"][0].pop("message_ref")
        editor.edits.write(db, workspace, increment=False)
    reopened = CardAuthorService(Store(tmp_path))
    try:
        migrated = reopened.edits.conversation(wid)
        assert migrated["files"][0]["message_ref"] == original_ref
        assert migrated["messages"][0]["attachments"][0]["name"] == "PS.txt"
        assert len(list(tmp_path.glob("before-file-messages-*.sqlite3"))) == 1
        Store(tmp_path)
        assert len(list(tmp_path.glob("before-file-messages-*.sqlite3"))) == 1
    finally:
        await reopened.close()
        await editor.close()


async def test_rejected_uploads_leave_no_files(tmp_path, monkeypatch):
    app = create_app(tmp_path)
    editor = app.state.editor
    wid = editor.edits.create_conversation()["id"]
    try:
        async with AsyncClient(transport=ASGITransport(app), base_url="http://testserver") as client:
            endpoint = f"/api/card-author/conversations/{wid}/files"
            for filename, revision in [("x.exe", 0), ("x.txt", 9)]:
                response = await client.post(endpoint, data={"expected_revision": revision}, files={"file": (filename, b"abc")})
                assert response.status_code in (400, 409), response.text
            monkeypatch.setattr("tavern.character.editor_files.MAX_FILE_BYTES", 2)
            response = await client.post(endpoint, data={"expected_revision": 0}, files={"file": ("x.txt", b"abc")})
            assert response.status_code == 400
            assert not editor.edits.conversation(wid).get("files")
            assert not list((tmp_path / "blobs").rglob("F*"))
    finally:
        await editor.close()
        await app.state.service.close()
