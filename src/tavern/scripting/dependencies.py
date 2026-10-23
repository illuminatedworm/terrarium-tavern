"""Download explicitly referenced resources through the configured proxy, then pin hashes."""

import asyncio
import base64
import hashlib
import ipaddress
import json
import re
from collections import OrderedDict
from contextlib import aclosing
from io import BytesIO
from urllib.parse import unquote, urljoin, urlparse
from xml.etree import ElementTree

import httpx
from PIL import Image

from tavern.character.cards import card_scripts, diagnose
from tavern.scripting.bundler import DependencyBundler
from tavern.scripting.image_resources import TEMPLATE, declared_image, rewrite_markup
from tavern.web.transport import PublicHTTPTransport, public_addresses

IMPORTS = re.compile(r"""(?:\bfrom\s*|\bimport\s*\(?\s*|\bexport\s+[^;]*?\bfrom\s*)["']([^"']+)["']""")
CSS_URLS = re.compile(r"""url\(\s*(?:["']([^"']*)["']|([^\s)]*))\s*\)|@import\s+["']([^"']+)["']""", re.I)


def css_urls(source):
    for match in CSS_URLS.finditer(source):
        url = next(value for value in match.groups() if value is not None)
        if url and not url.startswith(("data:", "blob:", "#")):
            yield url


PRIVATE_NETWORK = "卡片不能访问本地或私有网络资源"


async def validate_remote(url):
    """Static URL checks; direct connections resolve once in PublicHTTPTransport."""
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.port not in (None, 443)
    ):
        raise ValueError("卡片远程资源仅允许公开 HTTPS 地址")
    try:
        literal = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        return
    if not literal.is_global:
        raise ValueError(PRIVATE_NETWORK)


async def download_resource(client, url, *, proxied=False):
    current = url
    for _ in range(5):
        await validate_remote(current)
        if proxied:
            # The configured proxy chooses the final address; this local lookup
            # is the best available pre-check and cannot pin that connection.
            await public_addresses(urlparse(current).hostname, 443, PRIVATE_NETWORK)
        async with client.stream("GET", current) as response:
            if response.is_redirect:
                current = urljoin(current, response.headers["location"])
                continue
            response.raise_for_status()
            buffer = bytearray()
            async for part in response.aiter_bytes():
                buffer.extend(part)
                if len(buffer) > 8 * 1024 * 1024:
                    raise ValueError("单个卡片依赖超过 8 MB")
            return bytes(buffer), response.headers.get("content-type", "application/octet-stream"), current
    raise ValueError("依赖重定向次数过多")


