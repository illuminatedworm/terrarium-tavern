"""Verify discarded memory alerts in an isolated app with a local model."""

import asyncio
import json
import socket
import tempfile
from threading import Thread
from types import SimpleNamespace

import uvicorn
from kohakuterrarium.testing.llm import ScriptedLLM
from playwright.async_api import async_playwright, expect

from tavern.application.api import create_app
from tavern.core.models import SessionCreate


async def main():
    with tempfile.TemporaryDirectory() as root:
        app = create_app(root)
        store = app.state.store
        settings = store.settings()
        settings.memory_interval_turns = 1
        store.save_settings(settings)
        card = store.import_card(json.dumps({"name": "记忆提醒测试", "first_mes": "真实原文"}).encode(), "card.json")
        session = store.new_session(SessionCreate(card_id=card["id"]))
        app.state.service.provider_factory = lambda: ScriptedLLM(["剧情继续。"])

        class Memory:
            async def chat_complete(self, messages, **kwargs):
                source = json.loads(messages[-1]["content"])["messages"][0]["id"]
                return SimpleNamespace(content=json.dumps({"text": "有效整理", "memories": [
                    {"content": "错误引用", "source": source, "kind": "fact", "excerpt": "不存在的引用"}
                ]}), usage={})

            async def close(self):
                pass

        app.state.service.memory_provider_factory = Memory
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        address = f"http://127.0.0.1:{sock.getsockname()[1]}"
        server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
        thread = Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
        thread.start()
        try:
            while not server.started:
                await asyncio.sleep(.05)
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch()
                try:
                    page = await browser.new_page()
                    await page.goto(address)
                    await page.evaluate("id=>localStorage.setItem('tavern-session',id)", session["id"])
                    await page.reload()
                    send = page.get_by_role("button", name="发送", exact=True)
                    await page.get_by_role("button", name="折叠左侧栏", exact=True).click()
                    await page.get_by_role("button", name="折叠右侧栏", exact=True).click()
                    for width, height in [(1440, 900), (1280, 720), (390, 844), (720, 450)]:
                        await page.set_viewport_size({"width": width, "height": height})
                        await page.locator(".compose-box > textarea").fill("继续")
                        await send.click()
                        await page.locator(".compose-box > textarea").fill("继续整理")
                        await expect(send).to_be_enabled(timeout=15000)
                        await send.click()
                        alert = page.get_by_role("alert").filter(has_text="已丢弃")
                        await expect(alert).to_contain_text("可以继续游玩", timeout=15000)
                        assert await alert.evaluate("e=>e.scrollWidth<=e.clientWidth+1")
                        await expect(alert).to_have_count(0, timeout=9500)
                        await page.locator(".compose-box > textarea").fill("下一轮")
                        await expect(send).to_be_enabled(timeout=15000)
                        assert store.session(session["id"])["snapshot"]["memory"] == []
                    print("Memory discard alerts and continued play passed at four viewport sizes.")
                finally:
                    await browser.close()
        finally:
            server.should_exit = True
            thread.join(timeout=10)


if __name__ == "__main__":
    asyncio.run(main())
