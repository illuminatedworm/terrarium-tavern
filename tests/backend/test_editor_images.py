"""Image attachments remain message-owned through requests, recovery and archival."""

import asyncio
import base64
import copy
import json
from io import BytesIO

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from PIL import Image

from tavern.application.api import create_app
from tavern.character.card_author import CardAuthorService
from tavern.character.editor_attachments import attach_file, attachment_path, detach_file
from tavern.character.editor_images import IMAGE_REFS, image_metadata, materialize_images
from tavern.character.editor_tools import EditorTools
from tavern.core.models import CardAuthorRequest
from tavern.scripting.model_transport import ModelTransport
from tavern.storage.backup import backup, restore
from tavern.storage.store import Store
from tests.backend.test_card_author import ToolLLM
from tests.backend.test_card_author_alignment import Editor
from tests.backend.test_card_authoring import run
from tests.backend.test_editor_reference_prefix import execute
from tests.backend.test_half_agent_alignment import blocks
from tests.backend.test_image_protocols import assert_images, response


def image_bytes(format="PNG", *, animated=False, size=(24, 16)):
    output = BytesIO()
    image = Image.new("RGB", size, (40, 90, 150))
    kwargs = (
        {"save_all": True, "append_images": [Image.new("RGB", size, (90, 40, 10))], "duration": 80}
        if animated
        else {}
    )
    image.save(output, format=format, **kwargs)
    return output.getvalue()


@pytest.mark.parametrize(
    "extension,format,mime",
    [
        (".png", "PNG", "image/png"),
        (".jpg", "JPEG", "image/jpeg"),
        (".jpeg", "JPEG", "image/jpeg"),
        (".webp", "WEBP", "image/webp"),
    ],
)
def test_original_bytes_metadata_and_pending_removal(tmp_path, extension, format, mime):
    editor = CardAuthorService(Store(tmp_path))
    wid = editor.edits.create_conversation()["id"]
    data = image_bytes(format)
    workspace = attach_file(editor.edits, wid, 0, "../参考" + extension.upper(), data)
    file = workspace["files"][0]
    assert {key: file[key] for key in ("mime_type", "width", "height")} == {
        "mime_type": mime,
        "width": 24,
        "height": 16,
    }
    assert file["message_ref"] is None
    assert attachment_path(editor.store, wid, file).read_bytes() == data
    assert detach_file(editor.edits, wid, workspace["revision"], file["ref"])["files"] == []
    assert not attachment_path(editor.store, wid, file).exists()


@pytest.mark.parametrize(
    "filename,data,error",
    [
        ("fake.png", b"not an image", "损坏"),
        ("fake.png", image_bytes("JPEG"), "扩展名"),
        ("moving.png", image_bytes(animated=True), "动图"),
    ],
)
def test_invalid_image_upload_is_atomic(tmp_path, filename, data, error):
    editor = CardAuthorService(Store(tmp_path))
    wid = editor.edits.create_conversation()["id"]
    with pytest.raises(ValueError, match=error):
        attach_file(editor.edits, wid, 0, filename, data)
    assert not editor.edits.conversation(wid).get("files")
    assert not list((tmp_path / "blobs").rglob("F*"))


def test_decoder_pixel_limit_is_an_explicit_error(monkeypatch):
    data = image_bytes()
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 100)
    with pytest.raises(ValueError, match="像素过多"):
        image_metadata(".png", data)


async def test_preview_stats_and_upload_size_are_truthful(tmp_path, monkeypatch):
    app = create_app(tmp_path)
    editor = app.state.editor
    wid = editor.edits.create_conversation()["id"]
    base = f"/api/card-author/conversations/{wid}"
    data = image_bytes()
    encoded = base64.b64encode(data).decode()
    try:
        async with AsyncClient(transport=ASGITransport(app), base_url="http://testserver") as client:
            uploaded = await client.post(
                base + "/files",
                data={"expected_revision": 0},
                files={"file": ("参考.png", data, "text/plain")},
            )
            assert uploaded.status_code == 200, uploaded.text
            assert uploaded.json()["files"][0]["mime_type"] == "image/png"
            preview = await client.post(base + "/preview", json={"text": "参考图片"})
            assert preview.status_code == 200, preview.text
            body = preview.json()
            assert body["image_count"] == 1
            item = next(m for m in body["messages"] if m.get("attachments"))
            assert item["attachments"][0]["width"] == 24
            assert encoded not in preview.text and str(tmp_path) not in preview.text
            assert "实际请求发送原图" in body["note"]
            stats = (await client.post(base + "/stats", json={"text": "参考图片"})).json()
            assert stats["image_count"] == 1
            # Only a real user message binds pending files; previews stay read-only.
            assert editor.edits.conversation(wid)["files"][0]["message_ref"] is None
            monkeypatch.setattr("tavern.character.editor_files.MAX_FILE_BYTES", len(data) - 1)
            rejected = await client.post(
                base + "/files",
                data={"expected_revision": uploaded.json()["revision"]},
                files={"file": ("另一张.png", data)},
            )
            assert rejected.status_code == 400
            assert len(editor.edits.conversation(wid)["files"]) == 1
    finally:
        await editor.close()
        await app.state.service.close()


