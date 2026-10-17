"""Shared transport selection reaches the wire without changing response contracts."""
import json
import zipfile

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from tavern.application.api import create_app
from tavern.core.provider import make_provider
from tavern.core.settings import SettingsDocument
from tavern.storage.request_debug import RequestDebugLog
from tavern.storage.store import Store
from tests.backend.chatgpt_fixtures import IdentityFixture, seed_account, sse
from tests.backend.test_chatgpt_provider import configuration
from tests.backend.test_unified_cache import config, provider, reply

APIS = ["chat_completions", "responses", "anthropic", "gemini"]
MESSAGES = [{"role": "user", "content": "完整原文，包含末尾"}]
TOOLS = [{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}]


def events(api):
    payload = reply(api)
    if api == "chat_completions":
        return [{"choices": [{"delta": {"content": "OK"}, "finish_reason": "stop"}]},
                {"usage": payload["usage"]}]
    if api == "responses":
        return [{"type": "response.output_text.delta", "delta": "OK"},
                {"type": "response.completed", "response": payload}]
    if api == "anthropic":
        return [{"type": "message_start", "message": {"usage": payload["usage"]}},
                {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": "OK"}},
                {"type": "message_delta", "delta": {"stop_reason": "end_turn"}},
                {"type": "message_stop"}]
    return [payload]


@pytest.mark.parametrize("api", APIS)
@pytest.mark.parametrize("mode,entry,streamed", [
    ("stream", "complete", True), ("non_stream", "chat", False),
    ("auto", "complete", False), ("auto", "chat", True),
])
async def test_mode_controls_wire_and_preserves_content_tools_and_usage(api, mode, entry, streamed):
    cfg = config(api, model_request_mode=mode)
    sent = []

    def route(request):
        body = json.loads(request.content)
        sent.append(body)
        assert "完整原文，包含末尾" in str(body)
        assert body["tools"]
        if api == "gemini":
            assert (":streamGenerateContent" in request.url.path) is streamed
            assert (request.url.params.get("alt") == "sse") is streamed
        else:
            assert bool(body.get("stream")) is streamed
        return sse(events=events(api)) if streamed else httpx.Response(200, json=reply(api))

    async with provider(cfg, route) as p:
        if entry == "complete":
            response = await p.chat_complete(MESSAGES, tools=TOOLS)
            assert response.content == "OK" and response.finish_reason == "stop"
        else:
            assert [chunk async for chunk in p.chat(MESSAGES, tools=TOOLS)] == ["OK"]
        assert len(p.usage_history) == 1
        assert p.last_usage["prompt_tokens"] == 10
        assert p.last_usage["cached_tokens"] == 8
    assert len(sent) == 1


async def test_streamed_complete_retains_fragmented_tool_calls_and_rejects_disconnect():
    tool_events = [
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call-1",
            "function": {"name": "lookup", "arguments": '{"q":'}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0,
            "function": {"arguments": '"原文"}'}}]}, "finish_reason": "tool_calls"}]},
        {"usage": reply("chat_completions")["usage"]},
    ]
    cfg = config(model_request_mode="stream")
    async with provider(cfg, lambda _: sse(events=tool_events)) as p:
        result = await p.chat_complete(MESSAGES, tools=TOOLS)
        assert result.finish_reason == "tool_calls"
        assert p.last_tool_calls[0].name == "lookup"
        assert json.loads(p.last_tool_calls[0].arguments) == {"q": "原文"}
    async with provider(cfg, lambda _: sse(events=tool_events[:1])) as p:
        with pytest.raises(ValueError, match="提前结束"):
            await p.chat_complete(MESSAGES, tools=TOOLS)
        assert not p.usage_history and not p.last_tool_calls


