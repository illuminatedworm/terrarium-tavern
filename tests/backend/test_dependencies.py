import asyncio
import json
from copy import deepcopy
from io import BytesIO

import httpcore
import httpx
import pytest
from PIL import Image

from tavern.core.models import SessionCreate
from tavern.scripting.dependencies import PRIVATE_NETWORK, Dependencies
from tavern.scripting.image_resources import declared_image
from tavern.storage.store import Store
from tavern.web.transport import PublicNetworkBackend


async def test_dynamic_image_templates_resolve_after_expansion_and_stay_image_only(tmp_path, monkeypatch):
    store = Store(tmp_path)
    root = "https://cards.example/gallery"
    document = {
        "name": "templates",
        "first_mes": f'<img src="{root}/static.png">',
        "extensions": {
            "regex_scripts": [
                {"findRegex": "(.*)", "replaceString": f'<img src="{root}/$1/$2.png">'},
                {"findRegex": "(.*)", "replaceString": f'<img src="{root}/$<category>/$<name>.png">'},
                {"findRegex": "(.*)", "replaceString": f'<img src="{root}/{{{{getvar::art}}}}.png">'},
                {
                    "findRegex": "(.*)",
                    "replaceString": f'<script>const base="{root}";const src=`{root}/${{name}}.png`;</script>',
                },
            ]
        },
    }
    card = store.import_card(json.dumps(document).encode(), "card.json")
    card["permission"] = 1
    before = deepcopy(card)
    buffer = BytesIO()
    Image.new("RGB", (2, 3)).save(buffer, "PNG")
    requested, validated = [], []
    started, release, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def validate(url):
        validated.append(url)
        if not url.startswith(root + "/"):
            raise ValueError("卡片不能访问本地或私有网络资源")

    async def route(request):
        requested.append(str(request.url))
        assert "$" not in str(request.url) and "%24" not in str(request.url)
        if request.url.path.endswith(("waiting.png", "cancelled.png")):
            started.set()
            try:
                await release.wait()
            finally:
                closed.set()
        if request.url.path.endswith("missing.png"):
            return httpx.Response(404)
        if request.url.path.endswith("redirect.png"):
            return httpx.Response(302, headers={"location": "https://127.0.0.1/private.png"})
        if request.url.path.endswith("invalid.png"):
            return httpx.Response(200, text="<html>not an image</html>")
        return httpx.Response(
            200, headers={"content-type": "application/octet-stream"}, content=buffer.getvalue()
        )

    real = httpx.AsyncClient
    monkeypatch.setattr("tavern.scripting.dependencies.validate_remote", validate)
    monkeypatch.setattr(
        "tavern.scripting.dependencies.httpx.AsyncClient",
        lambda **kwargs: real(transport=httpx.MockTransport(route)),
    )
    resolver = Dependencies(store)
    try:
        assert declared_image(root + "/nested/scene.png", [root + "/$1.png"])
        assert declared_image(root + "/nested/scene.png", [root + "/$<path>.png"])
        await resolver.resolve(card)
        assert requested == [root + "/static.png"]
        # Positional/named captures, snapshot macros and JS templates are declarations,
        # never literal downloads. Source code and its HTML samples stay untouched.
        sample = f'<script>const sample=\'<img src="{root}/$1/$2.png">\';const img=new Image();img.src="{root}/late.png";</script>'
        handler = f"<button onclick=\"image.src='{root}/click.png'\">show</button>"
        html = (
            f'<img src="{root}/scene/1.png"><picture><source srcset="{root}/small.png 1x, {root}/large.png 2x"></picture>'
            f'<div style="background:url({root}/background.png)"></div>'
            f"<style>.art{{background:url({root}/style.png)}}</style>" + sample + handler
        )
        assert resolver.markup(card, html) == html
        rendered = await resolver.prepare_markup(card, html)
        assert rendered.count("data:image/png;base64,") == 5
        assert sample in rendered
        assert handler in rendered
        assert not any("late.png" in url for url in requested)
        assert await resolver.prepare_markup(card, rendered) == rendered
        count = len(requested)
        await resolver.image(card, root + "/scene/1.png")
        assert len(requested) == count
        # Streaming fails per image: a missing one never hides the others.
        streamed = [
            item
            async for item in resolver.stream_images(card, [root + "/missing.png", root + "/scene/1.png"])
        ]
        assert [url for url, data, _ in streamed if data and data.startswith("data:image/png")] == [
            root + "/scene/1.png"
        ]
        assert isinstance(
            next(error for url, _, error in streamed if url.endswith("missing.png")), httpx.HTTPStatusError
        )
        assert "{{getvar::art}}" in await resolver.prepare_markup(
            card, f'<img src="{root}/{{{{getvar::art}}}}.png">'
        )
        for url in ["https://other.example/art.png", root + "-other/art.png", root + "/%2e%2e/private.png"]:
            with pytest.raises(ValueError, match="不属于"):
                await resolver.image(card, url)
        with pytest.raises(ValueError, match="启用"):
            await resolver.image({**card, "permission": 0}, root + "/scene/1.png")
        with pytest.raises(httpx.HTTPStatusError):
            await resolver.image(card, root + "/missing.png")
        with pytest.raises(ValueError, match="私有"):
            await resolver.image(card, root + "/redirect.png")
        assert validated[-1] == "https://127.0.0.1/private.png"
        with pytest.raises(ValueError, match="图片内容无效"):
            await resolver.image(card, root + "/invalid.png")
        first = asyncio.create_task(resolver.image(card, root + "/waiting.png"))
        second = asyncio.create_task(resolver.image(card, root + "/waiting.png"))
        await started.wait()
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert not second.done()
        release.set()
        assert (await second)[1] == "image/png"
        assert requested.count(root + "/waiting.png") == 1
        started.clear()
        release.clear()
        closed.clear()
        cancelled = asyncio.create_task(resolver.image(card, root + "/cancelled.png"))
        await started.wait()
        cancelled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled
        assert closed.is_set() and not resolver.image_tasks
        # Cache eviction does not write derived image entries into persistent manifests.
        for index in range(129):
            await resolver.image(card, root + f"/evict-{index}.png")
        assert len(resolver.images) == 128
        assert resolver.image_bytes <= 32 * 1024 * 1024
        assert card == before
        assert list(resolver.read(card)["resources"]) == [root + "/static.png"]
    finally:
        await resolver.close()
    assert not resolver.images and not resolver.image_tasks


