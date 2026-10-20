"""Web source fidelity, compressed responses and unavailable searches."""

import gzip

import httpx
import pytest

from tavern.search.retrieval_config import Network
from tavern.web.client import WebTools
from tavern.web.config import WebConfig


@pytest.fixture
def web_factory(monkeypatch):
    async def verified(url):
        return url

    monkeypatch.setattr("tavern.web.client.public_url", verified)

    def factory(handler, **options):
        return WebTools(
            WebConfig(exa={"enabled": False}, tavily={"enabled": False}, **options),
            Network(api_proxy=False),
            httpx.MockTransport(handler),
        )

    return factory


async def test_search_returns_source_links_and_explicit_navigation_snippets(web_factory):
    def handler(request):
        assert request.url.params["q"] == "原著时间线"
        return httpx.Response(
            200,
            text='<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.org%2Ftimeline">时间线</a><a class="result__snippet">定位摘要</a>',
        )

    result = await web_factory(handler).search("原著时间线", 3)
    assert result["results"] == [
        {"title": "时间线", "url": "https://example.org/timeline", "snippet": "定位摘要"}
    ]


async def test_compressed_page_is_decoded_once_and_returned_as_lossless_pages(web_factory):
    source = (
        "<html><title>完整来源</title><body><p>"
        + ("很长的资料" * 20000)
        + "</p><script>不能执行</script><p>原文最后一行</p></body></html>"
    )
    client = web_factory(
        lambda r: httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8", "content-encoding": "gzip"},
            content=gzip.compress(source.encode()),
        )
    )
    result = await client.read("https://example.org/source")
    assert result["title"] == "完整来源"
    assert len(result["text"]) == 12000 and result["has_more"]
    pages = [result["text"]]
    while result["has_more"]:
        result = await client.read(
            "https://example.org/source", result["next_offset"], 30000, service=result["service"]
        )
        pages.append(result["text"])
    combined = "".join(pages)
    assert "很长的资料" * 20000 in combined
    assert combined.endswith("原文最后一行") and "不能执行" not in combined


async def test_unavailable_search_and_disabled_network_are_reported(web_factory):
    client = web_factory(lambda r: httpx.Response(202, text='<form id="challenge-form">verify</form>'))
    with pytest.raises(ValueError, match="验证"):
        await client.search("资料", 3)
    client.mode = "off"
    with pytest.raises(ValueError, match="关闭"):
        await client.read("https://example.org")


async def test_private_webpage_is_not_read():
    from tavern.web.client import public_url

    with pytest.raises(ValueError, match="公共"):
        await public_url("http://127.0.0.1/private")
    with pytest.raises(ValueError, match="HTTP"):
        await public_url("file:///private")
