import copy
import json
import time
import uuid

import httpx
import pytest

from tavern.chatgpt import API_ROOT
from tavern.chatgpt.errors import ChatGPTError
from tavern.chatgpt.models import model_details
from tavern.core.models import Settings
from tavern.core.provider import make_provider
from tavern.scripting.native_response import prepare_messages
from tavern.scripting.request_context import ProviderRequestContext, response_origin
from tests.backend.chatgpt_fixtures import ACCESS, IdentityFixture, completed, seed_account, sse

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": name,
            "description": "测试工具",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    }
    for name in ("lookup", "new_context")
]


async def test_account_efforts_include_xhigh_and_are_sent_without_remapping(tmp_path):
    service = IdentityFixture().service(tmp_path)
    account_id, account = seed_account(service)
    details = model_details(
        {
            "slug": "fixture",
            "default_reasoning_level": "low",
            "supported_reasoning_levels": [
                {"effort": level} for level in ("low", "medium", "high", "xhigh", "max", "ultra")
            ],
        }
    )
    assert [level["effort"] for level in details["supported_reasoning_levels"]] == [
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
    ]
    assert details["unavailable_reasoning_levels"][0]["effort"] == "ultra"
    service.remember_models(account_id, account["generation"], [details])
    wire = []

    def route(request):
        wire.append(json.loads(request.content))
        return sse()

    service.http_transport = httpx.MockTransport(route)
    settings = configuration(service, account_id, account)
    settings.reasoning_effort = "xhigh"
    provider = await make_provider(settings)
    try:
        assert (await provider.chat_complete([{"role": "user", "content": "test"}])).content == "完成"
        assert wire[-1]["reasoning"] == {"effort": "xhigh"}
        provider.settings.reasoning_effort = "max"
        await provider.chat_complete([{"role": "user", "content": "test"}])
        assert wire[-1]["reasoning"] == {"effort": "max"}
        provider.settings.reasoning_effort = "none"
        with pytest.raises(ChatGPTError, match="unsupported_reasoning_effort"):
            await provider.chat_complete([{"role": "user", "content": "test"}])
        assert len(wire) == 2
    finally:
        await provider.close()


def configuration(service, account_id, account):
    settings = Settings(
        auth_mode="chatgpt_subscription",
        chatgpt_account_id=account_id,
        model="fixture",
        use_proxy=False,
        api_mode="chat_completions",
        base_url="https://untrusted.example/v9",
        api_path="v9",
        api_key="unused-key",
        temperature=0.8,
        top_p=0.9,
        reply_tokens=1024,
        reasoning_effort="high",
        cache={
            "mode": "runtime",
            "preset": "custom",
            "field": "prompt_cache_breakpoint",
            "value": {"mode": "explicit"},
            "request": {"prompt_cache_options": {"mode": "explicit"}},
        },
    )
    settings._chatgpt, settings._auth_generation = service, account["generation"]
    return settings


