"""Hosted web search on the public subscription route, without granting other agents tools."""
import copy
import json

import httpx
import pytest

from tavern.character.editor_tools import tool_definitions
from tavern.core.provider import make_provider
from tavern.scripting.native_response import message_digest
from tavern.web.cloud import WebSession
from tests.backend.chatgpt_fixtures import IdentityFixture, completed, seed_account, sse
from tests.backend.test_chatgpt_provider import configuration


def search_result():
    payload = completed("官方说明如下。")
    payload["output"][0]["content"][0]["annotations"] = [{"type": "url_citation", "start_index": 0, "end_index": 6,
        "title": "官方文档", "url": "https://example.org/docs"}]
    payload["output"].insert(0, {"type": "web_search_call", "id": "ws-fixture", "status": "completed",
        "action": {"type": "search", "query": "官方说明", "sources": [{"type": "url", "url": "https://example.org/docs", "title": "官方文档"}]}})
    return payload


@pytest.mark.parametrize("role", ["advisor", "card_author"])
async def test_cloud_search_wire_citations_and_lossless_continuation(tmp_path, role):
    accounts = IdentityFixture().service(tmp_path)
    aid, account = seed_account(accounts)
    cfg = configuration(accounts, aid, account)
    requests, events = [], []
    def route(request):
        requests.append(json.loads(request.content))
        return sse(search_result())
    accounts.http_transport = httpx.MockTransport(route)
    provider = await make_provider(cfg)
    provider.operation_purpose = role
    web = WebSession("auto", cfg, role, lambda k, p: events.append((k, p)))
    try:
        base = tool_definitions()
        text = "".join([c async for c in web.chat(provider, [{"role": "user", "content": "查阅官方说明"}], base)])
        assert text == "官方说明如下。"
        body = requests[0]
        assert body["store"] is False and body["stream"] is True
        assert body["tools"][-1] == {"type": "web_search", "external_web_access": True}
        names = {t["name"] for t in body["tools"][0]["tools"]}
        assert "web_search" not in names and "read_webpage" in names
        assert "web_search_call.action.sources" in body["include"]
        sources = provider.last_assistant_extra_fields["web_sources"]
        assert sources == [{"url": "https://example.org/docs", "title": "官方文档"}]
        assert events[-1] == ("web_sources", {"sources": sources})
        native = copy.deepcopy(provider.last_native_response)
        assistant = {"role": "assistant", "content": text, "_native_response": native}
        assert native["message_sha256"] == message_digest(assistant)
        assert not provider.last_tool_calls
        async for _ in web.chat(provider, [assistant, {"role": "user", "content": "继续"}], base):
            pass
        assert requests[1]["input"][0] == native["items"][0]
        assert requests[1]["input"][1]["content"][0]["annotations"]
    finally:
        await provider.close()


@pytest.mark.parametrize("mode,choice", [("off", "auto"), ("local", "auto"), ("auto", "none"), ("auto", {"type": "function", "function": {"name": "new_context"}})])
async def test_local_off_and_maintenance_requests_never_enable_hosted_search(tmp_path, mode, choice):
    accounts = IdentityFixture().service(tmp_path)
    aid, account = seed_account(accounts)
    cfg = configuration(accounts, aid, account)
    wire = []
    def route(request):
        wire.append(json.loads(request.content))
        return sse(completed())
    accounts.http_transport = httpx.MockTransport(route)
    provider = await make_provider(cfg)
    provider.operation_purpose = "card_author"
    web = WebSession(mode, cfg, "card_author")
    try:
        async for _ in web.chat(provider, [{"role": "user", "content": "test"}], tool_definitions(), extra_body={"tool_choice": choice}):
            pass
        assert all(tool["type"] != "web_search" for tool in wire[0].get("tools", []))
        names = {tool["name"] for group in wire[0].get("tools", []) for tool in group.get("tools", [])}
        if mode == "off":
            assert not names & {"web_search", "read_webpage"}
        if mode == "local":
            assert {"web_search", "read_webpage"} <= names
    finally:
        await provider.close()


