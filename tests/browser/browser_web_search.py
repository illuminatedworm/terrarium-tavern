"""Shared web settings, independent modes and source/activity UI in an isolated app."""
import asyncio
import json
import socket
import tempfile
from pathlib import Path
from threading import Thread

import httpx
import uvicorn
from playwright.async_api import async_playwright, expect

from tavern.application.api import create_app
from tavern.core.models import CardEditorRequest
from tests.backend.test_card_editor import ToolLLM
from tests.browser.browser_release import assert_viewport

OUTPUT = Path(".agents/.cache/web-search-validation")


async def main():
    OUTPUT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="app-", dir=OUTPUT) as root:
        app = create_app(root)
        store = app.state.store
        app.state.editor.provider_factory = lambda: ToolLLM([])
        doc = store.settings_document()
        doc.shared.web.exa.api_key = "browser-fixture-secret"
        store.save_settings(doc)
        editor = app.state.editor
        workspace = editor.edits.create_conversation("联网验收")
        run = editor.submit(workspace["id"], CardEditorRequest(text="查阅资料", expected_revision=0, operation_id="browser-source-fixture"), legacy=False)
        await editor.tasks[run["id"]]
        assert editor.edits.run(run["id"])["status"] == "completed"
        # Saved public events must survive opening an old conversation and log cleanup.
        editor.edits.event(run["id"], "web_activity", {"service": "tavily", "auth_mode": "keyless", "operation": "search", "status": "completed"})
        editor.edits.event(run["id"], "web_sources", {"sources": [{"url": "https://example.org/source", "title": "官方来源"}]})
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
            checked, errors = [], []
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch()
                for width, height, scale in [(2560, 1440, 1), (1920, 1080, 1), (360, 792, 3), (960, 540, 2)]:
                    context = await browser.new_context(viewport={"width": width, "height": height}, device_scale_factor=scale)
                    page = await context.new_page()
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    await page.goto(address)
                    if width < 760:
                        await page.get_by_role("button", name="切换左侧栏" if width < 620 else "展开左侧栏", exact=True).click()
                    await page.get_by_role("button", name="⚙ 设置", exact=True).click()
                    dialog = page.get_by_role("dialog", name="设置", exact=True)
                    await expect(dialog.locator(".settings-layout")).to_be_visible()
                    before_height = (await dialog.bounding_box())["height"]
                    async def navigate(key, label):
                        if width <= 760:
                            await dialog.locator(".settings-mode-buttons").get_by_role("button", name="通用", exact=True).click()
                            await dialog.get_by_role("combobox", name="设置页面", exact=True).select_option("shared." + key)
                        else:
                            await dialog.locator(".settings-pages").get_by_role("button", name="通用 " + label, exact=True).click()
                    await navigate("web", "联网服务")
                    await expect(page.get_by_label("Exa MCP 接入方式", exact=True)).to_be_visible()
                    await page.get_by_label("Exa MCP 接入方式", exact=True).select_option("api_key")
                    key = page.get_by_label("Exa MCP API key", exact=True)
                    await expect(key).to_have_value("")
                    assert "browser-fixture-secret" not in await page.content()
                    await key.fill("replacement-browser-key")
                    await expect(key).to_have_value("", timeout=10000)
                    await expect(dialog.get_by_role("button", name="撤回本页修改", exact=True)).to_be_enabled()
                    await page.get_by_role("button", name="下移 Exa MCP", exact=True).click()
                    await expect(dialog.locator(".web-settings legend").first).to_contain_text("Tavily")
                    await page.get_by_role("button", name="上移 Exa MCP", exact=True).click()
                    await page.get_by_label("Exa MCP 接入方式", exact=True).select_option("keyless")
                    await navigate("advisor", "参谋 agent")
                    await page.get_by_label("联网方式", exact=True).select_option("local")
                    await page.get_by_role("button", name="配置共享联网服务与密钥", exact=True).click()
                    await expect(dialog.locator(".web-settings")).to_be_visible()
                    await navigate("editor", "角色卡编辑 agent")
                    await expect(page.get_by_label("联网方式", exact=True)).to_have_value("auto")
                    await page.get_by_label("联网方式", exact=True).select_option("off")
                    await page.get_by_role("button", name="配置共享联网服务与密钥", exact=True).click()
                    await expect(dialog.locator(".web-settings")).to_be_visible()
                    assert abs((await dialog.bounding_box())["height"] - before_height) < 1
                    label = f"{width}x{height}-scale{scale}"
                    await assert_viewport(page, label)
                    await page.screenshot(path=str(OUTPUT / f"settings-{label}.png"))
                    await dialog.get_by_role("button", name="关闭", exact=True).click()
                    async with httpx.AsyncClient(base_url=address, trust_env=False) as client:
                        current = (await client.get("/api/settings")).json()
                        assert current["shared"]["advisor"]["web"]["mode"] == "local"
                        assert current["shared"]["card_editor_config"]["web"]["mode"] == "off"
                        assert current["shared"]["web"]["exa"]["has_api_key"]
                        result = await client.patch("/api/settings/shared", json={"expected_revision": current["revisions"]["shared"],
                            "changes": {"advisor.web.mode": "auto", "card_editor_config.web.mode": "auto"}})
                        result.raise_for_status()
                    await page.get_by_role("tab", name="制卡", exact=True).click()
                    entry = page.locator(f'[data-conversation-id="{workspace["id"]}"] .session-open')
                    if await entry.is_visible():
                        await entry.click()
                    else:
                        # Mobile auto-opens the existing conversation and closes its drawer.
                        await expect(page.get_by_role("textbox", name="制卡要求", exact=True)).to_be_visible()
                    await expect(page.locator(".web-sources summary")).to_contain_text("网页来源")
                    await page.locator(".web-sources summary").click()
                    await expect(page.get_by_role("link", name="官方来源", exact=True)).to_have_attribute("href", "https://example.org/source")
                    await page.locator(".cw-run summary").first.click()
                    await expect(page.get_by_text("Tavily · 免 key · 完成", exact=True)).to_be_visible()
                    await assert_viewport(page, label + ":sources")
                    await page.screenshot(path=str(OUTPUT / f"sources-{label}.png"))
                    checked.append(label)
                    await context.close()
                await browser.close()
            assert not errors, errors
            report = {"checked": checked, "errors": errors, "zoom_note": "200% uses a 960×540 CSS viewport at device scale 2; mobile uses 360×792 at scale 3 (1080×2376 device pixels)."}
            (OUTPUT / "browser.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps(report, ensure_ascii=False))
        finally:
            server.should_exit = True
            await asyncio.to_thread(thread.join, 10)


if __name__ == "__main__":
    asyncio.run(main())
