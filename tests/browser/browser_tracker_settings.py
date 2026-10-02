"""Tracker controls, prompt save/undo and responsive layout in an isolated app."""
import asyncio
import json
import socket
import tempfile
from pathlib import Path
from threading import Thread

import uvicorn
from playwright.async_api import async_playwright, expect

from tavern.application.api import create_app


async def main():
    output = Path(".cache/tracker-browser")
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="tavern-tracker-browser-") as root:
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
                checks, errors = [], []
                try:
                    for width, height, scale, label in (
                        (1920, 1080, 1, "desktop"), (2560, 1440, 1, "large-desktop"),
                        (360, 792, 3, "mobile-1080x2376"), (960, 540, 2, "zoom-200"),
                    ):
                        context = await browser.new_context(viewport={"width": width, "height": height},
                                                            device_scale_factor=scale)
                        page = await context.new_page()
                        page.on("pageerror", lambda error: errors.append(str(error)))
                        await page.goto(address)
                        if width < 760:
                            await page.get_by_role("button", name="切换左侧栏" if width < 620 else "展开左侧栏", exact=True).click()
                        await page.get_by_role("button", name="⚙ 设置", exact=True).click()
                        dialog = page.get_by_role("dialog", name="设置", exact=True)
                        initial_height = (await dialog.bounding_box())["height"]

                        async def navigate(key, title):
                            if width <= 760:
                                await dialog.locator(".settings-mode-buttons").get_by_role("button", name="half agent", exact=True).click()
                                await dialog.get_by_role("combobox", name="设置页面", exact=True).select_option("half_agent." + key)
                            else:
                                await dialog.get_by_role("button", name="half agent " + title, exact=True).click()

                        async def saved():
                            await expect(page.locator(".settings-save-status")).to_contain_text("已保存")

                        await navigate("memory", "历史与记忆")
                        limit = page.get_by_label("Tracker 每次任务模型调用上限", exact=True)
                        await expect(limit).to_have_value("8")
                        await limit.fill("12")
                        await saved()
                        settings = await (await page.request.get(address + "/api/settings")).json()
                        assert settings["half_agent"]["tracker_max_steps"] == 12
                        await dialog.get_by_role("button", name="撤回本页修改", exact=True).click()
                        await expect(limit).to_have_value("8")
                        await saved()
                        await page.screenshot(path=str(output / f"{label}-memory.png"))

                        await navigate("prompts", "提示词")
                        await dialog.get_by_role("button", name="Tracker 系统提示词", exact=True).click()
                        field = dialog.get_by_role("textbox", name="Tracker 系统提示词", exact=True)
                        original = await field.input_value()
                        assert "负责维护故事的连续性" in original
                        await expect(dialog.get_by_role("button", name="记忆系统提示词", exact=True)).to_have_count(0)
                        await expect(dialog.get_by_role("button", name="设定维护提示词", exact=True)).to_have_count(0)
                        await field.fill("Tracker 浏览器隔离测试")
                        await dialog.get_by_role("button", name="保存此文本：Tracker 系统提示词", exact=True).click()
                        await expect(dialog.get_by_role("button", name="保存此文本：Tracker 系统提示词", exact=True)).to_have_count(0)
                        await saved()
                        assert (await (await page.request.get(address + "/api/settings")).json())["half_agent"]["tracker_system_prompt"] == "Tracker 浏览器隔离测试"
                        await dialog.get_by_role("button", name="撤回本页修改", exact=True).click()
                        await expect(field).to_have_value(original)
                        await saved()
                        assert abs((await dialog.bounding_box())["height"] - initial_height) < 1
                        assert await page.evaluate("document.documentElement.scrollWidth<=innerWidth && document.documentElement.scrollHeight<=innerHeight")
                        assert await dialog.evaluate("e=>e.scrollWidth<=e.clientWidth+1")
                        await page.screenshot(path=str(output / f"{label}-prompt.png"))
                        checks.append(label)
                        await context.close()
                    assert not errors, errors
                    (output / "result.json").write_text(json.dumps({"checks": checks, "errors": errors}), encoding="utf-8")
                    print("Tracker settings passed: " + ", ".join(checks))
                finally:
                    await browser.close()
        finally:
            server.should_exit = True
            await asyncio.to_thread(thread.join, 10)


if __name__ == "__main__":
    asyncio.run(main())