async def test_cloud_search_failure_retries_only_current_request_and_discards_partial_output(tmp_path):
    accounts = IdentityFixture().service(tmp_path)
    aid, account = seed_account(accounts)
    cfg = configuration(accounts, aid, account)
    wire, events = [], []
    def route(request):
        wire.append(json.loads(request.content))
        if len(wire) == 1:
            return sse(events=[{"type": "response.output_text.delta", "delta": "未完成的内容"},
                {"type": "response.failed", "response": {"error": {"code": "web_search_unavailable"}}}])
        return sse(completed("", [("web_search", {"query": "官方说明"})]))
    accounts.http_transport = httpx.MockTransport(route)
    provider = await make_provider(cfg)
    provider.operation_purpose = "card_author"
    web = WebSession("auto", cfg, "card_author", lambda k, p: events.append((k, p)))
    messages = [{"role": "user", "content": "查证"},
                {"role": "assistant", "content": "", "tool_calls": [{"id": "prior-write", "function": {"name": "set_text_field", "arguments": "{}"}}]},
                {"role": "tool", "tool_call_id": "prior-write", "content": "草稿已暂存"}]
    try:
        chunks = [c async for c in web.chat(provider, messages, tool_definitions())]
        assert chunks == ["未完成的内容", None]
        assert web.failed and not web.cloud and len(wire) == 2
        assert wire[0]["input"] == wire[1]["input"]
        assert all(t["type"] != "web_search" for t in wire[1]["tools"])
        assert "web_search" in {t["name"] for t in wire[1]["tools"][0]["tools"]}
        assert [c.name for c in provider.last_tool_calls] == ["web_search"]
        assert [kind for kind, _ in events] == ["web_activity", "response_restarted"]
    finally:
        await provider.close()


@pytest.mark.parametrize("code", ["subscription_sharing_usage_limit_exceeded", "subscription_sharing_usage_unavailable", "chatpass_v2_scope_not_authorized"])
async def test_auth_and_model_quota_failures_do_not_fall_back(tmp_path, code):
    accounts = IdentityFixture().service(tmp_path)
    aid, account = seed_account(accounts)
    wire = []
    def route(request):
        wire.append(request)
        return sse(events=[{"type": "response.failed", "response": {"error": {"code": code}}}])
    accounts.http_transport = httpx.MockTransport(route)
    cfg = configuration(accounts, aid, account)
    provider = await make_provider(cfg)
    provider.operation_purpose = "advisor"
    web = WebSession("auto", cfg, "advisor")
    try:
        with pytest.raises(ValueError, match=code):
            async for _ in web.chat(provider, [{"role": "user", "content": "test"}], tool_definitions()):
                pass
        assert not web.failed and len(wire) == 1
    finally:
        await provider.close()


async def test_other_agent_cannot_enable_search_through_request_flag(tmp_path):
    accounts = IdentityFixture().service(tmp_path)
    aid, account = seed_account(accounts)
    wire = []
    def route(request):
        wire.append(json.loads(request.content))
        return sse(completed())
    accounts.http_transport = httpx.MockTransport(route)
    provider = await make_provider(configuration(accounts, aid, account))
    provider.operation_purpose = "tracker"
    try:
        await provider.chat_complete([{"role": "user", "content": "test"}], _web_search=True, _web_role="advisor")
        assert not wire[0].get("tools")
    finally:
        await provider.close()


def test_provider_switch_preserves_portable_source_urls():
    from tavern.scripting.native_response import prepare_messages
    message = {"role": "assistant", "content": "已查证", "_response_origin": {"api": "responses", "model": "old"},
               "web_sources": [{"title": "官方资料", "url": "https://example.org/source"}]}
    replay, invalid = prepare_messages([message], {"api": "anthropic", "model": "new"})
    assert invalid and "https://example.org/source" in replay[0]["content"]
    assert message["content"] == "已查证"