@pytest.mark.parametrize("api", ["responses", "anthropic"])
async def test_images_on_wire_survive_tools_restart_rollover_and_history_read(tmp_path, monkeypatch, api):
    editor = Editor(tmp_path, monkeypatch, api)
    edits, wid = editor.service.edits, editor.workspace["id"]
    png, jpeg = image_bytes(), image_bytes("JPEG")
    expected = [
        ("image/png", base64.b64encode(png).decode()),
        ("image/jpeg", base64.b64encode(jpeg).decode()),
    ]
    try:
        workspace = edits.conversation(wid)
        for name, data in [("参考.png", png), ("说明.md", "完整文档尾部".encode()), ("参考.jpg", jpeg)]:
            workspace = attach_file(edits, wid, workspace["revision"], name, data)
        image1, document, image2 = workspace["files"]
        result = await execute(
            editor, [editor.tool(1, "read_text_file", {"path": document["ref"]}), blocks(api, "已理解图片")]
        )
        for request in editor.requests:
            assert_images(json.loads(request.content), api, expected)
        first_message = edits.messages(wid)[0]
        assert len(first_message["attachments"]) == 3
        assert all(f["message_ref"] == first_message["ref"] for f in edits.conversation(wid)["files"])
        raw_events = json.dumps(edits.events(result["id"]), ensure_ascii=False)
        saved = json.dumps(edits.context(wid), ensure_ascii=False)
        assert expected[0][1] not in raw_events + saved
        assert "图片二进制已省略" in raw_events
        assert "完整文档尾部" in raw_events
        assert str(tmp_path) not in json.dumps([json.loads(r.content) for r in editor.requests])
        with pytest.raises(ValueError, match="已发送附件"):
            detach_file(edits, wid, edits.conversation(wid)["revision"], image1["ref"])

        # Restart, then change model: active user images rebuild from their
        # attachment ownership, not an old provider's encoded request.
        await editor.service.close()
        editor.service = CardAuthorService(Store(tmp_path))
        edits = editor.service.edits
        settings = editor.service.store.settings_document()
        settings.shared.card_author.model = "second-image-model"
        editor.service.store.save_settings(settings)
        with editor.service.store.connection() as db:
            db.execute(
                "UPDATE card_author_conversations SET model_selection=? WHERE id=?",
                (json.dumps({"provider_id": "default", "model": "second-image-model"}), wid),
            )
        await execute(editor, [blocks(api, "重启后继续")], "继续讨论")
        assert json.loads(editor.requests[-1].content)["model"] == "second-image-model"
        assert_images(json.loads(editor.requests[-1].content), api, expected)
        assert edits.messages(wid)[-2]["attachments"] == []
        await execute(editor, [blocks(api, "另一个回合")])
        start = len(editor.requests)
        await execute(
            editor,
            [
                editor.tool(2, "new_context", {"content": "回读最早图片核对"}),
                editor.tool(3, "read_history", {"ref": first_message["ref"]}),
                blocks(api, "已重新看到原图"),
            ],
        )
        assert_images(json.loads(editor.requests[start + 1].content), api, [])
        rows = assert_images(json.loads(editor.requests[start + 2].content), api, expected)
        recalled = next(
            i for i, m in enumerate(rows) if "由 read_history 恢复" in json.dumps(m, ensure_ascii=False)
        )
        if api == "responses":
            assert rows[recalled - 1]["type"] == "function_call_output"
        else:
            assert rows[recalled - 1]["content"][0]["type"] == "tool_result"
        assert sum(len(m.get(IMAGE_REFS, [])) for m in edits.context(wid)["messages"]) == 2

        settings = editor.service.store.settings_document()
        settings.shared.card_author.model = "third-image-model"
        editor.service.store.save_settings(settings)
        with editor.service.store.connection() as db:
            db.execute(
                "UPDATE card_author_conversations SET model_selection=? WHERE id=?",
                (json.dumps({"provider_id": "default", "model": "third-image-model"}), wid),
            )
        await execute(editor, [blocks(api, "切换模型后继续核对原图")])
        assert json.loads(editor.requests[-1].content)["model"] == "third-image-model"
        assert_images(json.loads(editor.requests[-1].content), api, expected)

        archive = tmp_path / "backup.zip"
        backup(editor.service.store, archive)
        restored = restore(archive, tmp_path / "restored")
        assert attachment_path(restored, wid, image1).read_bytes() == png
        assert attachment_path(restored, wid, image2).read_bytes() == jpeg
        edits.delete(wid, edits.conversation(wid)["revision"])
        assert not attachment_path(editor.service.store, wid, image1).exists()
        assert not attachment_path(editor.service.store, wid, image2).exists()
    finally:
        await editor.service.close()


