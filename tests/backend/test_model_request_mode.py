"""Shared transport selection reaches the wire without changing response contracts."""
import json
import zipfile

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from tavern.application.api import create_app
from tavern.core.kemini import TOOL_NAME, KeminiConfig, control_text, inject_half
from tavern.core.provider import make_provider
from tavern.core.settings import SettingsDocument
from tavern.full_agent.prefix_debug import PrefixDebug, compare_parameters, wire_parameters
from tavern.scripting.kemini import AuxiliaryParser, KeminiReply
from tavern.scripting.model_stream import StreamState
from tavern.storage.request_debug import RequestDebugLog
from tavern.storage.store import Store
from tests.backend.chatgpt_fixtures import IdentityFixture, seed_account, sse
from tests.backend.test_chatgpt_provider import configuration
from tests.backend.test_unified_cache import config, provider, reply

APIS = ["chat_completions", "responses", "anthropic", "gemini"]
MESSAGES = [{"role": "user", "content": "完整原文，包含末尾"}]
TOOLS = [{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}]


async def test_kemini_fragmented_reply_preserves_prose_and_rejects_mixed_tools():
    from types import SimpleNamespace
    cfg = config(model_request_mode="stream")
    cfg.kemini = KeminiConfig(enabled=True, normal_disclaimer_enabled=True)
    prose = '开门😀\n```html\n<thinking>代码原文</thinking>\n```'
    raw = '<thinking>创作安排\n含"引号"</thinking>' + prose + '<disclaimer>声明</disclaimer>'
    encoded = json.dumps({"content": raw})
    frames = [{"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "reply1",
        "function": {"name": TOOL_NAME, "arguments": ""}}]}}]}]
    frames.extend({"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {
        "arguments": encoded[i:i + 7]}}]}}]} for i in range(0, len(encoded), 7))
    frames.append({"choices": [{"delta": {}, "finish_reason": "tool_calls"}]})
    chunks = []
    async with provider(cfg, lambda _: sse(events=frames)) as p:
        p.operation_purpose = "narrative"
        response = await p.chat_complete_with_preview(MESSAGES, chunks.append)
        assert response.content == "".join(chunks) == prose
        assert p.last_auxiliary == [{"kind": "thinking", "text": '创作安排\n含"引号"'}, {"kind": "disclaimer", "text": "声明"}]
        assert len(chunks) > 1
        assert p.last_response_record["raw_response"]["choices"][0]["message"]["tool_calls"]
    call = SimpleNamespace(name=TOOL_NAME, arguments=encoded)
    with pytest.raises(ValueError, match="不能与业务工具混合"):
        KeminiReply(cfg.kemini, True).finish("", [call, SimpleNamespace(name="read_state")])
    with pytest.raises(ValueError, match="未闭合"):
        KeminiReply(cfg.kemini, True).finish("<thinking>不完整", [])
    assert AuxiliaryParser(KeminiConfig()).feed(raw, final=True) == raw
    disabled_disclaimer = AuxiliaryParser(KeminiConfig(normal_disclaimer_enabled=True))
    assert disabled_disclaimer.feed(raw, final=True) == raw


