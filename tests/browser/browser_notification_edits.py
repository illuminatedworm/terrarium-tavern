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
from tavern.core.models import SessionCreate, TurnRequest


async def main():
    with tempfile.TemporaryDirectory() as root:
        app = create_app(root)
        store = app.state.store
        settings = store.settings()
        settings.memory_interval_turns = 1
        store.save_settings(settings)
        card = store.import_card(json.dumps({"name": "记忆提醒测试", "first_mes": "真实原文"}).encode(), "card.json")
        session = store.new_session(SessionCreate(card_id=card["id"]))
        tid, _ = store.create_turn(session["id"], TurnRequest(text="旧发言", operation_id="failed", expected_revision=session["revision"]))
        store.set_turn(tid, status="error", error="测试错误", draft="旧草稿")
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
                    alert = page.get_by_role("alert")
                    await expect(alert).to_have_count(1)
                    assert await alert.evaluate("e=>getComputedStyle(e).position") == "fixed"
                    await expect(alert.locator("button")).to_have_count(0)
                    await page.locator(".message.user").get_by_role("button", name="编辑", exact=True).click()
                    await page.get_by_role("textbox", name="编辑消息原文", exact=True).fill("修改后的发言“正反引号”")
                    await page.locator(".inline-message-editor").get_by_role("button", name="保存", exact=True).click()
                    await expect(page.locator(".message.user")).to_contain_text("修改后的发言")
                    await page.reload()
                    await expect(page.locator(".message.user")).to_contain_text("修改后的发言")
                    await expect(page.get_by_role("alert")).to_have_count(0, timeout=9500)
                    assert store.turn(tid)["request"]["text"] == "修改后的发言“正反引号”"
                    regenerate = page.locator(".story").get_by_role("button", name="↻ 重新生成", exact=True)
                    await expect(regenerate).to_have_count(1)
                    collapsed_width = (await regenerate.bounding_box())["width"]
                    await regenerate.click()
                    await expect(regenerate).to_have_count(1)
                    collapse = page.get_by_role("button", name="收起重新生成选项", exact=True)
                    await expect(collapse).to_be_visible()
                    assert abs((await collapse.bounding_box())["width"] - collapsed_width) < 1
                    options = page.locator(".turn-options label")
                    first, second = await options.nth(0).bounding_box(), await options.nth(1).bounding_box()
                    assert second["x"] - (first["x"] + first["width"]) <= 11
                    assert first["x"] > (await page.locator(".compose-bottom").bounding_box())["width"] / 2
                    assert ">" not in await page.locator(".compose-bottom").evaluate(
                        "e => [...e.childNodes].filter(n => n.nodeType === Node.TEXT_NODE).map(n => n.textContent).join('')"
                    )
                    await collapse.click()
                    await expect(page.locator(".regenerate-bar")).to_be_hidden()
                    await expect(regenerate).to_be_visible()
                    await regenerate.click()
                    await expect(collapse).to_be_visible()
                    await regenerate.click()
                    await expect(page.locator(".message.assistant").last).to_contain_text("剧情继续", timeout=15000)
                    await expect(page.get_by_role("button", name="发送", exact=True)).to_be_visible(timeout=15000)
                    assert store.session(session["id"])["messages"][-2]["raw"] == "修改后的发言“正反引号”"
                    await expect(regenerate).to_have_count(1)
                    print("Floating notification, automatic dismissal, persisted pending edit and retry passed.")
                finally:
                    await browser.close()
        finally:
            server.should_exit = True
            thread.join(timeout=10)


if __name__ == "__main__":
    asyncio.run(main())
