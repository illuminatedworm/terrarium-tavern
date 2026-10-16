"""Real ZIP download/upload, settings restore, and continued story regression."""
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
from tavern.core.models import PersonaEdit, SessionCreate
from tavern.storage.transfer import read_package


async def main():
    with tempfile.TemporaryDirectory(prefix="tavern-transfer-browser-") as root:
        app = create_app(root)
        store = app.state.store
        persona = store.save_persona(None, PersonaEdit(name="测试玩家", description="钟楼旅人"))
        card = store.import_card(json.dumps({"name": "钟楼约定", "first_mes": "她在钟楼下等你。"}).encode(), "card.json")
        session = store.new_session(SessionCreate(card_id=card["id"], persona_id=persona["id"]))
        doc = store.settings_document()
        doc.half_agent.foreground.model = "exported-model"
        # Half Agent sends only when the Tracker model is configured too.
        doc.half_agent.background.model = "exported-tracker"
        store.save_settings(doc)
        app.state.service.provider_factory = lambda: ScriptedLLM(["导入后的故事继续了。"])
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
                    page = await browser.new_page(viewport={"width": 1536, "height": 1024})
                    errors = []
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    await page.goto(address)
                    await page.get_by_role("button", name="设置", exact=True).click()
                    await page.get_by_role("button", name="通用 数据导入导出", exact=True).click()
                    await expect(page.get_by_role("button", name="导出所选数据")).to_be_enabled()
                    # Empty selections must never download a misleading empty package.
                    for label in ("个人设置", "用户角色", "角色卡", "对话"):
                        await page.get_by_role("checkbox", name=label, exact=True).uncheck()
                    await expect(page.get_by_role("button", name="导出所选数据")).to_be_disabled()
                    for label in ("个人设置", "用户角色", "角色卡", "对话"):
                        await page.get_by_role("checkbox", name=label, exact=True).check()
                    await page.get_by_role("button", name="选择对话条目", exact=True).click()
                    await page.get_by_role("button", name="全不选", exact=True).click()
                    await expect(page.get_by_text("已选 0 个 / 共 1 个 · 含消息、记忆、变量与分支", exact=True)).to_be_visible()
                    await page.get_by_role("checkbox", name="钟楼约定 Half Agent", exact=True).check()
                    await page.get_by_role("button", name="选择对话条目", exact=True).click()
                    async with page.expect_download() as downloading:
                        await page.get_by_role("button", name="导出所选数据", exact=True).click()
                    download = await downloading.value
                    archive = Path(root) / "browser-export.zip"
                    await download.save_as(archive)
                    payload, blobs = read_package(archive.read_bytes(), store)
                    assert len(payload["sessions"]) == len(payload["cards"]) == len(payload["personas"]) == 1
                    assert not payload["includes_api_keys"]
                    assert payload["settings"]["document"]["half_agent"]["foreground"]["model"] == "exported-model"
                    assert payload["settings"]["browser"]["layout"]["fontSize"] == 18
                    changed = store.settings_document()
                    changed.half_agent.foreground.model = "changed-model"
                    store.save_settings(changed)
                    await page.get_by_label("导入数据包", exact=True).set_input_files(archive)
                    await expect(page.get_by_role("heading", name="数据包预览")).to_be_visible()
                    restore = page.get_by_role("checkbox", name="恢复包内个人设置，替换当前设置与阅读偏好", exact=True)
                    await expect(restore).not_to_be_checked()
                    await restore.check()
                    await page.get_by_role("button", name="确认导入", exact=True).click()
                    await expect(page.get_by_text("导入完成：1 个用户角色、1 张角色卡、1 个对话。个人设置已恢复。", exact=True)).to_be_visible()
                    assert len(store.sessions()) == 2
                    assert store.settings_document().half_agent.foreground.model == "exported-model"
                    imported_id = next(item["id"] for item in store.sessions() if item["id"] != session["id"])
                    await page.get_by_role("button", name="打开导入的对话，继续故事", exact=True).click()
                    await page.get_by_placeholder("说些什么，或描述你的行动…").fill("继续前进")
                    await page.get_by_role("button", name="发送", exact=True).click()
                    await expect(page.get_by_text("导入后的故事继续了。", exact=True)).to_be_visible(timeout=15000)
                    assert store.session(imported_id)["messages"][-1]["raw"] == "导入后的故事继续了。"
                    await page.get_by_role("button", name="设置", exact=True).click()
                    # New settings must become the editor's baseline, not an unsaved rollback.
                    await page.get_by_role("button", name="half agent 模型与生成", exact=True).click()
                    await expect(page.get_by_role("region", name="正文生成", exact=True).get_by_role("button", name="模型名称", exact=True)).to_have_attribute("data-value", "exported-model")
                    await page.get_by_role("button", name="通用 数据导入导出", exact=True).click()
                    # Re-entering restores a fresh working page; invalid archives stay visible as errors.
                    invalid = Path(root) / "bad.zip"
                    invalid.write_bytes(b"broken ZIP")
                    await page.get_by_label("导入数据包", exact=True).set_input_files(invalid)
                    await expect(page.get_by_role("alert")).to_contain_text("无法读取数据包")
                    await expect(page.get_by_role("button", name="确认导入")).to_have_count(0)
                    await page.set_viewport_size({"width": 390, "height": 844})
                    assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                    assert await page.locator(".settings-page-content").evaluate("e=>e.scrollWidth<=e.clientWidth")
                    await expect(page.get_by_role("combobox", name="设置页面", exact=True)).to_have_value("shared.data_transfer")
                    assert not errors, errors
                    print("PASS: download, selection, preview, settings restore, invalid ZIP, mobile, and continued story")
                finally:
                    await browser.close()
        finally:
            server.should_exit = True
            thread.join(timeout=10)


if __name__ == "__main__":
    asyncio.run(main())