def test_prefix_debug_preserves_wire_bytes_and_separate_parameters():
    previous = '{"messages":[{"content":"中文原文"}],"tools":[{"name":"old"}],"system":"stable","gone":true}'.encode()
    current = '{"messages":[{"content":"中文原文"},{"content":"追加"}],"tools":[{"name":"new"}],"system":"stable","added":null}'.encode()
    result = {item["parameter"]: item for item in compare_parameters(previous, current)}
    messages = wire_parameters(previous)["messages"]
    assert result["messages"] == {"parameter": "messages", "previous_bytes": len(messages),
        "current_bytes": len(wire_parameters(current)["messages"]), "status": "array_extended",
        "common_prefix_bytes": len(messages) - 1, "first_difference_byte": len(messages) - 1}
    assert result["tools"]["status"] == "changed"
    assert result["tools"]["common_prefix_bytes"] == len(b'[{"name":"')
    assert result["system"]["status"] == "identical" and result["system"]["first_difference_byte"] is None
    assert result["gone"]["status"] == "removed" and result["gone"]["current_bytes"] is None
    assert result["added"]["status"] == "added" and result["added"]["previous_bytes"] is None
    # Semantically equal JSON still differs byte-for-byte; never normalize it.
    spelling = b'{"messages":[{"content":"\\u4e2d\\u6587\\u539f\\u6587"}],"tools":[{ "name": "old"}]}'
    changed = {item["parameter"]: item for item in compare_parameters(previous, spelling)}
    assert changed["messages"]["first_difference_byte"] == len(b'[{"content":"')
    assert changed["tools"]["first_difference_byte"] == 2
    debug = PrefixDebug()
    def record(body):
        return debug.record(httpx.Request("POST", "https://fixture.test", content=body))
    assert record(previous)["parameters"] == []
    report = record(current)
    assert "中文原文" not in json.dumps(report, ensure_ascii=False)
    assert "messages：公共前缀与首个差异未错位" in report["message"]
    assert "tools：不一致" in report["message"] and "system：一致" in report["message"]
    assert "字节" not in report["message"]
    record(previous)
    assert list(debug.requests) == [current, previous]
    identical = record(previous)["message"]
    assert "messages：公共前缀与首个差异未错位" in identical and "tools：一致" in identical
    debug.clear()
    assert not debug.requests and debug.sequence == 0


async def test_prefix_debug_setting_defaults_projection_and_page_undo(tmp_path):
    app = create_app(tmp_path)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        editor = (await client.post("/api/settings/edit-sessions", json={})).json()
        before = editor["settings"]
        assert before["shared"]["debug_full_agent_prefix"] is False
        assert not before["shared"]["kemini"]["enabled"]
        assert not before["shared"]["kemini"]["normal_disclaimer_enabled"]
        assert "debug_full_agent_prefix" in next(p for p in before["pages"] if p["id"] == "shared.misc")["paths"]
        changed = await client.patch("/api/settings/shared", json={
            "expected_revision": before["revisions"]["shared"], "edit_session": editor["id"],
            "changes": {"debug_full_agent_prefix": True, "kemini.normal_disclaimer_enabled": True,
                        "kemini.filler_text": "完整填充末尾"}})
        assert changed.status_code == 200, changed.text
        runtime = app.state.store.settings("full_agent").for_role("full_agent")
        assert runtime.debug_full_agent_prefix
        assert runtime.kemini.normal_disclaimer_enabled and not runtime.kemini.enabled
        assert runtime.kemini.filler_text.endswith("末尾")
        invalid = await client.patch("/api/settings/shared", json={
            "expected_revision": changed.json()["revisions"]["shared"],
            "changes": {"debug_full_agent_prefix": "false"}})
        assert invalid.status_code == 400
        undone = await client.post(f"/api/settings/edit-sessions/{editor['id']}/undo", json={
            "page": "shared.misc", "expected_revision": changed.json()["revisions"]["shared"]})
        assert undone.status_code == 200 and not undone.json()["shared"]["debug_full_agent_prefix"]
        assert not undone.json()["shared"]["kemini"]["normal_disclaimer_enabled"]
        assert runtime.debug_full_agent_prefix
    legacy = SettingsDocument().model_dump()
    del legacy["shared"]["debug_full_agent_prefix"]
    assert SettingsDocument.model_validate(legacy).shared.debug_full_agent_prefix is False


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