async def test_cancel_missing_corrupt_and_resume_do_not_drop_or_rebind_images(tmp_path):
    editor = CardAuthorService(Store(tmp_path))
    wid = editor.edits.create_conversation()["id"]
    data = image_bytes()
    workspace = attach_file(editor.edits, wid, 0, "参考.png", data)
    file = workspace["files"][0]
    editor.provider_factory = lambda: ToolLLM([])
    request = CardAuthorRequest(text="读图", expected_revision=workspace["revision"], operation_id="first")
    first = editor.submit(wid, request)
    assert editor.submit(wid, request)["id"] == first["id"]
    await editor.cancel(first["id"])
    path = attachment_path(editor.store, wid, file)
    path.unlink()
    try:
        provider = ToolLLM([])
        missing = await run(editor, wid, provider, "", resume=first["id"])
        assert missing["status"] == "error" and "无法读取" in missing["error"]
        assert provider.call_count == 0
        path.write_bytes(b"invalid")
        corrupt = await run(editor, wid, provider, "", resume=missing["id"])
        assert corrupt["status"] == "error" and "大小已改变" in corrupt["error"]
        path.write_bytes(data)
        restored = await run(editor, wid, provider, "", resume=corrupt["id"])
        assert restored["status"] == "completed", restored["error"]
        assert len(editor.edits.messages(wid)) == 2
        assert editor.edits.messages(wid)[0]["attachments"][0]["ref"] == file["ref"]
        assert len(provider.call_log[0][1]["content"]) >= 1
        other = editor.edits.create_conversation()["id"]
        saved = editor.edits.context(wid)["messages"]
        with pytest.raises(ValueError, match="不属于当前"):
            materialize_images(saved, editor.store, other, [])
    finally:
        await editor.close()


