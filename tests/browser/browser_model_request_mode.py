"""Request mode autosave, page undo and responsive settings visibility."""
import asyncio
import socket
import tempfile
from pathlib import Path
from threading import Thread

import uvicorn
from playwright.async_api import async_playwright, expect

from tavern.application.api import create_app


async def main():
    output = Path(".cache/model-request-mode")
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="tavern-request-mode-") as root:
        app = create_app(root)
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        address = f"http://127.0.0.1:{sock.getsockname()[1]}"
        server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
        thread = Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
        thread.start()
        try:
            for _ in range(100):
                if server.started:
                    break
                await asyncio.sleep(.05)
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch()
                try:
                    page = await browser.new_page(viewport={"width": 1920, "height": 1080})
                    errors = []
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    await page.goto(address)
                    await page.get_by_role("button", name="设置", exact=True).click()
                    await page.get_by_role("button", name="通用 网络与代理", exact=True).click()
                    field = page.get_by_role("combobox", name="模型请求方式", exact=True)
                    dialog = page.get_by_role("dialog", name="设置", exact=True)
                    await expect(field).to_have_value("auto")
                    height = await dialog.evaluate("e => e.getBoundingClientRect().height")
                    for mode in ("stream", "non_stream"):
                        async with page.expect_response(lambda response: response.request.method == "PATCH"
                                                        and response.url.endswith("/settings/shared")) as pending:
                            await field.select_option(mode)
                        response = await pending.value
                        assert response.ok
                        assert (await response.json())["shared"]["model_request_mode"] == mode
                        await expect(page.locator(".settings-save-status")).to_contain_text("已保存")
                        assert app.state.store.settings("full_agent").model_request_mode == mode
                        assert await dialog.evaluate("e => e.getBoundingClientRect().height") == height
                    await page.get_by_role("button", name="撤回本页修改", exact=True).click()
                    await expect(field).to_have_value("auto")
                    await expect(page.locator(".settings-save-status")).to_contain_text("已保存")
                    for width, height, name in [(2560, 1440, "large"), (1920, 1080, "desktop"),
                                                (360, 792, "mobile-dpr3"), (960, 540, "zoom-equivalent")]:
                        await page.set_viewport_size({"width": width, "height": height})
                        await field.scroll_into_view_if_needed()
                        await expect(field).to_be_visible()
                        assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth && "
                                                   "document.documentElement.scrollHeight <= innerHeight")
                        assert await dialog.evaluate("e => e.scrollWidth <= e.clientWidth + 1")
                        await page.screenshot(path=str(output / f"{name}.png"))
                    assert not errors, errors
                    print("Request modes, autosave, runtime projection, undo and four viewports passed.")
                finally:
                    await browser.close()
        finally:
            server.should_exit = True
            await asyncio.to_thread(thread.join, 10)


if __name__ == "__main__":
    asyncio.run(main())
