"""Deterministic wire contracts, complete sources, fallback and cancellation."""
import asyncio
import json

import httpx
import pytest

from tavern.search.retrieval_config import Network
from tavern.web.client import EXA_MAX_CHARACTERS, WebTools, public_url
from tavern.web.config import WebConfig


@pytest.fixture
def make_web(monkeypatch):
    async def public(url):
        return url
    monkeypatch.setattr("tavern.web.client.public_url", public)
    def factory(handler, **config):
        return WebTools(WebConfig(**config), Network(api_proxy=False), httpx.MockTransport(handler))
    return factory


def exa_response(request, content, *, events=True, required_objective=True):
    if request.method == "DELETE":
        assert request.headers["mcp-session-id"] == "fixture-session"
        return httpx.Response(204)
    body = json.loads(request.content)
    method = body["method"]
    if method == "notifications/initialized":
        return httpx.Response(202)
    if method == "initialize":
        result = {"protocolVersion": "2025-03-26", "capabilities": {"tools": {}}}
    elif method == "tools/list":
        result = {"tools": [
            {"name": "web_search_exa", "inputSchema": {"properties": {"query": {}, **({"objective": {}} if required_objective else {})},
                "required": ["query", "objective"] if required_objective else ["query"]}},
            {"name": "web_fetch_exa", "inputSchema": {"required": ["urls"]}},
        ]}
    else:
        assert method == "tools/call"
        result = content(body["params"]) if callable(content) else content
    packet = {"jsonrpc": "2.0", "id": body["id"], "result": result}
    headers = {"mcp-session-id": "fixture-session"} if method == "initialize" else {}
    if events:
        return httpx.Response(200, headers={**headers, "content-type": "text/event-stream"},
                              text=f": keepalive\n\nevent: message\ndata: {json.dumps(packet)}\n\n")
    return httpx.Response(200, headers=headers, json=packet)


@pytest.mark.parametrize("mode,events,objective", [("keyless", True, None), ("api_key", False, "查证官方发布日期")])
async def test_exa_search_handshake_auth_discovery_and_release(make_web, mode, events, objective):
    requests = []
    complete = "Title: 官方资料\nURL: https://example.org/source\nHighlights:\n" + "摘要" * 4000 + "摘要尾部"
    def content(params):
        assert params["name"] == "web_search_exa"
        assert params["arguments"] == {"query": "原著", "numResults": 3, "objective": objective or "原著"}
        return {"content": [{"type": "text", "text": complete}]}
    def route(request):
        requests.append(request)
        assert request.headers.get("x-api-key") == ("fixture-exa-key" if mode == "api_key" else None)
        assert "fixture-exa-key" not in str(request.url)
        return exa_response(request, content, events=events)
    web = make_web(route, exa={"auth_mode": mode, "api_key": "fixture-exa-key"})
    result = await web.search("原著", 3, objective)
    assert result["service"] == "exa" and result["auth_mode"] == mode
    assert result["text"] == complete and result["results"][0]["url"] == "https://example.org/source"
    assert [json.loads(r.content)["method"] for r in requests[:-1]] == ["initialize", "notifications/initialized", "tools/list", "tools/call"]
    assert requests[-1].method == "DELETE"
    assert "fixture-session" not in json.dumps(result)


@pytest.mark.parametrize("mode", ["keyless", "api_key"])
async def test_tavily_search_and_extract_auth_and_full_text(make_web, mode):
    seen = []
    text = "完整正文" * 10000 + "网页原文尾部"
    def route(request):
        seen.append(request)
        assert request.headers.get("authorization") == ("Bearer fixture-tavily-key" if mode == "api_key" else None)
        assert request.headers.get("x-tavily-access-mode") == ("keyless" if mode == "keyless" else None)
        assert "fixture-tavily-key" not in str(request.url)
        body = json.loads(request.content)
        if request.url.path == "/search":
            assert body["search_depth"] == "basic" and not body["include_answer"]
            return httpx.Response(200, json={"results": [{"url": "https://example.org", "title": "来源", "content": "定位摘要", "id": "private-id"}], "request_id": "opaque-request-id"})
        assert body == {"urls": ["https://example.org"], "format": "markdown", "extract_depth": "basic"}
        return httpx.Response(200, json={"results": [{"url": "https://example.org", "raw_content": text}], "failed_results": []})
    web = make_web(route, exa={"enabled": False}, tavily={"auth_mode": mode, "api_key": "fixture-tavily-key"})
    search = await web.search("query", 5)
    assert search["results"] == [{"url": "https://example.org", "title": "来源", "snippet": "定位摘要"}]
    assert "private-id" not in json.dumps(search) and "opaque-request-id" not in json.dumps(search)
    assert (await web.read("https://example.org"))["text"] == text
    assert len(seen) == 2


async def test_exa_fetch_explicit_full_content_limit(make_web):
    source = "全文" * 5000 + "末尾必须保留"
    def content(params):
        assert params == {"name": "web_fetch_exa", "arguments": {"urls": ["https://example.org"], "maxCharacters": EXA_MAX_CHARACTERS}}
        return {"content": [{"type": "text", "text": "# 来源\nURL: https://example.org\n\n" + source}]}
    result = await make_web(lambda r: exa_response(r, content)).read("https://example.org")
    assert result["service"] == "exa" and result["text"].endswith(source)


async def test_truncated_extraction_falls_back_without_returning_partial_text(make_web):
    def route(request):
        if request.url.host == "mcp.exa.ai":
            return exa_response(request, {"content": [{"type": "text", "text": "部分正文 [content truncated]"}]})
        if request.url.host == "api.tavily.com":
            return httpx.Response(200, json={"failed_results": [{"url": "https://example.org"}], "results": []})
        return httpx.Response(200, headers={"content-type": "text/html"}, text="<p>完整读取尾部</p>")
    result = await make_web(route).read("https://example.org")
    assert result["service"] == "local" and result["text"] == "完整读取尾部"
    assert [a["status"] for a in result["attempts"]] == ["error", "error", "completed"]
    assert "部分正文" not in json.dumps(result)


