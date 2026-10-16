"""Hit-test the sidebar toggle where it overlaps the story header."""
import asyncio
import socket
import tempfile
from pathlib import Path
from threading import Thread

import uvicorn
from playwright.async_api import async_playwright, expect

from tavern.application.api import create_app


async def main():
    with tempfile.TemporaryDirectory() as data:
        app = create_app(data)
        card = app.state.store.import_card(Path("samples/雾港来信.json").read_bytes(), "card.json")
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
            assert server.started
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch()
                for mode in ("half_agent", "full_agent"):
                    page = await browser.new_page(viewport={"width": 1440, "height": 900})
                    await page.goto(address)
                    await page.get_by_role("tab", name="会话", exact=True).click()
                    await page.get_by_role("button", name="开始新故事", exact=True).click()
                    await page.get_by_role("button", name=f"选择角色卡 {card['name']}").click()
                    await page.get_by_label("运行模式", exact=True).select_option(mode)
                    await page.get_by_role("button", name="进入故事 →", exact=True).click()
                    await page.locator(".story").wait_for()
                    await page.get_by_role("button", name="折叠左侧栏", exact=True).click()
                    await page.get_by_role("button", name="场景", exact=True).click()
                    toggle = page.locator(".right-sidebar > .sidebar-collapse")
                    for width in (1440, 1000, 720, 390):
                        await page.set_viewport_size({"width": width, "height": 900})
                        for _ in range(2):
                            await expect(toggle).to_be_visible()
                            assert await toggle.evaluate("""button => {
                                const r = button.getBoundingClientRect();
                                return [r.left + 2, r.left + r.width / 2, r.right - 2].every(x =>
                                    button.contains(document.elementFromPoint(x, r.top + r.height / 2)));
                            }"""), f"{mode} scene toggle is covered at viewport width {width}"
                            # Click the part extending over the center pane, not just its center.
                            await toggle.click(position={"x": 3, "y": 16})
                    await page.set_viewport_size({"width": 1440, "height": 900})
                    tabs = ["MVU 变量", "会话设置"]
                    if mode == "full_agent":
                        tabs.append("工具")
                    for tab in tabs:
                        await page.get_by_role("button", name=tab, exact=True).click()
                        for _ in range(2):
                            assert await toggle.evaluate("""button => {
                                const r = button.getBoundingClientRect();
                                return button.contains(document.elementFromPoint(r.left + 2, r.top + r.height / 2));
                            }"""), f"{mode} {tab} toggle is covered"
                            await toggle.click(position={"x": 3, "y": 16})
                    if await page.locator(".right-sidebar:not(.collapsed)").count():
                        await toggle.click()
                    await page.get_by_role("button", name="变量与记忆", exact=True).click()
                    await expect(page.get_by_role("dialog")).to_be_visible()
                    assert not await toggle.evaluate("""button => {
                        const r = button.getBoundingClientRect();
                        return button.contains(document.elementFromPoint(r.left + 3, r.top + 16));
                    }"""), "Modal overlay must remain above the toggle"
                    await page.close()
                await browser.close()
        finally:
            server.should_exit = True
            thread.join(timeout=15)


if __name__ == "__main__":
    asyncio.run(main())
