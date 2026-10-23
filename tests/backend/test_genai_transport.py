"""Exercise the real Google SDK against isolated HTTP transports, not SDK stubs."""

import asyncio
import copy
import gzip
import hashlib
import json
import zipfile
from contextlib import aclosing

import httpx
import pytest

from tavern.core.cache_policy import CacheSettings, effective_retention, preset
from tavern.core.gemini_capabilities import gemini_controls
from tavern.full_agent.prefix_debug import PrefixDebug, wire_parameters
from tavern.scripting.genai_transport import GenAITransport
from tavern.storage.request_debug import RequestDebugLog
from tests.backend.test_unified_cache import compiled, config, model_messages, provider, reply


@pytest.mark.parametrize(
    "model,sampling,explicit",
    [
        ("gemini-2.5-flash", True, True),
        ("gemini-3.5-flash", True, True),
        ("gemini-3.5-flash-lite-preview", False, True),
        ("models/gemini-3.6-flash", False, True),
        ("gemini-3.8-flash-high", False, True),
        ("gemini-4-argon", False, False),
        ("custom-model", True, True),
    ],
)
async def test_capabilities_remove_only_inapplicable_fields_from_final_sdk_wire(model, sampling, explicit):
    cfg = config("gemini", temperature=0.6, top_p=0.8, top_k=16, reasoning_effort="default")
    cfg.model = model
    # Previously saved controls must not inject fields that the UI has hidden.
    cfg.cache = CacheSettings(
        mode="runtime",
        preset="custom",
        field="obsolete_marker",
        request={"obsolete_cache_option": True},
        retention="24h",
    )
    original = cfg.model_dump()
    sent, captures = [], []

    def route(request):
        sent.append(json.loads(request.content))
        assert request.headers["x-goog-api-key"] == cfg.api_key
        assert "genai" in request.headers.get("x-goog-api-client", "").lower()
        return httpx.Response(200, json=reply("gemini"))

    async with provider(cfg, route, captures.append) as transport:
        assert transport._client is None
        await transport.chat_complete([{"role": "user", "content": "完整正文尾部"}])
    assert len(sent) == 1
    assert "完整正文尾部" in str(sent)
    assert (set(sent[0]["generationConfig"]) & {"temperature", "topP", "topK"}) == (
        {"temperature", "topP", "topK"} if sampling else set()
    )
    assert "obsolete_marker" not in str(sent) and "obsolete_cache_option" not in str(sent)
    assert captures[0]["wire"] == sent[0]
    assert captures[0]["cache"]["transport"] == "google-genai"
    assert gemini_controls("gemini", model) == (sampling, explicit)
    assert cfg.model_dump() == original
    if not explicit:
        assert effective_retention(cfg) == (None, None)
        assert preset(cfg) == {"kind": "implicit", "request": {}}
        assert captures[0]["cache"]["status"] == "cloud"


async def test_gemini4_long_context_keeps_full_stable_prefix_without_cache_objects():
    cfg = config("gemini")
    cfg.model = "gemini-4-argon"
    context, *_ = compiled("full_agent", cfg)
    assert context["cache"]["planned_index"] is None
    assert context["cache"]["status"] == "cloud"
    messages = model_messages(context)
    sent, audits, observed = [], [], []
    debug = PrefixDebug()

    def route(request):
        assert request.url.path.endswith(":generateContent")
        body = json.loads(request.content)
        sent.append(body)
        assert "cachedContent" not in body
        assert "stable ledger data " * 3000 in str(body)
        assert all(f"Answer {i}" in str(body) for i in range(4))
        assert "dynamic handoff" in str(body)
        return httpx.Response(200, json=reply("gemini"))

    async with provider(cfg, route, audits.append) as transport:
        transport.request_observer = lambda request: observed.append(debug.record(request))
        await transport.chat_complete(messages)
        await transport.chat_complete(messages + [{"role": "user", "content": "下一轮末尾"}])
    assert sent[0]["systemInstruction"] == sent[1]["systemInstruction"]
    assert sent[0]["contents"] == sent[1]["contents"][:-1]
    assert all(a["cache"]["sent_index"] is None for a in audits)
    assert len(observed) == 2
    assert [json.loads(body) for body in debug.requests] == sent
    assert {item["parameter"] for item in observed[1]["parameters"]} == set(sent[1])
    assert (
        wire_parameters(debug.requests[0])["systemInstruction"]
        == wire_parameters(debug.requests[1])["systemInstruction"]
    )