async def test_public_network_connects_only_to_the_checked_address(monkeypatch):
    # One lookup decides the connection: a second resolver answer cannot rebind it.
    answers = {
        "cdn.example": [["93.184.216.34", "10.0.0.1"], ["93.184.216.34"]],
        "rebind.example": [["93.184.216.34"], ["127.0.0.1"]],
    }
    monkeypatch.setattr(
        "tavern.web.transport.socket.getaddrinfo",
        lambda host, port, **_: [(None, None, None, "", (a, port)) for a in answers[host].pop()],
    )
    connected = []

    async def connect(self, host, port, *args):
        connected.append(host)
        return host

    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", connect)
    network = PublicNetworkBackend(PRIVATE_NETWORK)
    assert await network.connect_tcp("cdn.example", 443) == "93.184.216.34"
    with pytest.raises(ValueError, match="私有"):
        await network.connect_tcp("rebind.example", 443)
    with pytest.raises(ValueError, match="私有"):
        await network.connect_tcp("cdn.example", 443)  # Any private answer rejects the whole name.
    assert connected == ["93.184.216.34"]


async def test_offline_dependency_graph_is_pinned(tmp_path, monkeypatch):
    store = Store(tmp_path)
    source = (
        "import {answer} from 'https://cards.example/entry.js'; if(answer!==42)throw Error('bad module');"
    )
    card = store.import_card(
        json.dumps(
            {
                "name": "modules",
                "extensions": {"tavern_helper": {"scripts": [{"id": "module", "content": source}]}},
            }
        ).encode(),
        "card.json",
    )
    card["permission"] = 1
    requested = []

    async def validate(url):
        assert url.startswith("https://cards.example/")

    monkeypatch.setattr("tavern.scripting.dependencies.validate_remote", validate)

    def route(request):
        requested.append(str(request.url))
        return httpx.Response(
            200,
            headers={"content-type": "application/javascript"},
            text="import {value} from './value.js'; export const answer=value;"
            if request.url.path == "/entry.js"
            else "export const value=42;",
        )

    real = httpx.AsyncClient
    monkeypatch.setattr(
        "tavern.scripting.dependencies.httpx.AsyncClient",
        lambda **kwargs: real(transport=httpx.MockTransport(route)),
    )
    resolver = Dependencies(store)
    manifest = await resolver.resolve(card)
    assert len(requested) == 2
    assert len(manifest["resources"]) == 2
    assert len(manifest["scripts"]) == 1
    assert resolver.scripts(card)[0]["bundled"]
    assert "https://cards.example/entry.js" in json.dumps(manifest)
    await resolver.resolve(card)
    assert len(requested) == 2  # Cached resources never re-fetch mutable remote content.


