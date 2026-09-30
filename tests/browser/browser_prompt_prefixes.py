"""Verify mode-specific read-only prompt heads and the editable suffix on the real UI/wire."""
import asyncio
import json
import socket
import tempfile
from pathlib import Path
from threading import Thread

import uvicorn
from kohakuterrarium.testing.llm import ScriptedLLM
from playwright.async_api import async_playwright, expect

from tavern.application.api import create_app
from tavern.core.models import SessionCreate
from tests.backend.test_full_agent import Model


async def main():
    output = Path(".cache/prompt-prefix-ui")
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as root:
        app = create_app(root)
        store = app.state.store
        card = store.import_card(json.dumps({"name": "引导词检查", "first_mes": "旧港的灯亮了。"}).encode(), "card.json")
        settings = store.settings_document()
        settings.full_agent.tools.auto_retrieval = False
        settings.half_agent.retrieval.channels = ["keyword"]
        heads = {}
        sessions = {}
        for mode in ("full_agent", "half_agent"):
            agent = getattr(settings, mode)
            agent.preference_prompt = f"{mode} 用户偏好：\n<创作要求>"
            agent.regenerate_prompt = f"{mode} 用户本次要求："
            heads[mode] = (agent.preference_prompt, agent.regenerate_prompt)
            sessions[mode] = store.new_session(SessionCreate(card_id=card["id"], agent_mode=mode, persona="旅人"))
        store.save_settings(settings)
        providers = {mode: [] for mode in sessions}

        def full_provider():
            provider = Model("她点亮另一盏灯。")
            providers["full_agent"].append(provider)
            return provider

        def half_provider():
            provider = ScriptedLLM(["她点亮另一盏灯。"])
            providers["half_agent"].append(provider)
            return provider

        app.state.service.full.provider_factory = full_provider
        app.state.service.half.provider_factory = half_provider
        app.state.service.memory_provider_factory = lambda: ScriptedLLM(['{"operations":[],"questions":[]}'])
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
                try:
                    page = await browser.new_page(viewport={"width": 1440, "height": 1000})
                    errors = []
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    await page.goto(address)
                    for mode, session in sessions.items():
                        await page.evaluate("id=>localStorage.setItem('tavern-session',id)", session["id"])
                        await page.reload()
                        await expect(page.get_by_role("button", name="发送", exact=True)).to_be_visible()
                        expand = page.get_by_role("button", name="展开右侧栏", exact=True)
                        if await expand.is_visible():
                            await expand.click()
                        await page.locator(".inspector-tabs").get_by_role("button", name="会话设置", exact=True).click()
                        options = page.get_by_role("region", name="会话设置", exact=True)
                        prefix = options.get_by_role("region", name="本轮偏好的系统预制头", exact=True)
                        await expect(prefix.locator("pre")).to_have_text(heads[mode][0])
                        assert await prefix.locator("input, textarea, [contenteditable=true]").count() == 0
                        await options.get_by_role("button", name="放大编辑：本轮偏好", exact=True).click()
                        dialog = page.get_by_role("dialog", name="编辑：本轮偏好", exact=True)
                        await expect(dialog.get_by_role("region", name="本轮偏好的系统预制头").locator("pre")) \
                            .to_have_text(heads[mode][0])
                        await dialog.get_by_role("textbox", name="编辑内容：本轮偏好", exact=True).fill("放缓节奏。")
                        await dialog.get_by_role("button", name="应用修改", exact=True).click()
                        await options.get_by_role("button", name="保存", exact=True).click()
                        await expect(options.get_by_role("status")).to_contain_text("保存")
                        saved = store.session(session["id"])
                        assert saved["turn_preference"] == "放缓节奏。"
                        await page.screenshot(path=str(output / f"{mode}-preference.png"))
                        print(f"{mode}: suffix-only preference save passed", flush=True)
                        await page.get_by_role("textbox", name="用户输入", exact=True).fill("我走向灯光。")
                        await page.get_by_role("button", name="发送", exact=True).click()
                        await expect(page.locator(".message.assistant").nth(1)).to_contain_text("另一盏灯", timeout=30000)
                        await page.locator(".regenerate-toggle").click()
                        regeneration = page.get_by_role("region", name="重新生成补充要求的系统预制头", exact=True)
                        await expect(regeneration.locator("pre")).to_have_text(heads[mode][1])
                        assert await regeneration.locator("input, textarea, [contenteditable=true]").count() == 0
                        await page.get_by_role("textbox", name="重新生成补充要求", exact=True).fill("多用对白。")
                        await page.screenshot(path=str(output / f"{mode}-regenerate.png"))
                        await page.locator(".regenerate-bar").get_by_role("button", name="重新生成", exact=False).click()
                        await expect(page.locator(".regenerate-toggle")).to_be_enabled(timeout=30000)
                        assert len(providers[mode]) == 2
                        provider = providers[mode][-1]
                        messages = provider.requests[0]["messages"] if mode == "full_agent" else provider.call_log[0]
                        text = "\n".join(m.get("content", "") for m in messages)
                        assert heads[mode][0] + "\n放缓节奏。" in text
                        assert heads[mode][1] + "\n多用对白。" in text
                        assert "用户名字：旅人" in text if mode == "half_agent" else "用户：旅人" in text
                        assert "玩家" not in text
                        first_user = next(i for i, message in enumerate(messages) if message["role"] == "user")
                        preference_at = next(i for i, message in enumerate(messages) if heads[mode][0] in message.get("content", ""))
                        regenerate_at = next(i for i, message in enumerate(messages) if heads[mode][1] in message.get("content", ""))
                        assert preference_at < first_user < regenerate_at
                        print(f"{mode}: compiled prompt heads and message order passed", flush=True)
                        # The header must also remain readable on narrow screens.
                        for label in ("折叠右侧栏", "折叠左侧栏"):
                            collapse = page.get_by_role("button", name=label, exact=True)
                            if await collapse.is_visible():
                                await collapse.click()
                        await page.set_viewport_size({"width": 390, "height": 844})
                        await page.locator(".regenerate-toggle").click()
                        await expect(regeneration.locator("pre")).to_have_text(heads[mode][1])
                        assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                        await page.screenshot(path=str(output / f"{mode}-mobile.png"))
                        await page.set_viewport_size({"width": 1440, "height": 1000})
                    assert not errors, errors
                    assert store.settings_document() == settings
                finally:
                    await browser.close()
        finally:
            # Lifespan closes the service on the server's own event loop.
            server.should_exit = True
            thread.join(10)
            assert not thread.is_alive(), "Browser test server did not shut down"
    print("Both agent modes: read-only heads, suffix-only saves, compiled message order and mobile layout passed.")


if __name__ == "__main__":
    asyncio.run(main())
