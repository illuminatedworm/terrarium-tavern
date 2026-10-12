"""Optional outline editing, spoiler disclosure, conflicts and responsive layouts."""
import asyncio
import json
import re
import socket
import tempfile
from pathlib import Path
from threading import Thread

import uvicorn
from playwright.async_api import async_playwright, expect

from tavern.application.api import create_app
from tests.backend.test_full_agent import Model, ScriptHost


async def main():
    output = Path(".cache/full-agent-plot-ui")
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as data:
        app = create_app(data)
        store = app.state.store
        card = store.import_card(json.dumps({"name": "剧情规划验收", "first_mes": "你来到了港口。"}).encode(), "plot.json")
        config = store.settings_document()
        config.full_agent.model.model = "fixture"
        config.shared.providers[0].api_key = "fixture-local-only"
        config.full_agent.tools.auto_retrieval = False
        store.save_settings(config)
        app.state.service.full.runtime = ScriptHost()
        app.state.service.full.provider_factory = lambda: Model(
            ("write_plot", {"content": "SPOILER_守塔人是寄信者。近期：若玩家追问信件，再揭露线索。"}),
            "她望向灯塔，等待你作出决定。")
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
                page = await browser.new_page(viewport={"width": 1920, "height": 1080})
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                await page.goto(address)
                await page.get_by_role("tab", name="会话", exact=True).click()
                await page.get_by_role("button", name="＋ 开始新故事", exact=True).click()
                await page.get_by_role("button", name=f"选择角色卡 {card['name']}").click()
                await page.get_by_label("运行模式", exact=True).select_option("full_agent")
                await page.get_by_role("button", name="进入故事 →", exact=True).click()
                await page.locator(".story").wait_for()
                await page.get_by_role("button", name="变量与记忆", exact=True).click()
                modal = page.get_by_role("dialog")
                plot = modal.get_by_role("region", name="剧情规划", exact=True)
                toggle = plot.get_by_label("开启剧情规划", exact=True)
                await expect(toggle).not_to_be_checked()
                await expect(plot.locator("details").first).not_to_have_attribute("open", "")
                await toggle.check()
                await expect(toggle).to_be_checked()
                await expect(toggle).to_be_enabled()
                await modal.get_by_role("button", name="关闭", exact=True).click()
                await page.get_by_placeholder("说些什么，或描述你的行动…").fill("我走向灯塔。")
                await page.get_by_role("button", name="发送", exact=True).click()
                await expect(page.locator(".message.assistant").last).to_contain_text("她望向灯塔", timeout=30000)
                call = page.locator(".agent-activity").filter(has=page.locator(".activity-name", has_text="write_plot"))
                await expect(call.locator("summary")).to_contain_text("包含未来剧情")
                assert "SPOILER_" not in await call.inner_text()
                await page.get_by_role("button", name="变量与记忆", exact=True).click()
                modal = page.get_by_role("dialog")
                plot = modal.get_by_role("region", name="剧情规划", exact=True)
                await expect(plot.locator("details").first).not_to_have_attribute("open", "")
                before = await modal.bounding_box()
                await plot.get_by_text("查看和编辑大纲（含未来剧情）", exact=True).click()
                editor = plot.get_by_label("阶段大纲", exact=True)
                await expect(editor).to_have_value(re.compile("SPOILER_"))
                after = await modal.bounding_box()
                assert abs(after["height"] - before["height"]) <= 2
                await editor.fill("玩家调整：让守塔人保留秘密，先谈天气。")
                await plot.get_by_role("button", name="保存大纲", exact=True).click()
                await expect(plot.get_by_role("button", name="保存大纲", exact=True)).to_be_disabled()
                await plot.get_by_label("开启剧情规划", exact=True).uncheck()
                await expect(plot.get_by_label("开启剧情规划", exact=True)).to_be_enabled()
                await expect(editor).to_have_value("玩家调整：让守塔人保留秘密，先谈天气。")

                # An external edit cannot silently replace unsaved player input.
                await editor.fill("保留我的未提交草稿")
                session = store.session(store.sessions()[0]["id"])
                response = await page.request.put(address + f"/api/sessions/{session['id']}/full-agent/plot", data={
                    "expected_revision": session["revision"], "content": "外部已保存的新版本"})
                assert response.ok
                await plot.get_by_role("button", name="保存大纲", exact=True).click()
                await expect(plot.get_by_role("alert")).to_contain_text("会话已改变")
                await expect(editor).to_have_value("保留我的未提交草稿")
                await plot.get_by_role("button", name="刷新会话（保留编辑内容）", exact=True).click()
                await expect(plot.get_by_role("alert")).to_contain_text("已刷新")
                await expect(editor).to_have_value("保留我的未提交草稿")
                await plot.get_by_text("核对当前已保存版本", exact=True).click()
                await expect(plot.locator(".plot-saved-text")).to_have_text("外部已保存的新版本")
                await plot.get_by_role("button", name="保存大纲", exact=True).click()
                await expect(plot.get_by_role("button", name="保存大纲", exact=True)).to_be_disabled()
                current = store.session(session["id"])
                assert current["snapshot"]["full_agent"]["plot"]["text"] == "保留我的未提交草稿"
                await plot.get_by_role("button", name="清空编辑内容", exact=True).click()
                assert store.session(session["id"])["snapshot"]["full_agent"]["plot"]["text"]
                await plot.get_by_role("button", name="保存大纲", exact=True).click()
                await expect(plot.get_by_role("button", name="保存大纲", exact=True)).to_be_disabled()
                assert not store.session(session["id"])["snapshot"]["full_agent"]["plot"]["text"]
                await modal.get_by_role("button", name="关闭", exact=True).click()

                if not await page.get_by_role("button", name="工具", exact=True).is_visible():
                    await page.get_by_role("button", name="展开右侧栏", exact=True).click()
                await page.get_by_role("button", name="工具", exact=True).click()
                tool = page.get_by_role("region", name="Full Agent 工具").get_by_label("启用 write_plot", exact=True)
                await expect(tool).not_to_be_checked()
                await expect(tool).to_be_disabled()
                saved_browser_state = await page.context.storage_state()
                for width, height, scale, label in [
                    (2560, 1440, 1, "2560"), (1920, 1080, 1, "1920"),
                    (360, 792, 3, "mobile1080"), (960, 540, 2, "zoom200"),
                ]:
                    context = await browser.new_context(viewport={"width": width, "height": height},
                                                        device_scale_factor=scale, storage_state=saved_browser_state)
                    try:
                        viewport = await context.new_page()
                        viewport.on("pageerror", lambda error: errors.append(str(error)))
                        await viewport.goto(address)
                        await viewport.get_by_role("button", name="变量与记忆", exact=True).click()
                        dialog = viewport.get_by_role("dialog")
                        region = dialog.get_by_role("region", name="剧情规划", exact=True)
                        collapsed = await dialog.bounding_box()
                        await region.get_by_text("查看和编辑大纲（含未来剧情）", exact=True).click()
                        textarea = region.get_by_label("阶段大纲", exact=True)
                        await textarea.fill("很长的阶段大纲。\n" * 500)
                        await region.get_by_role("button", name="保存大纲", exact=True).scroll_into_view_if_needed()
                        expanded = await dialog.bounding_box()
                        assert abs(expanded["height"] - collapsed["height"]) <= 2, label
                        assert await viewport.evaluate("document.documentElement.scrollWidth <= innerWidth"), label
                        assert await viewport.evaluate("document.documentElement.scrollHeight <= innerHeight"), label
                        assert await textarea.evaluate("el => el.scrollHeight > el.clientHeight"), label
                        await viewport.screenshot(path=str(output / f"plot-{label}.png"))
                        await dialog.get_by_role("button", name="关闭", exact=True).click()
                        await expect(dialog).not_to_be_visible()
                    finally:
                        await context.close()
                assert not errors, errors
                await browser.close()
        finally:
            server.should_exit = True
            thread.join(timeout=15)
    print("Full Agent plot browser acceptance passed: editing, conflicts, spoilers, four viewports.")


if __name__ == "__main__":
    asyncio.run(main())