class Dependencies:
    def __init__(self, store):
        self.store = store
        (store.root / "dependencies").mkdir(exist_ok=True)
        self.lock = asyncio.Lock()
        # Dynamic galleries must not grow the pinned dependency manifest indefinitely.
        # These derived images expire with this runtime; story/card data is untouched.
        self.images = OrderedDict()
        self.image_bytes = 0
        self.image_tasks = {}
        self.image_waiters = {}
        self.image_slots = asyncio.Semaphore(4)
        # Images reuse keep-alive connections per proxy setting; resolve() keeps
        # its one-shot client. Closed with this runtime.
        self.image_clients = {}

    async def close(self):
        for task in self.image_tasks.values():
            task.cancel()
        await asyncio.gather(*self.image_tasks.values(), return_exceptions=True)
        self.image_tasks.clear()
        self.image_waiters.clear()
        self.images.clear()
        self.image_bytes = 0
        clients, self.image_clients = list(self.image_clients.values()), {}
        await asyncio.gather(*(client.aclose() for client in clients))

    def proxy(self):
        config = self.store.settings()
        return (config.network.proxy or None) if config.network.resources_proxy else None

    def client(self, proxy=None):
        # Direct connections use the checked addresses themselves (no DNS rebinding window).
        options = {"proxy": proxy} if proxy else {"transport": PublicHTTPTransport(PRIVATE_NETWORK)}
        return httpx.AsyncClient(**options, trust_env=False, timeout=30, follow_redirects=False)

    def declarations(self, card):
        declarations = diagnose(card["document"])["external_dependencies"]
        for resource in self.read(card)["resources"].values():
            if "text/css" in resource["content_type"]:
                css = (self.store.root / "blobs" / resource["hash"]).read_text(encoding="utf-8")
                declarations.extend(urljoin(resource["url"], child) for child in css_urls(css))
        return declarations

    async def image(self, card, url, declarations=None):
        if not card["permission"]:
            raise ValueError("请先启用此卡脚本权限")
        if declarations is None:
            declarations = self.declarations(card)
        if len(url) > 8192 or not declared_image(url, declarations):
            raise ValueError("图片地址不属于此卡声明的资源、模板或目录")
        if url in self.images:
            self.images.move_to_end(url)
            return self.images[url]
        task = self.image_tasks.get(url)
        if task is None:
            task = self.image_tasks[url] = asyncio.create_task(self._image(card, url))
        self.image_waiters[url] = self.image_waiters.get(url, 0) + 1
        try:
            return await asyncio.shield(task)
        finally:
            remaining = self.image_waiters.get(url, 1) - 1
            if remaining:
                self.image_waiters[url] = remaining
            else:
                self.image_waiters.pop(url, None)
                self.image_tasks.pop(url, None)
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

    async def _image(self, card, url):
        async with self.image_slots:
            if url in self.images:
                self.images.move_to_end(url)
                return self.images[url]
            resource = self.read(card)["resources"].get(url)
            if resource:
                raw = (self.store.root / "blobs" / resource["hash"]).read_bytes()
            else:
                proxy = self.proxy()
                if proxy not in self.image_clients:
                    self.image_clients[proxy] = self.client(proxy)
                raw, _, _ = await download_resource(self.image_clients[proxy], url, proxied=bool(proxy))
            # Never return arbitrary HTML/JavaScript through the image bridge.
            try:
                if b"<svg" in raw[:1024]:
                    if ElementTree.fromstring(raw).tag not in {"svg", "{http://www.w3.org/2000/svg}svg"}:
                        raise ValueError("图片不是有效的 SVG")
                    mime = "image/svg+xml"
                else:
                    with Image.open(BytesIO(raw)) as image:
                        mime = Image.MIME[image.format]
                        image.verify()
            except (OSError, ElementTree.ParseError, Image.DecompressionBombError) as error:
                raise ValueError("图片内容无效：" + url) from error
            self.images[url] = (raw, mime)
            self.image_bytes += len(raw)
            while len(self.images) > 128 or self.image_bytes > 32 * 1024 * 1024:
                _, (expired, _) = self.images.popitem(last=False)
                self.image_bytes -= len(expired)
            return raw, mime

    def remote_images(self, html):
        """Remote image URLs of real tags in document order, so loading follows reading."""
        urls = {}

        def collect(url):
            if (
                url.startswith("https://")
                and not TEMPLATE.search(url)
                and not re.search(r"\.(?:css|js|mjs|woff2?|ttf|otf|eot)(?:[?#]|$)", url, re.I)
            ):
                urls[url] = None
            return url

        rewrite_markup(html, collect, images_only=True)
        if len(urls) > 128:
            raise ValueError("单个内容块最多加载 128 张远程图片")
        return list(urls)

    async def stream_images(self, card, urls):
        """Yield (url, data URL, error) as each image finishes, started in document order.

        Each image fails on its own so readers still see the rest. Exceeding the
        block's 32 MiB budget ends the stream; closing it cancels unfinished
        downloads (image() keeps one alive only while another request waits).
        """
        declarations = self.declarations(card)

        async def load(url):
            try:
                return url, await self.image(card, url, declarations), None
            except (ValueError, httpx.HTTPError) as error:
                return url, None, error

        tasks = [asyncio.create_task(load(url)) for url in urls]
        total = 0
        try:
            for next_image in asyncio.as_completed(tasks):
                url, result, error = await next_image
                if error is None:
                    total += len(result[0])
                    if total > 32 * 1024 * 1024:
                        yield url, None, ValueError("单个内容块的远程图片原始数据超过 32 MiB")
                        return
                    result = f"data:{result[1]};base64,{base64.b64encode(result[0]).decode()}"
                yield url, result, error
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def prepare_markup(self, card, html):
        """Script frames mount once, so they wait for every image; prose streams instead."""
        loaded = {}
        async with aclosing(self.stream_images(card, self.remote_images(html))) as images:
            async for url, data, error in images:
                if error is not None:
                    raise error
                loaded[url] = data
        html = rewrite_markup(html, lambda url: loaded.get(url, url), images_only=True)
        return self.markup(card, html)

    def manifest_path(self, card):
        return self.store.card_manifest_path(card)

    def read(self, card):
        path = self.manifest_path(card)
        manifest = (
            json.loads(path.read_text(encoding="utf-8")) if path.exists() else self.store.card_manifest(card)
        )
        return manifest

    async def resolve(self, card):
        async with self.lock:
            return await self._resolve(card)

    async def _resolve(self, card):
        if not card["permission"]:
            raise ValueError("请先启用此卡脚本权限")
        manifest = self.read(card)
        proxy = self.proxy()
        async with self.client(proxy) as client, DependencyBundler() as bundler:
            visited = set()

            async def fetch(url, depth=0):
                if TEMPLATE.search(unquote(url)):
                    return  # Resolve image/CSS templates only after their captured values exist.
                if depth > 10:
                    raise ValueError("依赖层数或数量超限")
                if url in visited:
                    return
                visited.add(url)
                if url in manifest["resources"]:
                    resource = manifest["resources"][url]
                    if "text/css" in resource["content_type"] or urlparse(resource["url"]).path.endswith(
                        ".css"
                    ):
                        source = (self.store.root / "blobs" / resource["hash"]).read_text(encoding="utf-8")
                        for child in css_urls(source):
                            await fetch(urljoin(resource["url"], child), depth + 1)
                    return
                if len(manifest["resources"]) >= 100:
                    raise ValueError("依赖层数或数量超限")
                # This app already hosts the pinned MVU core. Never boot a second ST framework.
                if re.search(r"/gh/MagicalAstrogy/MagVarUpdate(?:@[^/]+)?/artifact/bundle\.js$", url, re.I):
                    digest = self.store.blob(b"export default globalThis.Mvu;")
                    manifest["resources"][url] = {
                        "hash": digest,
                        "url": url,
                        "content_type": "application/javascript",
                        "provided_by": "pinned MVU runtime",
                    }
                    return
                raw, content_type, current = await download_resource(client, url, proxied=bool(proxy))
                digest = self.store.blob(raw)
                manifest["resources"][url] = {"hash": digest, "url": current, "content_type": content_type}
                if "text/css" in content_type or urlparse(current).path.endswith(".css"):
                    for child in css_urls(raw.decode("utf-8")):
                        await fetch(urljoin(current, child), depth + 1)
                if "javascript" in content_type or urlparse(current).path.endswith((".js", ".mjs")):
                    for child in IMPORTS.findall(raw.decode("utf-8")):
                        if child in ("lodash", "jquery", "zod", "yaml"):
                            continue
                        await fetch(urljoin(current, child), depth + 1)

            for url in diagnose(card["document"])["external_dependencies"]:
                # Download assets and modules; ordinary creator links are not runtime dependencies.
                if re.search(
                    r"\.(?:js|mjs|css|png|jpe?g|webp|gif|svg|ico|woff2?|ttf|otf|eot)(?:[?#]|$)", url
                ):
                    await fetch(url)
            for script in card_scripts(card["document"]):
                source = script.get("content", "")
                for url in IMPORTS.findall(source):
                    if url.startswith("https://"):
                        await fetch(url)
                if not IMPORTS.search(source):
                    continue
                digest = hashlib.sha256(source.encode()).hexdigest()
                if (
                    digest in manifest["scripts"]
                    and (self.store.root / "blobs" / manifest["scripts"][digest]).exists()
                ):
                    continue
                modules = {
                    url: self.store.root / "blobs" / resource["hash"]
                    for url, resource in manifest["resources"].items()
                }
                output = await bundler.bundle(source, modules)
                manifest["scripts"][digest] = self.store.blob(output.encode("utf-8"))
        self.manifest_path(card).write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        if card.get("session_id"):
            self.store.cache_card_dependencies(card["id"], manifest)
        return manifest

    def scripts(self, card):
        manifest = self.read(card)
        result = []
        for script in card_scripts(card["document"]):
            source = script.get("content", "")
            digest = hashlib.sha256(source.encode()).hexdigest()
            if digest in manifest["scripts"]:
                script = {
                    **script,
                    "content": (self.store.root / "blobs" / manifest["scripts"][digest]).read_text(
                        encoding="utf-8"
                    ),
                    "bundled": True,
                }
            result.append(script)
        return result

    def markup(self, card, html):
        manifest = self.read(card)
        assets = dict(card.get("resources", {}))
        declarations = diagnose(card["document"])["external_dependencies"]

        def content(url):
            local = url.removeprefix("embeded://").removeprefix("__asset:")
            digest = assets.get(local) or manifest["resources"].get(url, {}).get("hash")
            if not digest:
                raise ValueError(f"卡片资源尚未缓存：{url}")
            import mimetypes

            mime = (
                manifest["resources"].get(url, {}).get("content_type")
                or mimetypes.guess_type(local)[0]
                or "application/octet-stream"
            )
            return (self.store.root / "blobs" / digest).read_bytes(), mime

        def script(match):
            raw, _ = content(match[2])
            source = raw.decode("utf-8")
            if IMPORTS.search(source):
                raise ValueError("消息界面中的 ES 模块需作为角色模块导入；尚不能内联")
            return "<script>" + source.replace("</script", "<\\/script") + "</script>"

        html = re.sub(
            r"""<script\b[^>]*\bsrc\s*=\s*(["'])(.*?)\1[^>]*>\s*</script\s*>""", script, html, flags=re.I
        )

        def inline_css(source, base, stack=()):
            def replace(match):
                url = next(value for value in match.groups() if value is not None)
                if not url or url.startswith(("data:", "blob:", "#")):
                    return match[0]
                absolute = urljoin(base, url)
                if TEMPLATE.search(unquote(absolute)):
                    return f'url("{absolute}")'
                raw, mime = content(absolute)
                if "text/css" in mime or urlparse(absolute).path.endswith(".css"):
                    if absolute in stack:
                        raise ValueError("CSS 依赖循环")
                    resource = manifest["resources"].get(absolute, {})
                    raw = inline_css(
                        raw.decode("utf-8"), resource.get("url", absolute), (*stack, absolute)
                    ).encode()
                data = f"data:{mime.split(';')[0]};base64,{base64.b64encode(raw).decode()}"
                return f'@import "{data}"' if match[3] is not None else f'url("{data}")'

            return CSS_URLS.sub(replace, source)

        # Older messages already contain cached CSS, but its font URLs were left relative.
        for url, resource in manifest["resources"].items():
            if "text/css" in resource["content_type"]:
                original = (self.store.root / "blobs" / resource["hash"]).read_text(encoding="utf-8")
                if original in html:
                    html = html.replace(original, inline_css(original, resource.get("url", url)))

        def stylesheet(match):
            raw, _ = content(match[2])
            base = manifest["resources"].get(match[2], {}).get("url", match[2])
            return "<style>" + inline_css(raw.decode("utf-8"), base) + "</style>"

        html = re.sub(
            r"""<link\b[^>]*\bhref\s*=\s*(["'])([^"']+\.css(?:\?[^"']*)?)\1[^>]*>""",
            stylesheet,
            html,
            flags=re.I,
        )

        def asset(url):
            if url.startswith(("data:", "blob:", "#")):
                return url
            if TEMPLATE.search(url):
                # Snapshot macros are expanded by the reader; its parent prepares
                # the resulting concrete image before mounting even script-free frames.
                return url
            if (
                url.startswith("https://")
                and url not in manifest["resources"]
                and declared_image(url, declarations)
            ):
                # Presentation-only images are resolved by the reader, after macros.
                # Their availability must not gate story generation/atomic commits.
                return url
            raw, mime = content(url)
            return f"data:{mime.split(';')[0]};base64,{base64.b64encode(raw).decode()}"

        return rewrite_markup(html, asset)