async def test_attached_image_becomes_png_portrait_of_saved_card(tmp_path):
    store = Store(tmp_path)
    editor = CardAuthorService(store)
    wid = editor.edits.create_conversation()["id"]
    attach_file(editor.edits, wid, 0, "封面.jpg", image_bytes("JPEG"))
    provider = ToolLLM(
        [
            (
                "create_draft",
                {
                    "name": "雾港",
                    "confirmation_ref": "H1",
                    "direct_instruction": True,
                    "fields": {"description": "港口小镇", "first_mes": "雾笛响起。"},
                },
            ),
            ("set_portrait", {"card_ref": "C2", "file_ref": "F1"}),
        ]
    )
    try:
        result = await run(editor, wid, provider, "直接做一张雾港的卡，用这张图当头像")
        assert result["status"] == "completed", result["error"]
        workspace = editor.edits.conversation(wid)
        source = workspace["cards"][0]["source"]
        assert source["container"] == "png"
        assert workspace["cards"][0]["portrait_changed"]
        async with AsyncClient(
            transport=ASGITransport(app=create_app(tmp_path)), base_url="http://testserver"
        ) as client:
            served = await client.get(f"/api/card-author/conversations/{wid}/portraits/{source['original']}")
            assert served.headers["content-type"] == "image/png"
            assert (
                await client.get(f"/api/card-author/conversations/{wid}/portraits/{'0' * 64}")
            ).status_code == 404
            removed = editor.edits.set_portrait(wid, "C2", None, workspace["revision"])
            assert removed["cards"][0]["source"]["container"] == "json"
            restored = editor.edits.undo_card(wid, "C2", removed["revision"])
            saved = editor.edits.save_card(wid, "C2", restored["revision"], "create", "portrait-save")
            assert saved["container"] == "png"
            assert not editor.edits.conversation(wid)["cards"][0]["portrait_changed"]
            exported = await client.get(f"/api/cards/{saved['id']}/export")
        with Image.open(BytesIO(exported.content)) as image:
            assert image.format == "PNG"
        assert (
            store.import_card(exported.content, "雾港.png")["document"]["data"]["first_mes"] == "雾笛响起。"
        )

        # A portrait-only agent turn must add its own undo step instead of undoing
        # an earlier text edit. The library stays unchanged until explicit save.
        workspace = editor.edits.conversation(wid)
        document = copy.deepcopy(workspace["cards"][0]["document"])
        document["data"]["description"] = "保留之前的人物设定修改"
        editor.edits.update_card(wid, "C2", document, workspace["revision"])
        result = await run(
            editor, wid, ToolLLM([("set_portrait", {"card_ref": "C2", "file_ref": None})]), "只移除头像"
        )
        assert result["status"] == "completed", result["error"]
        workspace = editor.edits.conversation(wid)
        assert workspace["cards"][0]["portrait_changed"] and workspace["cards"][0]["can_undo"]
        assert store.card(saved["id"])["original"] == source["original"]
        restored = editor.edits.undo_card(wid, "C2", workspace["revision"])
        assert restored["cards"][0]["document"] == document
        assert restored["cards"][0]["source"]["original"] == source["original"]
        assert not restored["cards"][0]["portrait_changed"]
    finally:
        await editor.close()


async def test_reading_image_as_text_returns_guidance_and_preserves_original(tmp_path):
    editor = CardAuthorService(Store(tmp_path))
    wid = editor.edits.create_conversation()["id"]
    workspace = attach_file(editor.edits, wid, 0, "参考.png", image_bytes())
    file = copy.deepcopy(workspace["files"][0])
    provider = ToolLLM([("read_text_file", {"path": file["ref"]})])
    try:
        result = await run(editor, wid, provider, "参考图片")
        assert result["status"] == "completed"
        event = next(e for e in editor.edits.events(result["id"]) if e["kind"] == "tool_finished")
        assert event["payload"]["status"] == "error"
        assert "随所属用户消息直接提供" in event["payload"]["result"]
    finally:
        await editor.close()


async def test_provider_rejection_preserves_images_for_explicit_retry(tmp_path, monkeypatch):
    editor = Editor(tmp_path, monkeypatch, "responses")
    wid = editor.workspace["id"]
    data = image_bytes()
    expected = [("image/png", base64.b64encode(data).decode())]
    requests = []
    reject = True

    def route(request):
        requests.append(json.loads(request.content))
        return (
            httpx.Response(400, json={"error": {"message": "This model does not support image inputs"}})
            if reject
            else response("responses", True)
        )

    async def factory(cfg, capture):
        return ModelTransport(
            cfg, cfg.api_key, capture, http_client=httpx.AsyncClient(transport=httpx.MockTransport(route))
        )

    monkeypatch.setattr("tavern.character.card_author.make_provider", factory)
    workspace = attach_file(editor.service.edits, wid, editor.workspace["revision"], "参考.png", data)
    try:
        first = editor.service.submit(
            wid, CardAuthorRequest(text="读图", expected_revision=workspace["revision"], operation_id="first")
        )
        await editor.service.tasks[first["id"]]
        failed = editor.service.edits.run(first["id"])
        assert failed["status"] == "error" and "确认所选模型支持图片输入" in failed["error"]
        assert editor.service.edits.progress(wid)
        assert_images(requests[0], "responses", expected)
        reject = False
        workspace = editor.service.edits.conversation(wid)
        resumed = editor.service.submit(
            wid,
            CardAuthorRequest(
                text="",
                expected_revision=workspace["revision"],
                operation_id="retry",
                resume_run_id=first["id"],
            ),
        )
        await editor.service.tasks[resumed["id"]]
        assert editor.service.edits.run(resumed["id"])["status"] == "completed"
        assert_images(requests[-1], "responses", expected)
        assert len(editor.service.edits.messages(wid)) == 2
    finally:
        await editor.service.close()