async def test_bounded_ordered_fallback_logs_reasons_and_keeps_zero_results(make_web):
    seen, events = [], []
    def route(request):
        seen.append(request.url.host)
        if request.url.host == "mcp.exa.ai":
            return httpx.Response(429, text="private fixture-exa-key")
        if request.url.host == "api.tavily.com":
            return httpx.Response(200, json={"results": []})
        pytest.fail("A valid empty result must not invoke a lower-priority service")
    web = make_web(route, exa={"auth_mode": "api_key", "api_key": "fixture-exa-key"})
    web.event = lambda kind, payload: events.append((kind, payload))
    result = await web.search("zero", 1)
    assert result["results"] == [] and seen == ["mcp.exa.ai", "api.tavily.com"]
    assert result["attempts"][0]["reason"] == "服务限流或额度已用完"
    assert [p["status"] for _, p in events] == ["started", "error", "started", "completed"]
    assert "fixture-exa-key" not in json.dumps([result, events])


async def test_all_services_fail_once_and_reordered_legacy_service_is_used(make_web):
    seen = []
    def route(request):
        seen.append(request.url.host)
        return httpx.Response(503)
    web = make_web(route, searxng={"endpoint": "https://search.example.org"})
    with pytest.raises(ValueError, match="所有联网服务均不可用"):
        await web.search("query", 1)
    assert seen == ["mcp.exa.ai", "api.tavily.com", "search.example.org", "html.duckduckgo.com"]
    web.config.order = ["searxng", "tavily", "exa", "duckduckgo"]
    web.transport = httpx.MockTransport(lambda r: httpx.Response(200, json={"results": []}))
    assert (await web.search("zero", 1))["service"] == "searxng"


async def test_disabled_permission_rejects_search_and_read_before_network(make_web):
    web = make_web(lambda r: pytest.fail("Disabled networking must not dispatch"))
    web.mode = "off"
    for call in (lambda: web.search("q", 1), lambda: web.read("https://example.org")):
        with pytest.raises(ValueError, match="关闭"):
            await call()


async def test_cancellation_never_falls_back(make_web):
    entered = asyncio.Event()
    seen = []
    async def route(request):
        seen.append(request.url.host)
        entered.set()
        await asyncio.Future()
    task = asyncio.create_task(make_web(route).search("q", 1))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert seen == ["mcp.exa.ai"]


async def test_private_urls_and_redirects_are_rejected():
    for url in ("http://127.0.0.1/secret", "http://[::1]/secret", "http://user:password@example.org", "file:///tmp/secret"):
        with pytest.raises(ValueError):
            await public_url(url)
    calls = []
    def redirect(request):
        calls.append(str(request.url))
        return httpx.Response(302, headers={"location": "http://127.0.0.1/secret"})
    web = WebTools(WebConfig(exa={"enabled": False}, tavily={"enabled": False}), Network(api_proxy=False), httpx.MockTransport(redirect))
    with pytest.raises(ValueError, match="公共"):
        await web.read("https://1.1.1.1/source")
    assert calls == ["https://1.1.1.1/source"]


async def test_malformed_mcp_and_tavily_responses_are_not_empty_success(make_web):
    def route(request):
        if request.url.host == "mcp.exa.ai":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": "wrong-id", "result": {}})
        return httpx.Response(200, json={"oops": []})
    web = make_web(route, duckduckgo={"enabled": False})
    with pytest.raises(ValueError, match="响应"):
        await web.search("query", 1)


async def test_exa_character_cap_and_oversized_decoded_page_are_not_returned(make_web, monkeypatch):
    monkeypatch.setattr("tavern.web.client.EXA_MAX_CHARACTERS", 100)
    def route(request):
        if request.url.host == "mcp.exa.ai":
            return exa_response(request, {"content": [{"type": "text", "text": "a" * 100}]})
        return httpx.Response(200, headers={"content-type": "text/plain"}, content=b"x" * (8 * 1024 * 1024 + 1))
    web = make_web(route, tavily={"enabled": False})
    with pytest.raises(ValueError, match="8 MiB") as error:
        await web.read("https://example.org")
    assert "截断" in str(error.value)


async def test_invalid_result_url_falls_back_and_known_keys_never_enter_results(make_web):
    events = []
    def route(request):
        if request.url.host == "api.tavily.com":
            return httpx.Response(200, json={"results": [{"url": "https://[invalid", "title": "来源", "content": "摘要"}]})
        return httpx.Response(200, text='<a class="result__a" href="https://example.org">fixture-key</a><a class="result__snippet">摘要</a>')
    web = make_web(route, exa={"enabled": False}, tavily={"api_key": "fixture-key"})
    web.event = lambda kind, payload: events.append((kind, payload))
    result = await web.search("q", 1)
    assert result["service"] == "duckduckgo" and result["attempts"][0]["status"] == "error"
    assert "fixture-key" not in json.dumps([result, events])


async def test_total_service_timeout_moves_to_next_candidate(make_web):
    seen = []
    async def route(request):
        seen.append(request.url.host)
        if request.url.host == "mcp.exa.ai":
            await asyncio.Future()
        return httpx.Response(200, json={"results": []})
    web = make_web(route)
    web.config = web.config.model_copy(update={"timeout_seconds": .01})
    result = await web.search("q", 1)
    assert result["service"] == "tavily" and "超时" in result["attempts"][0]["reason"]
    assert seen == ["mcp.exa.ai", "api.tavily.com"]