def carried_reply(api):
    payload = reply(api)
    content = "<thinking>创作安排</thinking>OK<disclaimer>声明</disclaimer>"
    arguments = json.dumps({"content": content}, ensure_ascii=False)
    if api == "chat_completions":
        call = {"index": 0, "id": "call1", "type": "function", "function": {"name": TOOL_NAME, "arguments": arguments}}
        payload["choices"] = [{"message": {"content": "", "tool_calls": [call]}, "finish_reason": "tool_calls"}]
        stream = [{"choices": [{"delta": {"tool_calls": [call]}, "finish_reason": "tool_calls"}]}, {"usage": payload["usage"]}]
    elif api == "responses":
        item = {"type": "function_call", "id": "fc1", "call_id": "call1", "name": TOOL_NAME, "arguments": arguments}
        payload["output"] = [item]
        stream = [{"type": "response.output_item.added", "output_index": 0, "item": {**item, "arguments": ""}},
                  {"type": "response.function_call_arguments.delta", "item_id": "fc1", "delta": arguments},
                  {"type": "response.completed", "response": payload}]
    elif api == "anthropic":
        item = {"type": "tool_use", "id": "call1", "name": TOOL_NAME, "input": {"content": content}}
        payload.update(content=[item], stop_reason="tool_use")
        stream = [{"type": "message_start", "message": {**payload, "content": []}},
                  {"type": "content_block_start", "index": 0, "content_block": {**item, "input": {}}},
                  {"type": "content_block_delta", "index": 0, "delta": {"partial_json": arguments}},
                  {"type": "message_delta", "delta": {"stop_reason": "tool_use"}}, {"type": "message_stop"}]
    else:
        payload["candidates"][0]["content"]["parts"] = [{"functionCall": {"name": TOOL_NAME, "args": {"content": content}}}]
        stream = [payload]
    return payload, stream


@pytest.mark.parametrize("api", APIS)
@pytest.mark.parametrize("mode,entry,streamed", [
    ("stream", "complete", True), ("non_stream", "chat", False),
    ("auto", "complete", False), ("auto", "chat", True),
])
async def test_mode_controls_wire_and_preserves_content_tools_and_usage(api, mode, entry, streamed):
    cfg = config(api, model_request_mode=mode)
    cfg.kemini = KeminiConfig(enabled=True, normal_disclaimer_enabled=True)
    payload, stream = carried_reply(api)
    sent = []

    def route(request):
        body = json.loads(request.content)
        sent.append(body)
        assert "完整原文，包含末尾" in str(body)
        assert body["tools"]
        assert TOOL_NAME in str(body["tools"])
        if api == "gemini":
            assert (":streamGenerateContent" in request.url.path) is streamed
            assert (request.url.params.get("alt") == "sse") is streamed
        else:
            assert bool(body.get("stream")) is streamed
        return sse(events=stream) if streamed else httpx.Response(200, json=payload)

    async with provider(cfg, route) as p:
        p.operation_purpose = "narrative"
        if entry == "complete":
            response = await p.chat_complete(MESSAGES, tools=TOOLS)
            assert response.content == "OK" and response.finish_reason == "stop"
        else:
            assert "".join([chunk async for chunk in p.chat(MESSAGES, tools=TOOLS)]) == "OK"
        assert not p.last_tool_calls and p.last_native_response is None
        assert [part["kind"] for part in p.last_auxiliary] == ["thinking", "disclaimer"]
        assert p.last_response_record["kemini"]["transported"]
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
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        {"usage": reply("chat_completions")["usage"]},
    ]
    cfg = config(model_request_mode="stream")
    async with provider(cfg, lambda _: sse(events=tool_events)) as p:
        result = await p.chat_complete(MESSAGES, tools=TOOLS)
        assert result.finish_reason == "tool_calls"
        assert p.last_tool_calls[0].name == "lookup"
        assert json.loads(p.last_tool_calls[0].arguments) == {"q": "原文"}
        assert result.usage["prompt_tokens"] == 10
        assert len(p.last_tool_calls) == 1
    async with provider(cfg, lambda _: sse(events=tool_events[:1])) as p:
        with pytest.raises(ValueError, match="提前结束"):
            await p.chat_complete(MESSAGES, tools=TOOLS)
        assert not p.usage_history and not p.last_tool_calls

    state = StreamState("chat_completions")
    for event in tool_events:
        state.feed(event)
    with pytest.raises(ValueError, match="完成事件之后"):
        state.feed({"choices": [{"delta": {"content": "非法追加"}, "finish_reason": "stop"}]})
    with pytest.raises(ValueError, match="完成事件之后"):
        state.feed(tool_events[1])


