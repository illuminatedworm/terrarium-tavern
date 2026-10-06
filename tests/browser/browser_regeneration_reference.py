"""Opt-in rewrite reference, responsive controls and failed-rewrite retry in both modes."""
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
from tests.backend.test_full_agent import Model, ScriptHost
from tests.backend.test_message_versions import generate
from tests.browser.browser_history_navigation import seed


async def layout(page):
    # Wait for the reading marker and rail's ResizeObserver to settle.
    return await page.evaluate("""async () => {
        await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
        await document.fonts.ready;
        const selectors = ['.dialogue-viewport', '.messages', '.message-jumps', '.conversation-rail',
                           '.compose-box', '.rail-tick.current'];
        return Object.fromEntries(selectors.map(selector => {
            const rect = document.querySelector(selector).getBoundingClientRect();
            return [selector, {x: rect.x, y: rect.y, width: rect.width, height: rect.height}];
        }));
    }""")


def same_layout(before, after):
    for selector, rect in before.items():
        for dimension, value in rect.items():
            assert abs(value - after[selector][dimension]) < 1, (selector, dimension, before, after)


async def main():
    screenshots = Path(".cache/regeneration-reference-ui")
    screenshots.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as root:
        app = create_app(root)
        store, service = app.state.store, app.state.service
        card = store.import_card('{"name":"重写参考检查","first_mes":"旧港的故事开始。"}'.encode(), "card.json")
        settings = store.settings()
        settings.full_agent_config.auto_retrieval = False
        settings.retrieval.channels = ["keyword"]
        settings.memory_interval_turns = 50
        store.save_settings(settings)
        service.half.runtime = ScriptHost()
        service.full.runtime = ScriptHost()
        original = "原 AI 回复：她沿着旧港回家。"
        sessions = {}
        for mode in ("half_agent", "full_agent"):
            sid, _, _ = seed(store, card["id"], mode, 56)
            sessions[mode] = generate(store, store.session(sid), original)
        half_models = [ScriptedLLM(["参考原文后的改写"]), ScriptedLLM(["重新从头生成"])]
        full_models = [Model(("read_state", {}), RuntimeError("synthetic interruption")),
                       Model("关闭参考后的改写")]
        half_plan, full_plan = iter(half_models), iter(full_models)
        service.half.provider_factory = lambda: next(half_plan)
        service.full.provider_factory = lambda: next(full_plan)
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
                    for mode, session in sessions.items():
                        page = await browser.new_page(viewport={"width": 1920, "height": 1080})
                        errors = []
                        page.on("pageerror", lambda error: errors.append(str(error)))
                        await page.goto(address)
                        await page.evaluate("id=>localStorage.setItem('tavern-session',id)", session["id"])
                        await page.reload()
                        await expect(page.get_by_role("button", name="发送", exact=True)).to_be_visible()
                        checkbox = page.get_by_role("checkbox", name="带上原 AI 回复作为重写参考", exact=True)
                        await expect(checkbox).to_be_hidden()
                        await page.locator(".regenerate-toggle").click()
                        await expect(checkbox).not_to_be_checked()
                        state = await page.context.storage_state()
                        for width, height, scale, label in [
                            (2560, 1440, 1, "2560"), (1920, 1080, 1, "1920"),
                            (360, 792, 3, "mobile"), (960, 540, 2, "zoom200"),
                        ]:
                            context = await browser.new_context(viewport={"width": width, "height": height},
                                                                device_scale_factor=scale, storage_state=state)
                            try:
                                view = await context.new_page()
                                await view.goto(address)
                                await expect(view.get_by_role("slider", name="对话历史导航")).to_be_visible()
                                await expect(view.locator(".composer-meta")).to_contain_text("tokens")
                                before = await layout(view)
                                await view.locator(".regenerate-toggle").click()
                                option = view.get_by_role("checkbox", name="带上原 AI 回复作为重写参考", exact=True)
                                await expect(option).to_be_visible()
                                await option.scroll_into_view_if_needed()
                                await expect(option).to_be_in_viewport()
                                same_layout(before, await layout(view))
                                assert await view.evaluate("""() =>
                                    [...document.querySelectorAll('.message-jumps button, .conversation-rail')]
                                        .every(node => {
                                            const rect = node.getBoundingClientRect();
                                            return node.contains(document.elementFromPoint(
                                                rect.x + rect.width / 2, rect.y + rect.height / 2));
                                        })""")
                                assert await view.evaluate("""() =>
                                    document.documentElement.scrollWidth <= innerWidth &&
                                    document.documentElement.scrollHeight <= innerHeight""")
                                assert await view.locator(".regenerate-options").evaluate(
                                    "e=>e.scrollWidth<=e.clientWidth+1")
                                await view.screenshot(path=str(screenshots / f"{mode}-{label}.png"))
                                note = view.get_by_role("textbox", name="重新生成补充要求")
                                await note.fill("保留原文的语气")
                                await note.press("Escape")
                                await expect(view.get_by_role("region", name="重新生成选项", exact=True)).to_be_hidden()
                                await expect(view.locator(".regenerate-toggle")).to_be_focused()
                                same_layout(before, await layout(view))
                                await view.locator(".regenerate-toggle").click()
                                await expect(note).to_have_value("保留原文的语气")
                            finally:
                                await context.close()
                        await checkbox.check()
                        await page.get_by_role("textbox", name="重新生成补充要求").fill("保持人物口吻，缩短正文。")
                        await page.locator(".regenerate-bar button").click()
                        if mode == "full_agent":
                            await expect(page.get_by_role("alert")).to_contain_text("未提交", timeout=15000)
                            failed = store.turn(store.session(session["id"])["turns"][0]["id"])
                            assert failed["status"] == "error" and failed["resumable"]
                            assert failed["error"] == "synthetic interruption"
                            assert failed["request"]["regenerate_include_reply"] is True
                            await page.reload()
                            await page.locator(".regenerate-toggle").click()
                            await expect(checkbox).to_be_checked()
                            await checkbox.uncheck()
                            await page.locator(".regenerate-bar button").click()
                            await expect(page.locator(".message.assistant").last).to_contain_text("关闭参考后的改写", timeout=15000)
                            retry = store.turn(store.session(session["id"])["turns"][0]["id"])
                            assert retry["request"]["regenerate_include_reply"] is False
                            assert retry["request"]["resume_turn_id"] is None
                            assert original in json.dumps(full_models[0].requests[0]["messages"], ensure_ascii=False)
                            assert original not in json.dumps(full_models[1].requests[0]["messages"], ensure_ascii=False)
                        else:
                            await expect(page.locator(".message.assistant").last).to_contain_text("参考原文后的改写", timeout=15000)
                            await expect(page.get_by_role("button", name="发送", exact=True)).to_be_visible()
                            await page.locator(".regenerate-toggle").click()
                            await expect(checkbox).not_to_be_checked()
                            await page.locator(".regenerate-bar button").click()
                            await expect(page.locator(".message.assistant").last).to_contain_text("重新从头生成", timeout=15000)
                            assert original in json.dumps(half_models[0].last_messages, ensure_ascii=False)
                            assert "rewrite_reference" not in json.dumps(half_models[1].last_messages)
                        await expect(page.get_by_role("button", name="发送", exact=True)).to_be_visible()
                        current = store.session(session["id"])
                        assert len(current["messages"]) == len(session["messages"])
                        assert not errors, errors
                        await page.close()
                    print("Both modes: rewrite reference requests, retry choice, stable history navigation and responsive layout passed.")
                finally:
                    await browser.close()
        finally:
            server.should_exit = True
            await asyncio.to_thread(thread.join, 10)


if __name__ == "__main__":
    asyncio.run(main())