@pytest.mark.parametrize("stream", [False, True])
async def test_sdk_tool_signature_usage_and_exact_wire_audit_survive_roundtrip(stream):
    cfg = config("gemini", model_request_mode="stream" if stream else "non_stream")
    cfg.cache.mode = "cloud"
    cfg.api_path = "proxy/v1beta"
    signature = "AP/+/2JpbmFyeQ=="
    native_parts = [
        {"text": "思考", "thought": True},
        {
            "functionCall": {"id": "call-native", "name": "lookup", "args": {"query": "末尾"}},
            "thoughtSignature": signature,
        },
    ]
    requests, audits = [], []

    def route(request):
        requests.append(request)
        assert request.url.path.startswith("/proxy/v1beta/models/")
        result = reply("gemini")
        if len(requests) == 1:
            result["candidates"][0]["content"]["parts"] = native_parts
            result["usageMetadata"]["thoughtsTokenCount"] = 3
            result["usageMetadata"]["providerBillingDetail"] = {"extra": 27}
        if stream:
            return httpx.Response(
                200,
                content="data: " + json.dumps(result) + "\n\n",
                headers={"content-type": "text/event-stream", "x-request-id": "test-request"},
            )
        return httpx.Response(200, json=result, headers={"x-request-id": "test-request"})

    messages = [{"role": "system", "content": "固定规则"}, {"role": "user", "content": "查找"}]
    tools = [
        {
            "type": "function",
            "function": {
                "name": "lookup",
                "parameters": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
            },
        }
    ]
    async with provider(cfg, route, audits.append) as transport:
        await transport.chat_complete(messages, tools=tools)
        assert len(requests) == 1  # SDK never executes the function itself.
        assert transport.reported_usage["completion_tokens"] == 4
        assert transport.reported_usage["raw_usage"][0]["providerBillingDetail"] == {"extra": 27}
        assert transport.last_assistant_extra_fields["reasoning_details"][0]["parts"] == native_parts
        assert transport.last_response_record["headers"]["x-request-id"] == "test-request"
        call = transport.last_tool_calls[0]
        messages += [
            {
                "role": "assistant",
                "content": "",
                **copy.deepcopy(transport.last_assistant_extra_fields),
                "tool_calls": [{"id": call.id, "function": {"name": call.name, "arguments": call.arguments}}],
            },
            {"role": "tool", "tool_call_id": call.id, "content": '{"result":"完整结果末尾"}'},
        ]
        assert (await transport.chat_complete(messages, tools=tools)).content == "OK"
    from google.genai import types

    rows = json.loads(requests[1].content)["contents"]
    parts = [types.Part.model_validate(part) for row in rows for part in row["parts"]]
    original = types.Part.model_validate(native_parts[1])
    assert next(p for p in parts if p.function_call).thought_signature == original.thought_signature
    result = next(p.function_response for p in parts if p.function_response)
    assert result.id == call.id and result.name == "lookup" and result.response == {"result": "完整结果末尾"}
    for request, audit in zip(requests, audits, strict=True):
        assert audit["wire"] == json.loads(request.content)
        assert audit["wire_sha256"] == hashlib.sha256(request.content).hexdigest()


@pytest.mark.parametrize("status", [400, 429, 500])
async def test_sdk_does_not_retry_generation_errors(status):
    calls = []

    def route(request):
        calls.append(request)
        return httpx.Response(status, json={"error": {"code": status, "message": "generation rejected"}})

    async with provider(config("gemini"), route) as transport:
        with pytest.raises(httpx.HTTPStatusError):
            await transport.chat_complete([{"role": "user", "content": "hello"}])
        assert not transport.usage_history
    assert len(calls) == 1