async def test_new_conversation_reuses_existing_session_dependencies(tmp_path, monkeypatch):
    store = Store(tmp_path)
    source = (
        "import {answer} from 'https://cards.example/entry.js'; if(answer!==42)throw Error('bad module');"
    )
    card = store.import_card(
        json.dumps(
            {
                "name": "modules",
                "extensions": {"tavern_helper": {"scripts": [{"id": "module", "content": source}]}},
            }
        ).encode(),
        "card.json",
    )
    with store.connection() as db:
        db.execute("UPDATE cards SET permission=1 WHERE id=?", (card["id"],))
    requested = []

    async def validate(url):
        assert url.startswith("https://cards.example/")

    def route(request):
        requested.append(str(request.url))
        return httpx.Response(
            200,
            headers={"content-type": "application/javascript"},
            text="import {value} from './value.js'; export const answer=value;"
            if request.url.path == "/entry.js"
            else "export const value=42;",
        )

    monkeypatch.setattr("tavern.scripting.dependencies.validate_remote", validate)
    real = httpx.AsyncClient
    monkeypatch.setattr(
        "tavern.scripting.dependencies.httpx.AsyncClient",
        lambda **kwargs: real(transport=httpx.MockTransport(route)),
    )
    resolver = Dependencies(store)
    first = store.new_session(SessionCreate(card_id=card["id"]))
    await resolver.resolve(store.session_card(first["id"]))
    assert len(requested) == 2
    assert len(store.card_manifest(card)["resources"]) == 2
    # Older installations only have the session manifest, with no card-level cache.
    resolver.manifest_path(card).unlink()
    second = store.new_session(SessionCreate(card_id=card["id"]))
    assert len(store.session_card(second["id"])["dependencies"]["resources"]) == 2
    await resolver.resolve(store.session_card(second["id"]))
    assert len(requested) == 2
    assert len(store.card_manifest(card)["scripts"]) == 1
    original = store.card_manifest(card)["resources"]["https://cards.example/entry.js"]["hash"]
    store.cache_card_dependencies(
        card["id"],
        {
            "resources": {
                "https://cards.example/entry.js": {
                    "hash": store.blob(b"changed"),
                    "url": "https://cards.example/entry.js",
                    "content_type": "application/javascript",
                }
            },
            "scripts": {},
        },
    )
    assert store.card_manifest(card)["resources"]["https://cards.example/entry.js"]["hash"] == original


async def test_css_fonts_and_imports_are_cached_and_inlined(tmp_path, monkeypatch):
    store = Store(tmp_path)
    url = "https://cards.example/css/icons.css"
    card = store.import_card(
        json.dumps({"name": "icons", "first_mes": f'<link href="{url}" rel="stylesheet">'}).encode(),
        "card.json",
    )
    card["permission"] = 1
    resolver = Dependencies(store)
    css = '@import "./theme.css"; @font-face{font-family:icons;src:url("../fonts/icons.woff2")} .icon{background:url(data:image/png;base64,eA==)}'
    # Simulate a stylesheet cached by the older resolver, without its font dependencies.
    resolver.manifest_path(card).write_text(
        json.dumps(
            {
                "scripts": {},
                "resources": {
                    url: {"hash": store.blob(css.encode()), "url": url, "content_type": "text/css"}
                },
            }
        )
    )
    requested = []

    async def validate(url):
        assert url.startswith("https://cards.example/")

    def route(request):
        requested.append(str(request.url))
        if request.url.path.endswith("theme.css"):
            return httpx.Response(200, headers={"content-type": "text/css"}, text=".icon{color:red}")
        return httpx.Response(200, headers={"content-type": "font/woff2"}, content=b"test-font")

    monkeypatch.setattr("tavern.scripting.dependencies.validate_remote", validate)
    real = httpx.AsyncClient
    monkeypatch.setattr(
        "tavern.scripting.dependencies.httpx.AsyncClient",
        lambda **kwargs: real(transport=httpx.MockTransport(route)),
    )
    await resolver.resolve(card)
    assert set(requested) == {
        "https://cards.example/css/theme.css",
        "https://cards.example/fonts/icons.woff2",
    }
    html = resolver.markup(card, f'<link rel="stylesheet" href="{url}"><i class="icon"></i>')
    assert 'url("data:font/woff2;base64,dGVzdC1mb250")' in html
    assert '@import "data:text/css;base64,' in html
    assert "../fonts" not in html
    assert "url(data:image/png;base64,eA==)" in html
    assert resolver.markup(card, f"<style>{css}</style>") == html.split("<i")[0]
    assert resolver.markup(card, html) == html
    await resolver.resolve(card)
    assert len(requested) == 2
