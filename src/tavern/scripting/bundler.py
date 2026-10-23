"""Bundle pinned card modules inside an offline realm, on desktop and Android."""

import asyncio
import secrets
from pathlib import Path

from tavern.scripting.realm import default_backend

STATIC = Path(__file__).parents[1] / "static" / "esbuild"


class DependencyBundler:
    def __init__(self):
        self.backend = None
        self.page = None
        self.modules = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        if self.backend:
            await self.backend.close()

    async def bundle(self, source, modules):
        # The former Node build had the same bound. Dependencies holds its lock meanwhile,
        # so a runaway build must not block every card's dependency resolution.
        try:
            async with asyncio.timeout(45):
                return await self._bundle(source, modules)
        except TimeoutError:
            raise ValueError("卡片模块打包超过 45 秒，已停止；请检查模块体积或依赖结构。") from None

    async def _bundle(self, source, modules):
        self.modules = modules
        if self.page is None:
            if not (STATIC / "esbuild.wasm").is_file():
                raise ValueError("模块打包器尚未构建，请先运行 npm run build:sdk")
            prefix = f"https://tavern.invalid/{secrets.token_urlsafe(24)}/"

            def resource(url, method):
                if method == "GET" and url == prefix + "esbuild.wasm":
                    return (STATIC / "esbuild.wasm").read_bytes(), "application/wasm"
                return None

            async def host(method, url):
                if method != "module" or url not in self.modules:
                    raise ValueError(f"Unresolved dependency: {url}")
                return self.modules[url].read_text(encoding="utf-8")

            self.backend = default_backend(tools=True)
            context = await self.backend.new_context(resource)
            self.page = await context.open_page(
                '<html><head><meta http-equiv="Content-Security-Policy" '
                "content=\"default-src 'none'; script-src 'unsafe-inline' 'unsafe-eval'; "
                f'connect-src {prefix}"></head><body></body></html>',
                host,
            )
            await self.page.add_script((STATIC / "browser.min.js").read_text(encoding="utf-8"))
            await self.page.add_script((STATIC / "bundle.js").read_text(encoding="utf-8"))
            await self.page.evaluate(
                "url => esbuild.initialize({wasmURL: url, worker: false})", prefix + "esbuild.wasm"
            )
        try:
            return await self.page.evaluate(
                "input => __tavernBundle(input)", {"source": source, "modules": list(modules)}
            )
        except Exception as error:
            raise ValueError(f"卡片模块打包失败：{error}") from error