@pytest.mark.parametrize("entry", ["complete", "chat", "preview"])
async def test_chatgpt_subscription_keeps_required_streaming(entry, tmp_path):
    service = IdentityFixture().service(tmp_path)
    account_id, account = seed_account(service)
    cfg = configuration(service, account_id, account)
    cfg.model_request_mode = "non_stream"
    seen = []

    def route(request):
        seen.append(json.loads(request.content))
        return sse()

    service.http_transport = httpx.MockTransport(route)
    p = await make_provider(cfg)
    try:
        if entry == "complete":
            assert (await p.chat_complete(MESSAGES)).content == "完成"
        elif entry == "preview":
            chunks = []
            assert (await p.chat_complete_with_preview(MESSAGES, chunks.append)).content == "完成"
            assert chunks == ["完成"]
        else:
            assert "".join([chunk async for chunk in p.chat(MESSAGES)]) == "完成"
        assert seen[0]["stream"] is True
    finally:
        await p.close()


async def test_selection_persists_projects_to_all_roles_and_undo_restores_default(tmp_path):
    app = create_app(tmp_path)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        session = (await client.post("/api/settings/edit-sessions", json={})).json()
        before = (await client.get("/api/settings")).json()
        assert before["shared"]["model_request_mode"] == "auto"
        changed = await client.patch("/api/settings/shared", json={
            "expected_revision": before["revisions"]["shared"], "edit_session": session["id"],
            "changes": {"model_request_mode": "stream"}})
        assert changed.status_code == 200, changed.text
        doc = Store(tmp_path).settings_document()
        for mode in ("full_agent", "half_agent"):
            runtime = doc.runtime(mode)
            assert runtime.model_request_mode == "stream"
            for role in ("foreground", "background", "card_editor", "full_agent"):
                assert runtime.for_role(role).model_request_mode == "stream"
        undone = await client.post(f"/api/settings/edit-sessions/{session['id']}/undo", json={
            "page": "shared.network", "expected_revision": changed.json()["revisions"]["shared"]})
        assert undone.status_code == 200, undone.text
        assert undone.json()["shared"]["model_request_mode"] == "auto"
        assert runtime.model_request_mode == "stream"  # An active operation retains its snapshot.
        invalid = await client.patch("/api/settings/shared", json={
            "expected_revision": undone.json()["revisions"]["shared"],
            "changes": {"model_request_mode": "invalid"}})
        assert invalid.status_code == 400
    legacy = SettingsDocument().model_dump()
    del legacy["shared"]["model_request_mode"]
    assert SettingsDocument.model_validate(legacy).shared.model_request_mode == "auto"


@pytest.mark.parametrize("api", APIS)
async def test_complete_preview_keeps_full_response_and_usage(api):
    cfg = config(api, model_request_mode="stream")
    chunks = []
    async with provider(cfg, lambda _: sse(events=events(api))) as p:
        response = await p.chat_complete_with_preview(MESSAGES, chunks.append, tools=TOOLS)
        assert chunks == ["OK"]
        assert response.content == "OK" and response.finish_reason == "stop"
        assert response.usage["cached_tokens"] == 8
        assert len(p.usage_history) == 1


async def test_complete_preview_parses_multiline_sse_keepalive_and_terminal_eof():
    cfg = config(model_request_mode="stream")
    wire = (': keepalive\r\n\r\nevent: message\r\nid: 4\r\n'
            'data: {"choices": [\r\ndata: {"delta": {"content": "OK"}}]}\r\n\r\n'
            'data: {"choices": [{"delta": {}, "finish_reason": "stop"}]}')
    chunks = []
    async with provider(cfg, lambda _: httpx.Response(200, text=wire)) as p:
        assert (await p.chat_complete_with_preview(MESSAGES, chunks.append)).content == "OK"
        assert chunks == ["OK"]


async def test_named_sse_error_after_terminal_is_not_success():
    cfg = config(model_request_mode="stream")
    wire = ('data: {"choices": [{"delta": {"content": "OK"}, "finish_reason": "stop"}]}\n\n'
            'event: error\ndata: {"message": "late error"}\n\n')
    async with provider(cfg, lambda _: httpx.Response(200, text=wire)) as p:
        with pytest.raises(ValueError, match="返回错误"):
            await p.chat_complete_with_preview(MESSAGES, lambda _: None)
        assert not p.usage_history


async def test_non_stream_mode_returns_complete_response_without_incremental_callbacks():
    cfg = config(model_request_mode="non_stream")
    chunks = []
    async with provider(cfg, lambda _: httpx.Response(200, json=reply("chat_completions"))) as p:
        result = await p.chat_complete_with_preview(MESSAGES, chunks.append)
        assert result.content == "OK"
        assert chunks == []


