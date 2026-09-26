"""Full Agent mode selection, real card IO, notes, rollover, and responsive UI."""
import asyncio
import socket
import tempfile
from pathlib import Path
from threading import Thread

import uvicorn
from playwright.async_api import async_playwright, expect

from tavern.application.api import create_app
from tests.backend.test_full_agent import Model


async def main():
    output = Path(".cache/full-agent-ui")
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as data:
        app = create_app(data)
        store = app.state.store
        card = store.import_card(Path("samples/雾港来信.json").read_bytes(), "card.json")
        settings = store.settings()
        settings.full_agent_config.auto_retrieval = False
        store.save_settings(settings)
        app.state.service.full.provider_factory = lambda: Model(
            ("read_history", {"ref": "H2"}),
            ("write_note", {"key": "约定/钟楼", "content": "玩家答应明天见面", "kind": "fact", "sources": ["H2"]}),
            ("new_context", {"content": "约定明天钟楼见面，继续当前对话。", "keep_recent": 0}),
            "她点头应下，钟声从远处传来。",
        )
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
                page = await browser.new_page(viewport={"width": 1440, "height": 900})
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                await page.goto(address)
                await page.get_by_role("tab", name="会话", exact=True).click()
                await page.get_by_role("button", name="＋ 开始新故事", exact=True).click()
                await page.get_by_role("button", name=f"选择角色卡 {card['name']}").click()
                await page.get_by_label("运行模式", exact=True).select_option("full_agent")
                await page.get_by_role("button", name="进入故事 →", exact=True).click()
                await page.locator(".story").wait_for()
                await expect(page.locator(".story-heading")).to_contain_text("Full Agent")
                await page.get_by_placeholder("说些什么，或描述你的行动…").fill("我答应明天在钟楼见面。")
                await page.get_by_role("button", name="发送", exact=True).click()
                await page.locator(".message.assistant").nth(1).wait_for(timeout=30000)
                await page.get_by_role("button", name="发送", exact=True).wait_for()
                await expect(page.locator(".message.assistant").nth(1)).to_contain_text("她点头应下")
                assert "工作过程" not in await page.locator(".messages").inner_text()
                if not await page.get_by_role("button", name="场景", exact=True).is_visible():
                    await page.get_by_role("button", name="展开右侧栏", exact=True).click()
                await page.get_by_role("button", name="场景", exact=True).click()
                notes = page.get_by_role("region", name="Full Agent 记忆与窗口")
                await expect(notes).to_contain_text("工作窗口 2")
                await expect(notes.get_by_text("约定/钟楼 · 有据事实", exact=True)).to_be_visible()
                await expect(page.get_by_role("button", name="重新生成记忆", exact=True)).to_have_count(0)
                await page.screenshot(path=str(output / "desktop.png"))
                await notes.get_by_label("交接笔记（checkpoint）", exact=True).fill("人工确认：明天去钟楼。")
                await notes.get_by_role("button", name="保存并换新窗口", exact=True).click()
                await expect(notes).to_contain_text("工作窗口 3")
                await page.get_by_role("button", name="状态与记忆", exact=True).click()
                modal = page.get_by_role("dialog")
                await expect(modal.get_by_label("交接笔记（checkpoint）", exact=True)).to_have_value("人工确认：明天去钟楼。")
                await page.set_viewport_size({"width": 390, "height": 844})
                await page.screenshot(path=str(output / "mobile.png"))
                assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                await modal.get_by_role("button", name="关闭", exact=True).click()
                await page.set_viewport_size({"width": 1440, "height": 900})
                await page.get_by_role("button", name="⚙ 设置", exact=True).click()
                modal = page.get_by_role("dialog", name="设置")
                await modal.get_by_role("button", name="Full Agent", exact=True).click()
                await expect(modal.get_by_label("每回合模型调用上限", exact=True)).to_have_value("24")
                await page.screenshot(path=str(output / "settings.png"))
                assert not errors, errors
                await browser.close()
        finally:
            server.should_exit = True
            thread.join(timeout=15)


if __name__ == "__main__":
    asyncio.run(main())
