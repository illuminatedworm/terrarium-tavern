"""Real UI acceptance for source-only switches and the compact trigger controls."""
import asyncio
import json
import socket
import tempfile
from pathlib import Path
from threading import Thread

import uvicorn
from playwright.async_api import async_playwright, expect

from tavern.application.api import create_app
from tests.backend.test_chatgpt_integration import connected_store, story


async def main():
    output = Path(".agents/.local/context-strategy-validation")
    output.mkdir(parents=True, exist_ok=True)
    checks, errors = [], []
    with tempfile.TemporaryDirectory(dir=output, prefix="isolated-") as directory:
        fixture, _, _ = connected_store(Path(directory))
        card, _ = story(fixture, "full_agent")
        app = create_app(Path(directory))
        app.state.store._chatgpt_accounts = fixture.chatgpt

        def unexpected_provider(_):
            raise AssertionError("Short-history switches must not initialize a model provider")

        app.state.service.context_operations.provider_factory = unexpected_provider
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        address = f"http://127.0.0.1:{listener.getsockname()[1]}"
        server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
        thread = Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
        thread.start()
        try:
            for _ in range(100):
                if server.started:
                    break
                await asyncio.sleep(.05)
            assert server.started
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch()
                for mode, title, local_name in (
                    ("full_agent", "Full Agent", "本地换窗"), ("half_agent", "Half Agent", "本地滚动窗口"),
                ):
                    page = await browser.new_page(viewport={"width": 1920, "height": 1080})
                    await page.goto(address)
                    await page.get_by_role("tab", name="会话", exact=True).click()
                    await page.get_by_role("button", name="开始新故事", exact=True).click()
                    await page.get_by_role("button", name=f"选择角色卡 {card['name']}").click()
                    await page.get_by_label("运行模式", exact=True).select_option(mode)
                    await page.get_by_role("button", name="进入故事", exact=True).click()
                    await page.locator(".story").wait_for()
                    saved = await page.context.storage_state()
                    await page.close()
                    for width, height, scale, label in [
                        (2560, 1440, 1, "desktop-2560"), (1920, 1080, 1, "desktop-1920"),
                        (360, 792, 3, "mobile-1080x2376"), (960, 540, 2, "desktop-200pct"),
                    ]:
                        context = await browser.new_context(viewport={"width": width, "height": height},
                                                            device_scale_factor=scale, storage_state=saved)
                        page = await context.new_page()
                        page.on("pageerror", lambda error: errors.append(str(error)))
                        await page.goto(address)
                        await expect(page.locator(".story-heading")).to_contain_text(title)
                        if not await page.get_by_role("button", name="场景", exact=True).is_visible():
                            await page.get_by_role("button", name="展开右侧栏", exact=True).click()
                        await page.get_by_role("button", name="场景", exact=True).click()
                        controls = page.get_by_role("region", name="上下文方式", exact=True)
                        compact = controls.get_by_role("radio", name="云端 compact", exact=True)
                        local = controls.get_by_role("radio", name=local_name, exact=True)
                        await local.check()
                        await expect(local).to_be_enabled()
                        await compact.check()
                        await expect(compact).to_be_checked()
                        await expect(compact).to_be_enabled()
                        await expect(controls.get_by_role("status")).to_contain_text("完整历史无需转换")
                        await expect(controls.get_by_role("button", name="切回本地换窗", exact=True)).to_have_count(0)
                        await expect(controls.get_by_role("button", name="立即压缩", exact=True)).to_be_visible()
                        row = controls.locator(".compact-auto-row")
                        await expect(row).to_contain_text("发送时超出窗口则先压缩")
                        bounds = await row.evaluate("""row => {
                            const a = row.querySelector('label span').getBoundingClientRect();
                            const b = row.querySelector('.inspector-note').getBoundingClientRect();
                            return {delta: Math.abs(a.top + a.height / 2 - b.top - b.height / 2),
                                sameLine: b.left >= a.right, scroll: document.documentElement.scrollWidth,
                                viewport: innerWidth, scrollHeight: document.documentElement.scrollHeight,
                                viewportHeight: innerHeight};
                        }""")
                        assert bounds["delta"] < 1 and bounds["sameLine"], (label, bounds)
                        assert bounds["scroll"] <= bounds["viewport"], (label, bounds)
                        assert bounds["scrollHeight"] <= bounds["viewportHeight"], (label, bounds)
                        await row.scroll_into_view_if_needed()
                        await controls.screenshot(path=str(output / f"{mode}-{label}.png"))
                        checks.append({"mode": mode, "label": label, **bounds})
                        automatic = controls.get_by_role("checkbox", name="自动压缩", exact=True)
                        await automatic.uncheck()
                        await expect(automatic).not_to_be_checked()
                        await automatic.check()
                        await expect(automatic).to_be_checked()
                        if mode == "half_agent":
                            await expect(controls).to_contain_text("Tracker 继续整理记忆")
                            await expect(controls).not_to_contain_text("交接笔记")
                        await context.close()
                await browser.close()
            assert not errors, errors
            (output / "report.json").write_text(json.dumps({"checks": checks, "page_errors": errors},
                                                          ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps({"passed": len(checks), "output": str(output)}, ensure_ascii=False))
        finally:
            server.should_exit = True
            thread.join(timeout=10)
            listener.close()
            assert not thread.is_alive()


if __name__ == "__main__":
    asyncio.run(main())