async def archived_image_editor(tmp_path, monkeypatch):
    editor = Editor(tmp_path, monkeypatch, "responses")
    wid = editor.workspace["id"]
    workspace = attach_file(
        editor.service.edits, wid, editor.workspace["revision"], "旧图.png", image_bytes()
    )
    workspace = attach_file(editor.service.edits, wid, workspace["revision"], "资料.md", b"full document")
    await execute(editor, [blocks("responses", "已看过旧图")], "old-image-message")
    source = editor.service.edits.messages(wid)[0]["ref"]
    await execute(editor, [blocks("responses", "第二轮")])
    await execute(editor, [blocks("responses", "第三轮")])
    await execute(
        editor,
        [
            editor.tool(1, "new_context", {"content": "完成旧图讨论，开始新窗口"}),
            blocks("responses", "已换窗"),
        ],
    )
    assert_images(json.loads(editor.requests[-1].content), "responses", [])
    return editor, source, workspace["files"][1]["ref"]


async def test_switching_model_does_not_revive_unrecalled_archived_images(tmp_path, monkeypatch):
    editor, source, _ = await archived_image_editor(tmp_path, monkeypatch)
    wid = editor.workspace["id"]
    try:
        with editor.store.connection() as db:
            db.execute(
                "UPDATE card_author_conversations SET model_selection=? WHERE id=?",
                (json.dumps({"provider_id": "default", "model": "another-model"}), wid),
            )
        await execute(editor, [blocks("responses", "继续新窗口")])
        body = json.loads(editor.requests[-1].content)
        assert body["model"] == "another-model"
        assert_images(body, "responses", [])
        assert "old-image-message" not in json.dumps(body, ensure_ascii=False)
        assert source in editor.service.edits.context(wid)["seen"]
    finally:
        await editor.service.close()


async def test_interrupted_history_batch_restores_images_after_all_tool_results(tmp_path, monkeypatch):
    editor, source, document = await archived_image_editor(tmp_path, monkeypatch)
    wid = editor.workspace["id"]
    waiting = asyncio.Event()
    read_document = EditorTools.read_text_file

    async def interrupted_read(self, args):
        waiting.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(EditorTools, "read_text_file", interrupted_read)
    batch = editor.tool(2, "read_history", {"ref": source})
    batch.insert(
        2,
        {
            "type": "function_call",
            "id": "fc3",
            "call_id": "tool3",
            "name": "read_text_file",
            "arguments": json.dumps({"path": document}),
        },
    )
    editor.plans.append([batch])
    workspace = editor.service.edits.conversation(wid)
    first = editor.service.submit(
        wid,
        CardAuthorRequest(
            text="回看旧图和资料", expected_revision=workspace["revision"], operation_id="batch"
        ),
    )
    try:
        await asyncio.wait_for(waiting.wait(), timeout=10)
        await editor.service.cancel(first["id"])
        progress = editor.service.edits.progress(wid)
        assert progress["pending_calls"]["image_history"] == [source]
        assert not any(m.get(IMAGE_REFS) for m in progress["context"]["messages"])
        monkeypatch.setattr(EditorTools, "read_text_file", read_document)
        editor.plans.append([blocks("responses", "已恢复图片和资料")])
        workspace = editor.service.edits.conversation(wid)
        resumed = editor.service.submit(
            wid,
            CardAuthorRequest(
                text="",
                expected_revision=workspace["revision"],
                operation_id="batch-resume",
                resume_run_id=first["id"],
            ),
        )
        await editor.service.tasks[resumed["id"]]
        assert editor.service.edits.run(resumed["id"])["status"] == "completed"
        expected = [("image/png", base64.b64encode(image_bytes()).decode())]
        rows = assert_images(json.loads(editor.requests[-1].content), "responses", expected)
        index = next(
            i for i, m in enumerate(rows) if "由 read_history 恢复" in json.dumps(m, ensure_ascii=False)
        )
        assert [row.get("type") for row in rows[index - 2 : index]] == [
            "function_call_output",
            "function_call_output",
        ]
        assert sum(m.get("type") == "function_call_output" and m["call_id"] == "tool2" for m in rows) == 1
    finally:
        await editor.service.close()
