"""Portable public-web tools independent of the selected model vendor."""
import asyncio
import json
import re
from html.parser import HTMLParser
from urllib.parse import parse_qs, urljoin, urlsplit

import httpx

from tavern.web.mcp import ExaMCP
from tavern.web.results import READ_DEFAULT_LIMIT, exa_search_rows, read_result, search_result
from tavern.web.sources import protect_secrets, source_links
from tavern.web.tools import WebRead, WebSearch
from tavern.web.transport import PublicHTTPTransport, public_addresses

MAX_BYTES = 8 * 1024 * 1024
# This upstream extraction ceiling is distinct from the much smaller returned page.
EXA_MAX_CHARACTERS = 1_000_000


class PageText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.title, self.hidden, self.in_title = [], [], [], False

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript", "template"}:
            self.hidden.append(tag)
        if tag == "title":
            self.in_title = True
        if not self.hidden and tag in {"p", "div", "li", "br", "h1", "h2", "h3", "tr", "section"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if self.hidden and self.hidden[-1] == tag:
            self.hidden.pop()
        if tag == "title":
            self.in_title = False
        if not self.hidden and tag in {"p", "div", "li", "h1", "h2", "h3", "tr"}:
            self.parts.append("\n")

    def handle_data(self, data):
        if self.in_title:
            self.title.append(data)
        if not self.hidden:
            self.parts.append(data)

    def result(self):
        return "\n".join(line.strip() for line in "".join(self.parts).splitlines() if line.strip())


class SearchResults(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows, self.current, self.label, self.snippet = [], None, False, False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = attrs.get("class", "").split()
        if tag == "a" and "result__a" in classes:
            link = urljoin("https://duckduckgo.com", attrs.get("href", ""))
            link = parse_qs(urlsplit(link).query).get("uddg", [link])[0]
            self.current = {"title": "", "url": link, "snippet": ""}
            self.rows.append(self.current)
            self.label = True
        if "result__snippet" in classes:
            self.snippet = True

    def handle_endtag(self, tag):
        if tag == "a":
            self.label = False
            self.snippet = False

    def handle_data(self, data):
        if self.current and self.label:
            self.current["title"] += data
        elif self.current and self.snippet:
            self.current["snippet"] += data


async def public_url(url):
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("网页地址必须是无凭据的 HTTP(S) 公共网址")
    await public_addresses(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80))
    return url


class WebTools:
    def __init__(self, config, network, transport=None, *, mode="auto", event=None):
        self.config, self.network, self.transport = config, network, transport
        self.mode, self.event = mode, event or (lambda kind, payload: None)
        self.serial = 0

    def require_enabled(self):
        if self.mode == "off":
            raise ValueError("联网查询已关闭")

    def client(self, *, configured=False):
        proxy = (self.network.proxy or None) if self.network.api_proxy and self.transport is None else None
        transport = self.transport
        if transport is None and proxy is None and not configured:
            transport = PublicHTTPTransport()
        return httpx.AsyncClient(proxy=proxy,
            trust_env=False, timeout=self.config.timeout_seconds, transport=transport,
            headers={"User-Agent": "TerrariumTavern/1.0 (reference reader)"})

    async def request(self, client, method, url, *, params=None, body=None, headers=None, configured=False):
        self.require_enabled()
        if not configured:
            await public_url(url)
        async with client.stream(method, url, params=params, json=body, headers=headers) as response:
            response.raise_for_status()
            chunks, size = [], 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > MAX_BYTES:
                    raise ValueError("网页或服务响应超过 8 MiB 读取上限，未返回截断内容")
                chunks.append(chunk)
            copied = {k: v for k, v in response.headers.items() if k not in {"content-encoding", "content-length"}}
            return httpx.Response(response.status_code, headers=copied, content=b"".join(chunks), request=response.request)

    async def fetch(self, url, *, params=None, configured=False):
        self.require_enabled()
        # Only the user-configured search endpoint may be on a private LAN.
        if not configured:
            await public_url(url)
        for _ in range(6):
            # The configured LAN exemption applies only to the first hop.
            async with self.client(configured=configured) as client:
                async with client.stream("GET", url, params=params) as response:
                    if response.is_redirect:
                        url = await public_url(urljoin(str(response.url), response.headers.get("location", "")))
                        params = None
                        configured = False
                        continue
                    response.raise_for_status()
                    chunks, size = [], 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > MAX_BYTES:
                            raise ValueError("网页超过 8 MiB 读取上限，未返回截断内容")
                        chunks.append(chunk)
                    headers = {k: v for k, v in response.headers.items() if k not in {"content-encoding", "content-length"}}
                    return httpx.Response(response.status_code, headers=headers, content=b"".join(chunks), request=response.request)
        raise ValueError("网页重定向次数过多")

    async def legacy_search(self, name, query, limit):
        if name == "searxng":
            endpoint = self.config.searxng.endpoint
            response = await self.fetch(endpoint.rstrip("/") + "/search", params={"q": query, "format": "json"}, configured=True)
            if not isinstance(response.json().get("results"), list):
                raise ValueError("SearXNG 搜索响应无效")
            rows = [{"title": r.get("title", ""), "url": r["url"], "snippet": r.get("content", "")}
                    for r in response.json().get("results", []) if r.get("url")]
        else:
            response = await self.fetch("https://html.duckduckgo.com/html/", params={"q": query})
            parser = SearchResults()
            parser.feed(response.text)
            rows = parser.rows
            if not rows and ("anomaly" in response.text.lower() or "challenge" in response.text.lower()):
                raise ValueError("搜索服务要求验证，未取得搜索结果；可重试或在设置中切换 SearXNG")
        return {"query": query, "results": rows}

    async def local_read(self, url):
        response = await self.fetch(url)
        content_type = response.headers.get("content-type", "").lower()
        if "html" in content_type:
            parser = PageText()
            parser.feed(response.text)
            text, title = parser.result(), "".join(parser.title).strip()
        elif content_type.startswith("text/") or "json" in content_type:
            text, title = response.text, ""
        else:
            raise ValueError("此网页工具只读取 HTML 或文本，暂不支持二进制文件")
        if not text.strip():
            raise ValueError("网页没有可读取文本，可能需要登录或执行脚本")
        return {"url": str(response.url), "title": title, "text": text,
                "source_truncated": response.status_code == 206 or "content-range" in response.headers}

    def auth(self, name):
        config = getattr(self.config, name)
        if config.auth_mode == "api_key":
            if not config.api_key.strip():
                raise ValueError("自有 key 模式尚未配置密钥")
            return {"x-api-key": config.api_key} if name == "exa" else {"Authorization": "Bearer " + config.api_key}
        return {} if name == "exa" else {"X-Tavily-Access-Mode": "keyless"}

    def safe_error(self, error):
        # Never return a response body or a request header from an upstream error.
        if isinstance(error, httpx.TimeoutException):
            return "服务请求超时"
        if isinstance(error, httpx.HTTPStatusError):
            code = error.response.status_code
            return {401: "密钥无效或未获授权", 403: "服务拒绝访问", 402: "服务额度不足",
                    429: "服务限流或额度已用完"}.get(code, f"服务 HTTP {code}")
        if isinstance(error, httpx.HTTPError):
            return "服务网络连接失败"
        if isinstance(error, (json.JSONDecodeError, KeyError, TypeError, AttributeError)):
            return "服务响应格式无效"
        text = str(error)
        for name in ("exa", "tavily"):
            key = getattr(self.config, name).api_key
            if key:
                text = text.replace(key, "[REDACTED]")
        return text if isinstance(error, ValueError) else "服务请求失败"

    async def attempt(self, operation, call, *, transform, service=None):
        self.require_enabled()
        candidates = [name for name in self.config.order if getattr(self.config, name).enabled
                      and (name != "searxng" or self.config.searxng.endpoint.strip())
                      and (operation == "search" or name in {"exa", "tavily"})]
        if operation == "read":
            candidates.append("local")
        if service is not None:
            if service not in candidates:
                raise ValueError("指定的网页提取服务未启用，不能续读；请从 offset=0 重新选择来源")
            candidates = [service]
        attempts = []
        for name in candidates:
            self.serial += 1
            auth_mode = getattr(getattr(self.config, name, None), "auth_mode", "none")
            info = {"operation": operation, "service": name, "auth_mode": auth_mode, "action_id": f"local-web-{self.serial}"}
            self.event("web_activity", {**info, "status": "started"})
            try:
                # One timeout bounds the entire MCP handshake and call, not each packet.
                async with asyncio.timeout(self.config.timeout_seconds):
                    result = await call(name)
                    # Redact before slicing so a page boundary cannot expose pieces of a credential.
                    result = protect_secrets(result, (self.config.exa.api_key, self.config.tavily.api_key))
                    result = transform(result)
                rows = result.get("results", []) if operation == "search" else [result]
                links = source_links(rows, strict=True)
            except TimeoutError:
                reason = "服务请求超时"
            except Exception as error:
                reason = self.safe_error(error)
            else:
                attempts.append({**info, "status": "completed"})
                self.event("web_activity", {**info, "status": "completed", "attempts": attempts})
                if links:
                    self.event("web_sources", {"sources": links})
                # Activity correlation is transport metadata, not model business data.
                model_attempts = [{k: v for k, v in row.items() if k != "action_id"} for row in attempts]
                return {**result, "service": name, "auth_mode": auth_mode, "attempts": model_attempts}
            attempts.append({**info, "status": "error", "reason": reason})
            self.event("web_activity", {**info, "status": "error", "reason": reason})
        prefix = "指定网页提取服务不可用；如需换服务，请从 offset=0 重读：" if service else "所有联网服务均不可用："
        raise ValueError(prefix + ("；".join(f"{a['service']}：{a['reason']}" for a in attempts) or "未启用搜索服务"))

    async def search(self, query, limit, objective=None):
        self.require_enabled()
        args = WebSearch(query=query, limit=limit, objective=objective)
        async def call(name):
            if name not in {"exa", "tavily"}:
                return await self.legacy_search(name, query, limit)
            async with self.client() as client:
                if name == "exa":
                    mcp = ExaMCP(client, self.request, self.auth(name))
                    payload = await mcp.call("web_search_exa", {"query": query, "numResults": limit}, objective=objective or query)
                    return {"query": query, "results": exa_search_rows(mcp.text(payload))}
                response = await self.request(client, "POST", "https://api.tavily.com/search", headers=self.auth(name),
                    body={"query": query, "max_results": limit, "search_depth": "basic",
                          "include_answer": False, "include_raw_content": False})
                data = response.json()
                if not isinstance(data.get("results"), list):
                    raise ValueError("Tavily 搜索响应无效")
                rows = [{"title": row.get("title", ""), "url": row["url"], "snippet": row.get("content", "")}
                        for row in data["results"]]
                if any(not isinstance(row["title"], str) or not isinstance(row["snippet"], str) for row in rows):
                    raise ValueError("Tavily 搜索摘要格式无效")
                return {"query": query, "results": rows}
        return await self.attempt("search", call, transform=lambda result: search_result(result, args.limit))

    async def read(self, url, offset=0, limit=READ_DEFAULT_LIMIT, *, service=None):
        self.require_enabled()
        args = WebRead(url=url, offset=offset, limit=limit, service=service)
        await public_url(url)
        async def call(name):
            if name == "local":
                return await self.local_read(url)
            async with self.client() as client:
                if name == "exa":
                    mcp = ExaMCP(client, self.request, self.auth(name))
                    payload = await mcp.call("web_fetch_exa", {"urls": [url], "maxCharacters": EXA_MAX_CHARACTERS})
                    text = mcp.text(payload)
                    if not text.strip() or text.startswith("No content found"):
                        raise ValueError("Exa 未返回网页正文")
                    truncated = (len(text) >= EXA_MAX_CHARACTERS
                                 or bool(re.search(r"(?i)\[(?:content )?truncated\]|\[truncated at", text))
                                 or payload.get("truncated") is True)
                    return {"url": url, "text": text, "source_truncated": True if truncated else None}
                response = await self.request(client, "POST", "https://api.tavily.com/extract", headers=self.auth(name),
                    body={"urls": [url], "format": "markdown", "extract_depth": "basic"})
                data = response.json()
                rows = data.get("results")
                if data.get("failed_results") or not isinstance(rows, list) or len(rows) != 1:
                    raise ValueError("Tavily 未能提取此网页")
                row = rows[0]
                text = row.get("raw_content")
                if not isinstance(text, str) or not text.strip():
                    raise ValueError("Tavily 未返回网页正文")
                await public_url(row["url"])
                truncated = row.get("truncated") if type(row.get("truncated")) is bool else None
                return {"url": row["url"], "text": text, "source_truncated": truncated}
        return await self.attempt("read", call, transform=lambda result: read_result(result, args.offset, args.limit),
                                  service=args.service)
