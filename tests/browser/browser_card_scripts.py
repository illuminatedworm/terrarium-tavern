"""All card-owned script forms remain visible and editable without flattening stored data."""

import asyncio
import json
import re
import socket
import tempfile
from threading import Thread

import uvicorn
from playwright.async_api import async_playwright, expect

from tavern.application.api import create_app


async def main():
    with tempfile.TemporaryDirectory(prefix="tavern-card-scripts-") as root:
        app = create_app(root)
        store = app.state.store
        document = {
            "name": "脚本展示检查", "first_mes": "开场。",
            "extensions": {
                "regex_scripts": [
                    {"id": "r1", "scriptName": "状态面板", "findRegex": "<StatusPlaceHolderImpl/>",
                     "replaceString": '<div class="status">旧面板</div>', "placement": [1, 2],
                     "markdownOnly": True, "promptOnly": False, "disabled": False, "extra": "保留"},
                    {"id": "r2", "scriptName": "停用规则", "findRegex": "x",
                     "replaceString": "y", "disabled": True},
                ],
                "tavern_helper": {"scripts": [{"name": "脚本组", "scripts": [["nested", {
                    "name": "内嵌逻辑", "content": "globalThis.answer = 1;", "enabled": True,
                }]]}]},
                "TavernHelper_scripts": [{"name": "旧版逻辑", "content": "globalThis.old = true;"}],
            },
        }
        card = store.import_card(json.dumps(document, ensure_ascii=False).encode(), "card.json")
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
                    page = await browser.new_page(viewport={"width": 1440, "height": 900})
                    errors = []
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    await page.goto(address)
                    await page.get_by_role("tab", name="角色卡", exact=True).click()
                    await page.locator(".card-side-item").first.click()
                    await page.get_by_role("button", name="脚本与兼容", exact=True).click()
                    groups = page.locator(".card-script-group")
                    regex = groups.nth(0).locator("details.card-script-entry")
                    helper = groups.nth(1).locator("details.card-script-entry")
                    await expect(regex).to_have_count(2)
                    await expect(helper).to_have_count(2)
                    await expect(regex.nth(1).locator(":scope > summary")).to_contain_text("未启用")
                    await expect(helper.nth(1).locator(":scope > summary")).to_contain_text("旧字段未执行")
                    await regex.first.locator(":scope > summary").click()
                    await expect(regex.first.get_by_role("textbox", name="匹配表达式：状态面板", exact=True)).to_have_value(
                        "<StatusPlaceHolderImpl/>")
                    await regex.first.get_by_role("textbox", name="替换内容：状态面板", exact=True).fill(
                        '<div class="status">新面板</div>')
                    await helper.first.locator(":scope > summary").click()
                    await helper.first.get_by_role("textbox", name="内嵌逻辑", exact=True).fill("globalThis.answer = 2;")
                    script_name = helper.first.locator(":scope > label").first.locator("input")
                    await script_name.fill("")
                    await script_name.press_sequentially("新逻辑")
                    await expect(script_name).to_have_value("新逻辑")
                    await page.set_viewport_size({"width": 390, "height": 844})
                    assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                    await page.set_viewport_size({"width": 1440, "height": 900})
                    await page.get_by_role("button", name="保存角色", exact=True).click()
                    await expect(page.get_by_role("button", name="角色设定", exact=True)).to_have_class(re.compile("active"))
                    saved = store.card(card["id"])["document"]["extensions"]
                    assert saved["regex_scripts"][0]["replaceString"] == '<div class="status">新面板</div>'
                    assert saved["regex_scripts"][0]["extra"] == "保留"
                    assert saved["regex_scripts"][0]["placement"] == [1, 2]
                    assert saved["tavern_helper"]["scripts"][0]["scripts"][0][1]["content"] == "globalThis.answer = 2;"
                    assert saved["tavern_helper"]["scripts"][0]["scripts"][0][1]["name"] == "新逻辑"
                    assert saved["TavernHelper_scripts"][0]["content"] == "globalThis.old = true;"
                    assert not errors, errors
                finally:
                    await browser.close()
        finally:
            server.should_exit = True
            await asyncio.to_thread(thread.join, 10)


if __name__ == "__main__":
    asyncio.run(main())
