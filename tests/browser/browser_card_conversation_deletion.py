"""Real-browser regression for sidebar deletion and off-screen editor dialogs."""
import asyncio
import json
import socket
import tempfile
from pathlib import Path
from threading import Thread

import uvicorn
from playwright.async_api import async_playwright, expect

from tavern.application.api import create_app
from tavern.core.models import CardEditorRequest, PersonaEdit, SessionCreate
from tests.backend.test_card_editor import ToolLLM, import_fixture


async def sidebar(page, *, opened=True):
    label = "展开左侧栏" if opened else "折叠左侧栏"
    button = page.get_by_role("button", name=label, exact=True)
    if await button.is_visible():
        await button.click()
    elif opened:
        button = page.get_by_role("button", name="切换左侧栏", exact=True)
        if await button.is_visible():
            await button.click()


async def visible_dialog(page, dialog):
    await expect(dialog).to_be_visible()
    box = await dialog.bounding_box()
    size = page.viewport_size
    assert box and box["x"] >= 0 and box["y"] >= 0, box
    assert box["x"] + box["width"] <= size["width"] + 1, box
    assert box["y"] + box["height"] <= size["height"] + 1, box
    assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth && document.documentElement.scrollHeight <= innerHeight")


async def open_delete(page, wid):
    await sidebar(page)
    row = page.locator(f'[data-conversation-id="{wid}"]')
    await row.hover()
    await row.get_by_role("button", name="删除制卡对话", exact=False).click()
    dialog = page.get_by_role("alertdialog")
    await visible_dialog(page, dialog)
    await expect(dialog).to_contain_text("聊天、草稿和撤销记录")
    return dialog


