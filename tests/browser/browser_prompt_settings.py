"""Prompt editors and individual default restoration against isolated app data."""
import asyncio
import socket
import tempfile
from threading import Thread

import uvicorn
from playwright.async_api import async_playwright, expect

from tavern.application.api import create_app


async def main():
    with tempfile.TemporaryDirectory() as data:
        app = create_app(data)
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
                    page = await browser.new_page(viewport={"width": 1440, "height": 1000})
                    errors = []
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    await page.goto(address)
                    await page.get_by_role("button", name="⚙ 设置", exact=True).click()
                    panel = page.get_by_role("dialog", name="设置", exact=True)
                    await panel.get_by_role("button", name="系统提示词", exact=True).click()
                    definitions = (await (await page.request.get(address + "/api/settings")).json())["prompt_definitions"]
                    await expect(panel.locator(".prompt-setting")).to_have_count(11)
                    for item in definitions:
                        field = panel.get_by_label(item["label"], exact=True)
                        await expect(field).to_have_value(item["default"])
                        await field.fill("自定义 " + item["path"])
                    for item in definitions:
                        await panel.get_by_role("button", name="恢复默认值：" + item["label"], exact=True).click()
                        await expect(panel.get_by_label(item["label"], exact=True)).to_have_value(item["default"])
                    await panel.get_by_label("对话偏好引导词", exact=True).fill("保存后持续生效的测试指令")
                    await panel.get_by_role("button", name="保存设置", exact=True).click()
                    await page.reload()
                    await page.get_by_role("button", name="⚙ 设置", exact=True).click()
                    panel = page.get_by_role("dialog", name="设置", exact=True)
                    await panel.get_by_role("button", name="系统提示词", exact=True).click()
                    await expect(panel.get_by_label("对话偏好引导词", exact=True)).to_have_value("保存后持续生效的测试指令")
                    await panel.get_by_role("button", name="恢复默认值：对话偏好引导词", exact=True).click()
                    await panel.get_by_role("button", name="保存设置", exact=True).click()
                    result = await (await page.request.get(address + "/api/settings")).json()
                    assert result["preference_prompt"] == next(p["default"] for p in definitions if p["path"] == "preference_prompt")
                    assert not errors, errors
                    print("11 prompt editors and reset buttons verified; custom and restored settings persisted.")
                finally:
                    await browser.close()
        finally:
            server.should_exit = True
            await asyncio.to_thread(thread.join, 10)


if __name__ == "__main__":
    asyncio.run(main())