async def test_public_wire_is_streamed_allowlisted_complete_and_stable_across_token_refresh(tmp_path):
    identity = IdentityFixture()
    service = identity.service(tmp_path)
    account_id, account = seed_account(service)
    settings = configuration(service, account_id, account)
    wire = []

    async def route(request):
        if str(request.url) != API_ROOT + "/responses":
            return await identity(request)
        wire.append(request)
        return sse()

    service.http_transport = httpx.MockTransport(route)
    capture = []
    context = ProviderRequestContext(str(uuid.uuid4()), "operation", "window", "device")
    context.routing["turn_state"] = "private-codex-route"
    provider = await make_provider(settings, capture.append, request_context=context)
    try:
        text = "原文" * 60000 + "不能丢失的尾部"
        messages = [
            {"role": "system", "content": text},
            {"role": "user", "content": "继续"},
            {"role": "system", "content": "中途插入的完整规则"},
        ]
        first = await provider.chat_complete(
            messages,
            max_tokens=3,
            extra_body={
                "temperature": 2,
                "max_output_tokens": 1,
                "previous_response_id": "must-not-send",
                "metadata": {"x": "y"},
            },
        )
        assert first.content == "完成" and first.usage["total_tokens"] == 25
        body = json.loads(wire[0].content)
        assert str(wire[0].url) == API_ROOT + "/responses"
        assert wire[0].headers["authorization"] == "Bearer " + ACCESS
        assert not {"session-id", "thread-id", "x-codex-turn-state"} & set(wire[0].headers)
        assert set(body) == {"model", "input", "store", "stream", "include", "reasoning", "prompt_cache_key"}
        assert body["store"] is False and body["stream"] is True
        assert [item["role"] for item in body["input"]] == ["developer", "user", "developer"]
        assert body["input"][0]["content"] == text
        assert body["input"][2]["content"] == "中途插入的完整规则"
        assert ACCESS not in json.dumps(capture)
        origin = response_origin(provider.settings)
        native = copy.deepcopy(provider.last_native_response)
        with service.vault.locked():
            value = service.vault.load()
            value["accounts"][account_id]["tokens"]["expires_at"] = time.time() - 1
            service.vault.save(value)
        await provider.chat_complete(messages)
        assert wire[1].headers["authorization"] == "Bearer fixture-access-1"
        assert response_origin(provider.settings) == origin
        replay, invalid = prepare_messages(
            [{"role": "assistant", "content": "完成", "_native_response": native}], origin
        )
        assert not invalid and replay[0]["_native_response"] == native
        await service.logout(account_id)
        with pytest.raises(ChatGPTError, match="sign_in_required"):
            await provider.chat_complete(messages)
        assert len(wire) == 2
    finally:
        await provider.close()


async def test_namespace_tools_forced_selection_and_lossless_native_continuation(tmp_path):
    service = IdentityFixture().service(tmp_path)
    account_id, account = seed_account(service)
    settings = configuration(service, account_id, account)
    wire = []

    def route(request):
        wire.append(json.loads(request.content))
        if len(wire) == 1:
            payload = completed("", [("lookup", {})])
            payload["output"].insert(
                0, {"type": "reasoning", "id": "r1", "encrypted_content": "encrypted-fixture", "summary": []}
            )
            return sse(payload)
        return sse(completed("工具续答完成"))

    service.http_transport = httpx.MockTransport(route)
    provider = await make_provider(settings)
    try:
        first = await provider.chat_complete(
            [{"role": "user", "content": "查阅资料"}],
            extra_body={"tools": TOOLS, "tool_choice": {"type": "function", "function": {"name": "lookup"}}},
        )
        assert not first.content
        assert [(call.id, call.name) for call in provider.last_tool_calls] == [("call-0", "lookup")]
        assert wire[0]["tool_choice"] == "required"
        assert wire[0]["tools"][0]["type"] == "namespace" and wire[0]["tools"][0]["name"] == "tavern"
        assert [tool["name"] for tool in wire[0]["tools"][0]["tools"]] == ["lookup"]
        native = copy.deepcopy(provider.last_native_response)
        tool_result = "完整资料" * 40000 + "工具结果尾部"
        messages = [
            {"role": "user", "content": "查阅资料"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "call-0", "function": {"name": "lookup", "arguments": "{}"}}],
                "_native_response": native,
            },
            {"role": "tool", "tool_call_id": "call-0", "content": tool_result},
        ]
        assert "".join([part async for part in provider.chat(messages, tools=TOOLS)]) == "工具续答完成"
        assert wire[1]["input"][1]["encrypted_content"] == "encrypted-fixture"
        assert wire[1]["input"][2]["namespace"] == "tavern"
        assert wire[1]["input"][-1]["output"] == tool_result
        assert wire[1]["input"][-1]["namespace"] == "tavern"
    finally:
        await provider.close()


@pytest.mark.parametrize(
    "namespace,name", [("another_app", "lookup"), (None, "lookup"), ("tavern", "unapproved")]
)
async def test_unapproved_tool_response_is_never_accepted(tmp_path, namespace, name):
    service = IdentityFixture().service(tmp_path)
    account_id, account = seed_account(service)
    payload = completed("", [(name, {})])
    payload["output"][0]["namespace"] = namespace
    service.http_transport = httpx.MockTransport(lambda _: sse(payload))
    provider = await make_provider(configuration(service, account_id, account))
    try:
        with pytest.raises(ChatGPTError, match="invalid_tool_call"):
            await provider.chat_complete([{"role": "user", "content": "test"}], tools=TOOLS)
        assert not provider.last_tool_calls
    finally:
        await provider.close()