@pytest.mark.parametrize("entry", ["complete", "chat", "preview"])
async def test_chatgpt_subscription_keeps_required_streaming(entry, tmp_path):
    service = IdentityFixture().service(tmp_path)
    account_id, account = seed_account(service)
    cfg = configuration(service, account_id, account)
    cfg.model_request_mode = "non_stream"
    cfg.kemini.enabled = True
    seen = []

    def route(request):
        seen.append(json.loads(request.content))
        return sse()

    service.http_transport = httpx.MockTransport(route)
    p = await make_provider(cfg)
    p.operation_purpose = "narrative"
    observed = []
    p.request_observer = lambda request: observed.append(request.content)
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
        assert TOOL_NAME in str(seen[0]["tools"])
        assert p.last_response_record["kemini"]["transported"] is False
        assert len(observed) == 1 and json.loads(observed[0]) == seen[0]
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
            for role in ("foreground", "background", "card_author", "full_agent"):
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
    wire_events = events(api)
    if api == "gemini":
        cfg.kemini.enabled = True
        def function_chunk(call, finish=None):
            candidate = {"content": {"role": "model", "parts": [{"functionCall": call}]}}
            if finish:
                candidate["finishReason"] = finish
            return {"candidates": [candidate]}
        wire_events = [function_chunk({"name": TOOL_NAME, "willContinue": True}),
                       function_chunk({"partialArgs": [{"jsonPath": "$.content", "stringValue": "OK"}], "willContinue": True}),
                       function_chunk({"partialArgs": [{"jsonPath": "$.content"}]}),
                       function_chunk({}, "STOP"), {"usageMetadata": reply(api)["usageMetadata"]}]
    chunks = []
    async with provider(cfg, lambda _: sse(events=wire_events)) as p:
        p.operation_purpose = "narrative"
        response = await p.chat_complete_with_preview(MESSAGES, chunks.append, tools=TOOLS)
        assert "".join(chunks) == "OK"
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
    cfg.kemini = KeminiConfig(enabled=True, normal_disclaimer_enabled=True)
    chunks = []
    payload = reply("chat_completions")
    payload["choices"][0]["message"]["content"] = "OK<disclaimer>声明</disclaimer>"
    def messages():
        return [{key: message[key] for key in ("role", "content")} for message in
                inject_half([{**message, "source": "input"} for message in MESSAGES], cfg.kemini)]
    def route(request):
        body = json.loads(request.content)
        if cfg.kemini.enabled:
            assert body["tools"][0]["function"]["name"] == TOOL_NAME
            assert cfg.kemini.disclaimer_text in body["messages"][-1]["content"]
        else:
            assert "tools" not in body
            assert body["messages"] == MESSAGES
        return httpx.Response(200, json=payload)
    async with provider(cfg, route) as p:
        p.operation_purpose = "narrative"
        result = await p.chat_complete_with_preview(messages(), chunks.append)
        assert result.content == "OK"
        assert chunks == []
        assert p.last_auxiliary == [{"kind": "disclaimer", "text": "声明"}]
        # Turning off the parent suppresses both request instructions and output
        # parsing without erasing the user's saved child preference.
        cfg.kemini.enabled = False
        assert control_text(cfg.kemini) == ""
        result = await p.chat_complete_with_preview(messages(), chunks.append)
        assert result.content == "OK<disclaimer>声明</disclaimer>"
        assert p.last_auxiliary == [] and cfg.kemini.normal_disclaimer_enabled
        cfg.kemini.enabled = True
        result = await p.chat_complete_with_preview(messages(), chunks.append)
        assert result.content == "OK" and chunks == []
        assert p.last_auxiliary == [{"kind": "disclaimer", "text": "声明"}]


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
            for role in ("foreground", "background", "card_author", "full_agent"):
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
        log.save({**record, "turn_id": "recoverable:tracker:0"}, b"request", None)
        log.save(record, b"request", None)
        assert log.clear({"recoverable"}) == {"removed": 1, "protected": 1}
        assert (await client.delete("/api/debug/failed-requests")).json() == {"removed": 1, "protected": 0}
        assert not log.records()
