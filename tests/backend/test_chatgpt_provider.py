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
        elif failure == "error":
            events.append(
                {"type": "error", "code": "subscription_sharing_usage_unavailable", "message": "fixture"}
            )
        return sse(events=events)

    service.http_transport = httpx.MockTransport(route)
    provider = await make_provider(configuration(service, account_id, account))
    errors = []
    provider.response_capture = errors.append
    try:
        with pytest.raises(ValueError):
            await provider.chat_complete([{"role": "user", "content": "test"}])
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
