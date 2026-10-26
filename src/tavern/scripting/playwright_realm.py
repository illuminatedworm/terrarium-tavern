"""Desktop realm backend on Playwright's headless Chromium."""

import asyncio

from tavern.scripting.network_guard import NETWORK_GUARD


class PlaywrightPage:
    def __init__(self, page):
        self.page = page

    async def evaluate(self, script, arg=None):
        return await self.page.evaluate(script, arg)

    async def add_script(self, source):
        await self.page.add_script_tag(content=source)

    async def texts(self, selector):
        return await self.page.locator(selector).all_text_contents()

    async def close(self):
        await self.page.close()


class PlaywrightContext:
    def __init__(self, context):
        self.context = context

    async def open_page(self, html, host, on_error=None):
        page = await self.context.new_page()
        if on_error:
            page.on("pageerror", lambda error: on_error(str(error)))
        await page.expose_function("__tavernHost", host)
        await page.set_content(html)
        return PlaywrightPage(page)

    async def close(self):
        await self.context.close()


class PlaywrightBackend:
    def __init__(self):
        self.playwright = None
        self.browser = None
        self.lock = asyncio.Lock()

    async def new_context(self, resource):
        async with self.lock:
            if self.browser is None:
                # Imported lazily so platforms without Playwright can load other backends.
                from playwright.async_api import async_playwright

                self.playwright = await async_playwright().start()
                # Share the full Chromium with DesktopWindow; portable bundles omit headless shell.
                self.browser = await self.playwright.chromium.launch(channel="chromium", headless=True)

        async def route(route):
            result = resource(route.request.url, route.request.method)
            if result is None:
                return await route.abort()
            body, mime = result
            # set_content pages are about:blank, so realm resources are cross-origin reads.
            await route.fulfill(
                body=body,
                content_type=mime,
                headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "no-store"},
            )

        context = await self.browser.new_context(service_workers="block")
        try:
            # Run before scripts in every document, including newly attached about:blank
            # frames; otherwise a card can recover the native constructor from a child.
            await context.add_init_script(NETWORK_GUARD)
            await context.route("**/*", route)
        except BaseException:
            await context.close()
            raise
        return PlaywrightContext(context)

    async def close(self):
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()
        self.browser = self.playwright = None