async def main():
    output = Path(".cache/card-authoring-ui/deletion")
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="card-delete-ui-") as root:
        app = create_app(root)
        store, editor = app.state.store, app.state.editor
        settings = store.settings()
        settings.retrieval.channels = ["keyword"]
        store.save_settings(settings)
        card = import_fixture(store)
        persona = store.save_persona(None, PersonaEdit(name="保留用户角色", description="保存的介绍"))
        for mode in ("half_agent", "full_agent"):
            story = store.new_session(SessionCreate(card_id=card["id"], persona_id=persona["id"], agent_mode=mode))
            store.update_session(story["id"], 0, title=mode)
        other = editor.edits.create_conversation("另一个制卡对话的长标题" * 8)
        current = editor.edits.create_conversation("当前制卡对话")
        current = editor.edits.attach(current["id"], card["id"], current["revision"])
        pending = editor.edits.create_conversation("有待续进度的对话")

        class FailedModel(ToolLLM):
            async def chat(self, messages, **kwargs):
                raise ValueError("隔离夹具模拟中断")
                yield ""

        editor.provider_factory = lambda: FailedModel([])
        run = editor.submit(pending["id"], CardEditorRequest(text="查看卡片", expected_revision=0, operation_id="pending"), legacy=False)
        await editor.tasks[run["id"]]
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
                    page = await browser.new_page(viewport={"width": 1920, "height": 1080})
                    errors = []
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    await page.add_init_script("localStorage.setItem('tavern-sidebar-section','editor');"
                        + f"localStorage.setItem('tavern-active-card-conversation',{json.dumps(current['id'])});")
                    await page.goto(address)
                    header = page.locator(".cw-header h1")
                    await expect(header).to_have_text(current["title"])
                    composer = page.get_by_role("textbox", name="制卡要求", exact=True)
                    await composer.fill("保留未发送的要求")
                    await sidebar(page)
                    await expect(page.locator(".session-row[data-conversation-id]")).to_have_count(3)
                    protected = page.locator(f'[data-conversation-id="{pending["id"]}"]')
                    await protected.hover()
                    await expect(protected.get_by_role("button", name="删除制卡对话", exact=False)).to_be_disabled()
                    response = await page.request.delete(address + f'/api/card-editor/conversations/{pending["id"]}',
                                                         data={"expected_revision": 0})
                    assert response.status == 409

                    # Both existing story modes still use the story delete action.
                    await page.get_by_role("tab", name="会话", exact=True).click()
                    for mode in ("half_agent", "full_agent"):
                        trigger = page.get_by_role("button", name=f"删除会话 {mode}", exact=True, include_hidden=True)
                        await trigger.locator("..").hover()
                        await trigger.click()
                        dialog = page.get_by_role("alertdialog")
                        await expect(dialog).to_contain_text("剧情分支")
                        await expect(dialog).not_to_contain_text("聊天、草稿")
                        await dialog.get_by_role("button", name="取消", exact=True).click()
                    await page.get_by_role("tab", name="制卡", exact=True).click()

                    for width, height, label in ((2560, 1440, "desktop-2560"), (1920, 1080, "desktop-1920"),
                                                 (960, 540, "zoom-200-equivalent")):
                        await page.set_viewport_size({"width": width, "height": height})
                        await page.get_by_role("button", name="删除对话", exact=True).click()
                        dialog = page.get_by_role("dialog", name="制卡对话操作", exact=True)
                        await visible_dialog(page, dialog)
                        assert await page.locator(".cw-overlay").evaluate("el=>getComputedStyle(el).position") == "fixed"
                        assert await page.evaluate("!!document.activeElement.closest('[role=dialog]')")
                        await page.keyboard.press("Shift+Tab")
                        assert await page.evaluate("!!document.activeElement.closest('[role=dialog]')")
                        await page.screenshot(path=str(output / f"header-{label}.png"))
                        await page.keyboard.press("Escape")
                        await expect(dialog).to_have_count(0)
                        assert not await page.locator(".shell").evaluate("el=>el.inert")
                        await expect(page.get_by_role("button", name="删除对话", exact=True)).to_be_focused()
                        popover = await open_delete(page, other["id"])
                        await page.screenshot(path=str(output / f"sidebar-{label}.png"))
                        await popover.evaluate("el=>el.scrollTop=el.scrollHeight")
                        await expect(popover).to_be_visible()
                        await popover.get_by_role("button", name="取消", exact=True).click()
                        await expect(header).to_have_text(current["title"])

                    mobile = await browser.new_page(viewport={"width": 360, "height": 792}, device_scale_factor=3,
                                                    is_mobile=True, has_touch=True)
                    mobile.on("pageerror", lambda error: errors.append(str(error)))
                    await mobile.add_init_script("localStorage.setItem('tavern-sidebar-section','editor');"
                        + f"localStorage.setItem('tavern-active-card-conversation',{json.dumps(current['id'])});")
                    await mobile.goto(address)
                    await expect(mobile.locator(".cw-header h1")).to_have_text(current["title"])
                    await sidebar(mobile, opened=False)
                    await mobile.get_by_role("button", name="删除对话", exact=True).click()
                    dialog = mobile.get_by_role("dialog", name="制卡对话操作", exact=True)
                    await visible_dialog(mobile, dialog)
                    await mobile.screenshot(path=str(output / "header-mobile-1080x2376.png"))
                    await dialog.get_by_role("button", name="取消", exact=True).click()
                    popover = await open_delete(mobile, other["id"])
                    await mobile.screenshot(path=str(output / "sidebar-mobile-1080x2376.png"))
                    await popover.get_by_role("button", name="取消", exact=True).click()
                    await mobile.close()

                    await page.set_viewport_size({"width": 1920, "height": 1080})
                    pattern = f"**/api/card-editor/conversations/{other['id']}"
                    async def reject_delete(route):
                        if route.request.method == "DELETE":
                            await route.fulfill(status=409, json={"detail": "删除版本冲突"})
                        else:
                            await route.continue_()
                    await page.route(pattern, reject_delete)
                    popover = await open_delete(page, other["id"])
                    await popover.get_by_role("button", name="彻底删除", exact=True).click()
                    await expect(page.get_by_role("alert").filter(has_text="删除版本冲突").first).to_be_visible()
                    await expect(header).to_have_text(current["title"])
                    assert not await page.locator(".shell").evaluate("el=>el.inert")
                    assert editor.edits.conversation(other["id"])
                    await page.unroute(pattern, reject_delete)

                    await page.evaluate("key=>sessionStorage.setItem(key,'draft')", f"tavern-editor-input-{other['id']}")
                    popover = await open_delete(page, other["id"])
                    await popover.get_by_role("button", name="彻底删除", exact=True).click()
                    await expect(page.locator(f'[data-conversation-id="{other["id"]}"]')).to_have_count(0)
                    await expect(header).to_have_text(current["title"])
                    await expect(composer).to_have_value("保留未发送的要求")
                    assert await page.evaluate("key=>sessionStorage.getItem(key)", f"tavern-editor-input-{other['id']}") is None

                    await page.get_by_role("button", name="删除对话", exact=True).click()
                    dialog = page.get_by_role("dialog", name="制卡对话操作", exact=True)
                    await visible_dialog(page, dialog)
                    await dialog.get_by_role("button", name="确认", exact=True).click()
                    await expect(header).to_have_text(pending["title"])
                    assert not await page.locator(".shell").evaluate("el=>el.inert")
                    assert await page.evaluate("key=>sessionStorage.getItem(key)", f"tavern-editor-input-{current['id']}") is None
                    await page.get_by_role("button", name="丢弃本轮", exact=True).click()
                    await page.get_by_role("dialog").get_by_role("button", name="确认", exact=True).click()
                    await expect(page.get_by_role("button", name="发送", exact=True)).to_be_visible()
                    popover = await open_delete(page, pending["id"])
                    await popover.get_by_role("button", name="彻底删除", exact=True).click()
                    await expect(page.locator(".card-workspace")).to_have_count(0)
                    await expect(page.get_by_text("从上方新建制卡对话。", exact=True)).to_be_visible()
                    assert not await page.locator(".shell").evaluate("el=>el.inert")
                    assert await page.evaluate("localStorage.getItem('tavern-active-card-conversation')") is None
                    assert editor.edits.conversations() == []
                    assert len(store.cards()) == 1 and len(store.personas()) == 1 and len(store.sessions()) == 2
                    await page.get_by_role("button", name="＋ 新制卡对话", exact=True).click()
                    await expect(composer).to_be_visible()
                    assert not errors, errors
                    print("Card conversation deletion passed: sidebar/header, cancel/confirm, failure, active/other/last, protected progress, preserved library, desktop/mobile/zoom.")
                finally:
                    await browser.close()
        finally:
            server.should_exit = True
            await asyncio.to_thread(thread.join, 5)


if __name__ == "__main__":
    asyncio.run(main())