@pytest.mark.parametrize(
    "failure", ["limit", "incomplete", "missing_completion", "error", "redirect", "region"]
)
async def test_failed_and_interrupted_streams_do_not_complete_or_fallback(tmp_path, failure, monkeypatch):
    monkeypatch.setenv("TAVERN_API_KEY", "must-not-fallback-to-this-key")
    service = IdentityFixture().service(tmp_path)
    account_id, account = seed_account(service)
    calls = []

    def route(request):
        calls.append(request)
        if failure == "redirect":
            return httpx.Response(307, headers={"location": "https://untrusted.example/steal"})
        if failure == "region":
            return httpx.Response(403, json={"detail": "region restriction " + ACCESS})
        events = [{"type": "response.output_text.delta", "delta": "未完成草稿"}]
        if failure == "limit":
            events.append(
                {
                    "type": "response.failed",
                    "response": {
                        "error": {"code": "subscription_sharing_usage_limit_exceeded", "message": ACCESS}
                    },
                }
            )
        elif failure == "incomplete":
            events.append({"type": "response.incomplete", "response": {"status": "incomplete"}})
            events.append({"type": "response.output_text.delta", "delta": "尾部仍需读取"})
        elif failure == "error":
            events.append(
                {"type": "error", "code": "subscription_sharing_usage_unavailable", "message": "fixture"}
            )
        return sse(events=events)

    service.http_transport = httpx.MockTransport(route)
    provider = await make_provider(configuration(service, account_id, account))
    errors = []
    chunks = []
    provider.response_capture = errors.append
    try:
        with pytest.raises(ValueError):
            await provider.chat_complete([{"role": "user", "content": "test"}], on_text=chunks.append)
        if failure == "incomplete":
            assert "".join(chunks) == "未完成草稿尾部仍需读取"
        assert len(calls) == 1
        assert provider.last_native_response is None and not provider.last_tool_calls
        assert ACCESS not in json.dumps(errors)
        assert service._read(account_id)["tokens"]
    finally:
        await provider.close()


async def test_missing_permission_does_not_use_environment_key_or_send_request(tmp_path, monkeypatch):
    monkeypatch.setenv("TAVERN_API_KEY", "must-not-fallback")
    identity = IdentityFixture()
    service = identity.service(tmp_path)
    account_id, account = seed_account(service, scopes="openid profile email")
    with pytest.raises(ChatGPTError, match="plan_permission_required"):
        await make_provider(configuration(service, account_id, account))
    assert identity.requests == []


async def test_account_or_authorization_change_invalidates_native_history(tmp_path):
    service = IdentityFixture().service(tmp_path)
    account_id, account = seed_account(service)
    settings = configuration(service, account_id, account)
    service.http_transport = httpx.MockTransport(lambda _: sse())
    provider = await make_provider(settings)
    try:
        await provider.chat_complete([{"role": "user", "content": "test"}])
        message = {"role": "assistant", "content": "完成", "_native_response": provider.last_native_response}
        changed = provider.settings.model_copy(deep=True)
        changed._auth_generation = uuid.uuid4().hex
        portable, invalid = prepare_messages([message], response_origin(changed))
        assert "_native_response" not in portable[0] and portable[0]["content"] == "完成"
        assert invalid[0]["reason"] == "provider_or_model_changed"
        second, _ = seed_account(service)
        changed.chatgpt_account_id = second
        assert response_origin(changed) != response_origin(provider.settings)
    finally:
        await provider.close()


