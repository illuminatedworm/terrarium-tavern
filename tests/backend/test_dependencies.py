import json

import httpx

from tavern.scripting.dependencies import Dependencies
from tavern.storage.store import Store


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
        "tavern.scripting.dependencies.httpx.AsyncClient", lambda **kwargs: real(transport=httpx.MockTransport(route))
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


async def test_css_fonts_and_imports_are_cached_and_inlined(tmp_path, monkeypatch):
    store = Store(tmp_path)
    url = "https://cards.example/css/icons.css"
    card = store.import_card(json.dumps({"name": "icons", "first_mes": f'<link href="{url}" rel="stylesheet">'}).encode(), "card.json")
    card["permission"] = 1
    resolver = Dependencies(store)
    css = '@import "./theme.css"; @font-face{font-family:icons;src:url("../fonts/icons.woff2")} .icon{background:url(data:image/png;base64,eA==)}'
    # Simulate a stylesheet cached by the older resolver, without its font dependencies.
    resolver.manifest_path(card).write_text(json.dumps({"scripts": {}, "resources": {
        url: {"hash": store.blob(css.encode()), "url": url, "content_type": "text/css"}
    }}))
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
    monkeypatch.setattr("tavern.scripting.dependencies.httpx.AsyncClient", lambda **kwargs: real(transport=httpx.MockTransport(route)))
    await resolver.resolve(card)
    assert set(requested) == {"https://cards.example/css/theme.css", "https://cards.example/fonts/icons.woff2"}
    html = resolver.markup(card, f'<link rel="stylesheet" href="{url}"><i class="icon"></i>')
    assert 'url("data:font/woff2;base64,dGVzdC1mb250")' in html
    assert '@import "data:text/css;base64,' in html
    assert "../fonts" not in html
    assert "url(data:image/png;base64,eA==)" in html
    assert resolver.markup(card, f"<style>{css}</style>") == html.split("<i")[0]
    assert resolver.markup(card, html) == html
    await resolver.resolve(card)
    assert len(requested) == 2