async def test_cancelled_sdk_stream_closes_http_before_provider_shutdown():
    ready = asyncio.Event()

    class BlockingStream(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            yield b'data: {"candidates":[{"content":{"parts":[{"text":"hello"}]}}]}\n\n'
            await asyncio.Event().wait()

        async def aclose(self):
            self.closed = True

    body = BlockingStream()
    async with provider(
        config("gemini", model_request_mode="stream"), lambda _: httpx.Response(200, stream=body)
    ) as transport:
        task = asyncio.create_task(
            transport.chat_complete([{"role": "user", "content": "hello"}], on_text=lambda _: ready.set())
        )
        await asyncio.wait_for(ready.wait(), 3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert body.closed and not transport.usage_history


async def test_sdk_stream_close_and_continuation_do_not_start_extra_requests():
    requests = []

    def route(request):
        requests.append(request)
        payload = reply("gemini")
        payload["candidates"][0].update(finishReason="CONTINUATION", continuationToken="opaque-state")
        return httpx.Response(200, content="data: " + json.dumps(payload) + "\n\n")

    async with provider(config("gemini"), route) as transport:
        with pytest.raises(ValueError, match="未完整结束"):
            async with aclosing(transport._consume_stream([{"role": "user", "content": "hello"}])) as chunks:
                async for _ in chunks:
                    pass
    assert len(requests) == 1


async def test_sdk_reports_stream_error_without_sse_separator_and_keeps_partial_reply_uncommitted(tmp_path):
    partial = {"candidates": [{"content": {"role": "model", "parts": [{"text": "未完成正文"}]}}]}
    error = {"error": {"type": "insufficient_quota", "message": "本次可用额度已耗尽，生成已停止"}}
    wire = "data: " + json.dumps(partial) + "\nevent: error\ndata: " + json.dumps(error) + "\n\n"
    compressed = gzip.compress(wire.encode(), mtime=0)
    requests, previews = [], []

    class RawStream(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            yield compressed[:17]
            yield compressed[17:]

        async def aclose(self):
            self.closed = True

    stream = RawStream()

    def route(request):
        requests.append(request)
        return httpx.Response(
            200, stream=stream, headers={"content-type": "text/event-stream", "content-encoding": "gzip"}
        )

    cfg = config("gemini", model_request_mode="stream", debug_failed_requests=True)
    cfg._data_root = tmp_path
    async with provider(cfg, route) as transport:
        with pytest.raises(
            ValueError,
            match="Gemini 流返回错误（insufficient_quota）：本次可用额度已耗尽，生成已停止；本回合未提交",
        ):
            await transport.chat_complete_with_preview([{"role": "user", "content": "继续"}], previews.append)
        assert previews == ["未完成正文"]
        assert not transport.usage_history and not transport.last_response_record
        assert transport._completed_response is None
    assert len(requests) == 1
    assert stream.closed
    records = RequestDebugLog(tmp_path, cfg).records()
    assert len(records) == 1 and not records[0]["response"]["body"]["complete"]
    assert records[0]["response"]["body"]["representation"] == "decoded"
    assert records[0]["request"]["headers"]["x-goog-api-key"] == "[REDACTED]"
    with zipfile.ZipFile(RequestDebugLog(tmp_path, cfg).path(records[0]["id"])) as archive:
        assert archive.read("response.bin") == wire.encode()
        assert archive.read("request.bin") == requests[0].content


async def test_sdk_model_catalog_pagination_uses_configured_http_and_ignores_cloud_env(monkeypatch):
    monkeypatch.setenv("GOOGLE_GENAI_USE_VERTEXAI", "true")
    monkeypatch.setenv("GOOGLE_GENAI_USE_ENTERPRISE", "true")
    monkeypatch.setenv("GOOGLE_API_KEY", "wrong-key")
    requests = []

    def route(request):
        requests.append(request)
        assert request.headers["x-goog-api-key"] == "configured-key"
        assert request.extensions["timeout"]["read"] == 7
        assert request.url.path == "/custom/v1/models"
        if len(requests) == 1:
            return httpx.Response(200, json={"models": [{"name": "models/first"}], "nextPageToken": "next"})
        assert request.url.params["pageToken"] == "next"
        return httpx.Response(200, json={"models": [{"name": "models/last"}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(route), timeout=7) as http:
        sdk = GenAITransport("https://fixture.test", "custom/v1", "configured-key", http)
        try:
            assert await sdk.list_models() == ["models/first", "models/last"]
        finally:
            await sdk.close()
        assert not http.is_closed  # Runtime owns the supplied network client.


async def test_sdk_validation_does_not_echo_opaque_input():
    cfg = config("gemini")
    native = [
        {
            "role": "assistant",
            "content": "",
            "reasoning_details": [
                {
                    "provider": "gemini",
                    "parts": [{"thoughtSignature": "private-signature", "text": "private-fragment"}],
                }
            ],
        }
    ]
    async with provider(cfg, lambda _: pytest.fail("invalid input must not reach HTTP")) as transport:
        with pytest.raises(ValueError, match="官方 SDK 格式") as error:
            await transport.chat_complete(native)
        assert "private-" not in str(error.value)


async def test_sdk_model_alias_cannot_change_displayed_endpoint_or_query():
    from tavern.core.cache_policy import endpoint

    cfg = config("gemini")
    cfg.model = "relay/gemini-4-argon:custom"
    cfg.api_path = "custom/v1beta"
    urls = []

    def route(request):
        urls.append(str(request.url))
        return httpx.Response(200, json=reply("gemini"))

    async with provider(cfg, route) as transport:
        await transport.chat_complete([{"role": "user", "content": "hello"}])
    assert urls == [endpoint(cfg.base_url, "gemini", cfg.model, api_path=cfg.api_path)]


async def test_expired_cache_retries_complete_context_even_if_relay_delete_returns_no_content():
    cfg = config("gemini")
    context, *_ = compiled("full_agent", cfg)
    requests = []

    def route(request):
        requests.append(request)
        if request.method == "DELETE":
            return httpx.Response(204)
        if request.url.path.endswith("cachedContents"):
            return httpx.Response(200, json={"name": "cachedContents/expired"})
        body = json.loads(request.content)
        if "cachedContent" in body:
            return httpx.Response(404, json={"error": {"code": 404, "message": "cachedContent expired"}})
        assert "stable ledger data " * 3000 in str(body)
        assert "Answer 0" in str(body) and "latest" in str(body)
        return httpx.Response(200, json=reply("gemini"))

    async with provider(cfg, route) as transport:
        assert (await transport.chat_complete(model_messages(context))).content == "OK"
    assert [r.method for r in requests] == ["POST", "POST", "DELETE", "POST"]