async def test_failed_request_debug_settings_download_privacy_and_retention(tmp_path, monkeypatch):
    app = create_app(tmp_path)
    store = app.state.store
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        editor = (await client.post("/api/settings/edit-sessions", json={})).json()
        before = editor["settings"]
        assert before["shared"]["debug_failed_requests"] is False
        changed = await client.patch("/api/settings/shared", json={
            "expected_revision": before["revisions"]["shared"], "edit_session": editor["id"],
            "changes": {"debug_failed_requests": True}})
        assert changed.status_code == 200, changed.text
        for mode in ("full_agent", "half_agent"):
            runtime = store.settings(mode)
            for role in ("foreground", "background", "card_editor", "full_agent"):
                assert runtime.for_role(role).debug_failed_requests
                assert runtime.for_role(role)._data_root == tmp_path
        cfg = config(debug_failed_requests=True, log_max_mib=1)
        cfg._data_root = tmp_path
        cfg.api_key = "private-key-for-failure-debug"
        body = (b"gateway failure " + cfg.api_key.encode() + b"\n") * 10000
        requests = []

        def route(request):
            requests.append(request)
            if len(requests) == 2:
                return httpx.Response(200, json=reply("chat_completions"))
            return httpx.Response(502, content=body, headers={"set-cookie": "private-cookie", "x-echo": cfg.api_key})

        log = RequestDebugLog(tmp_path, cfg)
        async with provider(cfg, route) as transport:
            with pytest.raises(httpx.HTTPStatusError):
                await transport.chat_complete(MESSAGES)
            records = log.records()
            assert len(records) == 1
            record = records[0]
            assert record["response"]["status_code"] == 502
            assert record["request"]["headers"]["authorization"] == "[REDACTED]"
            assert record["response"]["headers"]["set-cookie"] == "[REDACTED]"
            assert record["response"]["body"]["truncated"]
            assert record["response"]["body"]["received_bytes"] == len(body)
            assert record["response"]["body"]["limit_bytes"] == 256 * 1024
            assert (await transport.chat_complete(MESSAGES)).content == "OK"
            cfg.debug_failed_requests = False
            with pytest.raises(httpx.HTTPStatusError):
                await transport.chat_complete(MESSAGES)
            assert len(log.records()) == 1  # Successful and disabled requests never persist.
        listing = (await client.get("/api/debug/failed-requests")).json()
        assert listing["total"] == 1 and listing["items"][0]["id"] == record["id"]
        download = await client.get(f"/api/debug/failed-requests/{record['id']}")
        assert download.status_code == 200 and download.content == log.path(record["id"]).read_bytes()
        assert cfg.api_key.encode() not in download.content and b"private-cookie" not in download.content
        with zipfile.ZipFile(log.path(record["id"])) as archive:
            assert archive.read("request.bin") == requests[0].content
            assert archive.read("response.bin") == body[:256 * 1024].replace(cfg.api_key.encode(), b"[REDACTED]")
        assert (await client.get("/api/debug/failed-requests/invalid")).status_code == 422
        undone = await client.post(f"/api/settings/edit-sessions/{editor['id']}/undo", json={
            "page": "shared.misc", "expected_revision": changed.json()["revisions"]["shared"]})
        assert undone.status_code == 200 and not undone.json()["shared"]["debug_failed_requests"]
        assert runtime.debug_failed_requests  # Running operations keep their captured setting.
        log.save({**record, "turn_id": "recoverable:tracker:0"}, b"request", b"response")
        monkeypatch.setattr("tavern.storage.request_debug.time", lambda: record["created"] + 400 * 86400)
        paths, report = log.plan({"recoverable"})
        assert len(paths) == 1 and report["request_debug_protected_bytes"] > 0
        log.cleanup({"recoverable"})
        assert len(log.records()) == 1
        storage = (await client.get("/api/storage")).json()
        assert storage["request_debug_bytes"] > 0 and storage["request_debug_reclaimable_bytes"] > 0
        await client.post("/api/storage/cleanup", json={})
        assert not log.records()
