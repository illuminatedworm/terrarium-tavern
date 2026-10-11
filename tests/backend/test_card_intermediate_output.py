"""Public prose is distinct from reasoning and survives reload/log cleanup."""
import json

import httpx
import pytest

from tavern.character.card_editor import CardEditorService
from tavern.core.models import CardEditorRequest
from tavern.scripting.model_transport import ModelTransport
from tavern.storage.maintenance import cleanup
from tavern.storage.store import Store
from tests.backend.test_card_editor_alignment import Editor
from tests.backend.test_half_agent_alignment import blocks


def public_events(edits, rid):
    result, cursor = [], 0
    while True:
        page = edits.activity(rid, cursor)
        result.extend(page["events"])
        if not page["has_more"]:
            return result
        assert page["next_cursor"] > cursor
        cursor = page["next_cursor"]


@pytest.mark.parametrize("api", ["responses", "anthropic"])
async def test_native_intermediate_prose_remains_public_after_restart_cleanup(tmp_path, monkeypatch, api):
    editor = Editor(tmp_path, monkeypatch, api)
    first = editor.tool(1, "overview", {})
    final = blocks(api, "最终完成说明")
    if api == "responses":
        next(item for item in first if item["type"] == "message")["phase"] = "commentary"
        next(item for item in final if item["type"] == "message")["phase"] = "final_answer"
    try:
        run = await editor.run([first, final])
        events = public_events(editor.service.edits, run["id"])
        assert [e["payload"]["text"] for e in events if e["kind"] == "text"] == ["正在编辑", "最终完成说明"]
        serialized = json.dumps(events, ensure_ascii=False)
        assert "signed-opaque" not in serialized and "encrypted_content" not in serialized
        assert not any(e["kind"] in ("provider_response", "provider_request") for e in events)
        context = editor.service.edits.context(editor.workspace["id"])
        with editor.store.connection() as db:
            db.execute("UPDATE card_edit_runs SET created=0 WHERE id=?", (run["id"],))
        cleanup(editor.store)
        restored = CardEditorService(Store(tmp_path))
        assert public_events(restored.edits, run["id"]) == events
        assert restored.edits.context(editor.workspace["id"]) == context
        assert restored.edits.get(editor.workspace["id"])["document"] == editor.workspace["document"]
        await restored.close()
    finally:
        await editor.service.close()


async def test_chat_completions_stream_pagination_and_compaction_preserve_whole_prose(tmp_path, monkeypatch):
    editor = Editor(tmp_path, monkeypatch, "chat_completions")
    middle = "完整说明。" * 250 + "末尾不能丢失"
    calls = []

    async def factory(cfg, capture):
        def route(request):
            calls.append(request)
            first = len(calls) == 1
            text = middle if first else "最终说明"
            frames = [{"choices": [{"delta": {"content": char}, "finish_reason": None}]} for char in text]
            if first:
                frames.append({"choices": [{"delta": {"reasoning_content": "PRIVATE_REASONING",
                    "tool_calls": [{"index": 0, "id": "call", "type": "function", "function": {
                        "name": "overview", "arguments": "{}"}}]}, "finish_reason": None}]})
            frames.append({"choices": [{"delta": {}, "finish_reason": "tool_calls" if first else "stop"}]})
            return httpx.Response(200, content="".join("data: " + json.dumps(frame) + "\n\n" for frame in frames) + "data: [DONE]\n\n",
                                  headers={"content-type": "text/event-stream"})
        return ModelTransport(cfg, cfg.api_key, capture, http_client=httpx.AsyncClient(transport=httpx.MockTransport(route)))

    monkeypatch.setattr("tavern.character.card_editor.make_provider", factory)
    try:
        ws = editor.workspace
        run = editor.service.submit(ws["id"], CardEditorRequest(text="检查资料", operation_id="ordinary", expected_revision=0))
        await editor.service.tasks[run["id"]]
        assert editor.service.edits.run(run["id"])["status"] == "completed"
        assert editor.service.edits.activity(run["id"])["has_more"]
        events = public_events(editor.service.edits, run["id"])
        assert "".join(e["payload"]["text"] for e in events if e["kind"] == "text") == middle + "最终说明"
        assert "PRIVATE_REASONING" not in json.dumps(events)
        first_text = next(e["seq"] for e in events if e["kind"] == "text")
        with editor.store.connection() as db:
            db.execute("UPDATE card_edit_runs SET created=0 WHERE id=?", (run["id"],))
        cleanup(editor.store)
        compacted = public_events(editor.service.edits, run["id"])
        texts = [e for e in compacted if e["kind"] == "text"]
        assert len(texts) == 2
        assert texts[0]["seq"] == first_text and texts[0]["payload"]["text"] == middle
        assert texts[1]["payload"]["text"] == "最终说明"
    finally:
        await editor.service.close()