async def test_jwt_shaped_model_text_and_encrypted_reasoning_are_not_redacted(tmp_path):
    service = IdentityFixture().service(tmp_path)
    account_id, account = seed_account(service)
    encoded = "eyJhbGciOiJub25lIn0.eyJzdWIiOiJzdG9yeSJ9.c2lnbmF0dXJl"
    payload = completed("角色道具上的完整示例：" + encoded)
    payload["output"].insert(
        0, {"type": "reasoning", "id": "r1", "encrypted_content": encoded, "summary": []}
    )
    payload["diagnostics"] = {"authorization": "Bearer " + ACCESS}
    service.http_transport = httpx.MockTransport(lambda _: sse(payload))
    provider = await make_provider(configuration(service, account_id, account))
    try:
        result = await provider.chat_complete([{"role": "user", "content": "保留原文示例"}])
        assert result.content.endswith(encoded)
        assert provider.last_native_response["items"][0]["encrypted_content"] == encoded
        assert ACCESS not in json.dumps(provider.last_response_record["diagnostics"])
    finally:
        await provider.close()


async def test_official_api_preserves_cache_compaction_and_credential_bound_native_state(monkeypatch):
    from tavern.scripting.sent_breakpoints import marked_boundaries

    tools = copy.deepcopy(TOOLS)
    tools[0]["function"]["parameters"]["properties"]["limit"] = {"type": "integer", "default": 5}
    wire = []
    fail = False
    wrong_tool = False

    def route(request):
        wire.append(request)
        if fail:
            return httpx.Response(400, json={"error": {"message": "unsupported prompt_cache_options"}})
        body = json.loads(request.content)
        payload = completed("OK" if "context_management" in body else "完成")
        if isinstance(body.get("tool_choice"), dict):
            name = "lookup" if wrong_tool else body["tool_choice"]["tools"][0]["name"]
            payload = completed("", [(name, {})])
            for item in payload["output"]:
                item.pop("namespace", None)
        if "context_management" in body:
            payload["output"].insert(0, {"type": "compaction", "id": "cmp-1", "encrypted_content": "opaque"})
        return sse(payload)

    client_type = httpx.AsyncClient

    class Client(client_type):
        def __init__(self, **kwargs):
            super().__init__(transport=httpx.MockTransport(route), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", Client)
    settings = Settings(
        auth_mode="openai_api_key",
        api_key="official-api-test-key",
        base_url="https://official.test",
        api_mode="responses",
        api_path="v1",
        model="gpt-6-sol",
        reply_tokens=4096,
        cache={"mode": "cloud"},
    )
    context = ProviderRequestContext(str(uuid.uuid4()), "turn", "window", "device")
    context.routing["turn_state"] = "must-not-send"
    provider = await make_provider(settings, request_context=context)
    header = {"role": "system", "content": "完整固定规则" * 2000 + "固定尾部", "_cache_breakpoint": True}
    messages = [header, {"role": "user", "content": "完整用户输入"}]
    try:
        await provider.chat_complete(messages, tools=tools)
        body = json.loads(wire[-1].content)
        assert str(wire[-1].url) == "https://official.test/v1/responses"
        assert wire[-1].headers["authorization"] == "Bearer official-api-test-key"
        assert not {"session-id", "thread-id", "x-codex-turn-state"} & set(wire[-1].headers)
        assert body["store"] is False and body["stream"] is True
        assert body["max_output_tokens"] == 4096
        assert body["input"][0]["content"] == header["content"]
        assert body["input"][0]["role"] == "developer"
        assert body["include"] == ["reasoning.encrypted_content"]
        assert not {"context_management", "prompt_cache_options", "prompt_cache_retention"} & body.keys()
        assert body["prompt_cache_key"] == context.session_id
        original_tools = body["tools"]
        assert all(tool["type"] == "function" and tool["strict"] is False for tool in original_tools)
        assert original_tools[0]["parameters"] == tools[0]["function"]["parameters"]

        forced = {"tool_choice": {"type": "function", "function": {"name": "new_context"}}}
        await provider.chat_complete(messages, tools=tools, extra_body=forced)
        body = json.loads(wire[-1].content)
        assert body["tools"] == original_tools
        assert body["tool_choice"] == {
            "type": "allowed_tools",
            "mode": "required",
            "tools": [{"type": "function", "name": "new_context"}],
        }
        assert provider.last_tool_calls[0].name == "new_context"
        wrong_tool = True
        with pytest.raises(ChatGPTError, match="invalid_tool_call"):
            await provider.chat_complete(messages, tools=tools, extra_body=forced)
        assert provider.last_native_response is None and not provider.last_tool_calls
        wrong_tool = False

        provider.settings.cache.mode = "runtime"
        provider.settings.cache.retention = "12h"
        await provider.chat_complete(messages, tools=tools)
        body = json.loads(wire[-1].content)
        assert body["prompt_cache_options"] == {"mode": "explicit", "ttl": "12h"}
        assert len(marked_boundaries(body, "responses")) == 1
        # Moving the live marker must retain the previously sent prefix boundary.
        continued = [
            dict(header, _cache_breakpoint=False),
            messages[1],
            {"role": "assistant", "content": "完整旧回复", "_cache_breakpoint": True},
        ]
        await provider.chat_complete(continued, tools=tools)
        assert len(marked_boundaries(json.loads(wire[-1].content), "responses")) == 2

        await provider.chat_complete(
            continued,
            tools=tools,
            extra_body={"tool_choice": "none"},
            _compact_threshold=1024,
            _maintenance_instructions="只整理历史",
        )
        body = json.loads(wire[-1].content)
        assert body["context_management"] == [{"type": "compaction", "compact_threshold": 1024}]
        assert body["tool_choice"] == "none"
        native = copy.deepcopy(provider.last_native_response)
        assert native["items"][0]["encrypted_content"] == "opaque"
        replay = [
            {"role": "assistant", "content": "OK", "_native_response": native},
            {"role": "user", "content": "当前交流保持完整"},
        ]
        await provider.chat_complete(replay)
        body = json.loads(wire[-1].content)
        assert "context_management" not in body
        assert body["input"][0]["encrypted_content"] == "opaque"
        changed = provider.settings.model_copy(update={"api_key": "different-key"})
        _, invalid = prepare_messages(replay, response_origin(changed))
        assert invalid
        ordinary = provider.settings.model_copy(update={"auth_mode": "api_key"})
        assert response_origin(ordinary) != response_origin(provider.settings)
        legacy = copy.deepcopy(replay)
        legacy[0]["_native_response"]["origin"].pop("tool_format")
        portable, invalid = prepare_messages(legacy, response_origin(provider.settings))
        assert invalid and portable[0]["content"] == "OK" and "_native_response" not in portable[0]

        count = len(wire)
        provider.settings.model, provider.settings.reasoning_effort = "gpt-6-astra", "none"
        with pytest.raises(ValueError, match="思考档位"):
            await provider.chat_complete(messages)
        assert len(wire) == count and provider.settings.reasoning_effort == "none"
        provider.settings.model, provider.settings.reasoning_effort = "gpt-6-sol", "default"

        fail = True
        count = len(wire)
        with pytest.raises(ChatGPTError, match="unsupported prompt_cache_options") as rejected:
            await provider.chat_complete(messages)
        assert rejected.value.public()["usage_url"] == "https://platform.openai.com/usage"
        assert len(wire) == count + 1  # No implicit cache fallback or billing-source switch.
    finally:
        await provider.close()


def test_official_api_cache_configuration_keeps_user_values_and_rejects_non_generation_controls():
    from tavern.core.cache_policy import preset, validate_openai_cache
    from tavern.scripting.reasoning import reasoning_capabilities

    cfg = Settings(
        auth_mode="openai_api_key",
        api_mode="responses",
        model="gpt-6-astra",
        cache={"mode": "runtime", "retention": "12h"},
    )
    validate_openai_cache(cfg)
    assert preset(cfg)["request"] == {"prompt_cache_options": {"mode": "explicit", "ttl": "12h"}}
    cfg.model = "gpt-5.5"
    with pytest.raises(ValueError, match="已保存的缓存时效 12h"):
        validate_openai_cache(cfg)
    assert cfg.cache.retention == "12h"
    cfg.model, cfg.cache.retention = "gpt-6-astra", "default"
    assert preset(cfg)["request"] == {"prompt_cache_options": {"mode": "explicit"}}
    cfg.cache.request = {"prompt_cache_options": {"prewarm": True}}
    with pytest.raises(ValueError, match="独立缓存预热"):
        validate_openai_cache(cfg)
    cfg.cache.request = {"prompt_cache_options": {"ttl": "12h"}}
    with pytest.raises(ValueError, match="时效选择器"):
        validate_openai_cache(cfg)
    assert cfg.cache.request["prompt_cache_options"]["ttl"] == "12h"
    cfg.cache.request = {}
    cfg.cache.preset, cfg.cache.field = "custom", "prompt_cache_breakpoint"
    cfg.cache.value = {"mode": "explicit", "ttl": "12h"}
    with pytest.raises(ValueError, match="时效请使用选择器"):
        validate_openai_cache(cfg)
    known = reasoning_capabilities("responses", "gpt-6-astra", official_api=True)
    assert [level["effort"] for level in known["supported_reasoning_levels"]] == [
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
    ]
    unknown = reasoning_capabilities("responses", "deployment-gpt-6-astra", official_api=True)
    assert "未知" in unknown["note"] and any(
        level["effort"] == "none" for level in unknown["supported_reasoning_levels"]
    )


async def test_official_api_failed_billing_is_recorded_without_replacing_window_observation(tmp_path):
    from tavern.chatgpt.provider import ChatGPTProvider
    from tavern.core.context_usage import bind_meter, context_usage
    from tavern.scripting.request_context import configure_operation
    from tavern.storage.store import Store

    store = Store(tmp_path)
    cfg = bind_meter(
        Settings(
            auth_mode="openai_api_key",
            api_mode="responses",
            api_key="test-api-key",
            api_path="v1",
            model="gpt-6-sol",
        ),
        store,
        "story",
        "billing-fixture",
    )
    requests = 0
    billed = {
        "input_tokens": 37,
        "output_tokens": 128,
        "input_tokens_details": {"cached_tokens": 5, "cache_write_tokens": 7},
        "output_tokens_details": {"reasoning_tokens": 128},
    }

    def route(request):
        nonlocal requests
        requests += 1
        if requests == 1:
            return sse(completed("完整回复"))
        response = {
            "status": "incomplete",
            "incomplete_details": {"reason": "max_output_tokens"},
            "output": [],
        }
        if requests == 2:
            response["usage"] = billed
        return sse(events=[{"type": "response.incomplete", "response": response}])

    client = httpx.AsyncClient(transport=httpx.MockTransport(route))
    provider = ChatGPTProvider(cfg, cfg.api_key, http_client=client)
    events = []
    context = ProviderRequestContext(str(uuid.uuid4()), "turn", "window", "device")
    configure_operation(provider, context, lambda kind, payload: events.append((kind, payload)), "narrative")
    messages = [{"role": "user", "content": "同一完整输入"}]
    try:
        await provider.chat_complete(messages)
        before = context_usage(messages, provider.settings)
        assert before["tokens"] == 20 and before["last_attempt"] == "completed"
        with pytest.raises(ChatGPTError, match="输出上限包含思考"):
            await provider.chat_complete(messages)
        failed = [payload for kind, payload in events if kind == "usage"]
        assert len(failed) == 1 and failed[0]["status"] == "failed"
        assert failed[0]["usage"]["raw_usage"] == billed
        assert failed[0]["usage"]["total_tokens"] is None
        assert provider.reported_usage["total_tokens"] is None
        assert provider.last_response_record["raw_usage"] == billed
        after = context_usage(messages, provider.settings)
        assert after["tokens"] == before["tokens"] and after["last_attempt"] == "request_failed"
        assert provider.last_native_response is None and not provider.last_tool_calls
        assert len(provider.usage_history) == 2
        with pytest.raises(ChatGPTError):
            await provider.chat_complete(messages)
        assert len(provider.usage_history) == 2
        assert len([payload for kind, payload in events if kind == "usage"]) == 1
        assert "raw_usage" not in provider.last_response_record
    finally:
        await provider.close()
