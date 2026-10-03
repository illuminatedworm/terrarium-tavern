"""Actual UI save/readback for global, story and authoring adapter preferences."""
import asyncio
import json
import socket
import tempfile
from pathlib import Path
from threading import Thread

import uvicorn
from playwright.async_api import async_playwright, expect

from tavern.application.api import create_app
from tavern.core.models import SessionCreate


async def main():
    output = Path(".cache/message-adapter-ui")
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as directory:
        app = create_app(directory)
        store = app.state.store
        card = store.import_card(json.dumps({"name": "适配检查", "first_mes": "旧港的灯亮了。"}).encode(), "fixture.json")
        document = store.settings_document()
        document.half_agent.retrieval.channels = ["keyword"]
        document.full_agent.tools.auto_retrieval = False
        store.save_settings(document)
        sessions = {mode: store.new_session(SessionCreate(card_id=card["id"], agent_mode=mode))
                    for mode in ("half_agent", "full_agent")}
        conversation = app.state.editor.edits.create_conversation("消息适配检查")
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
                    await page.get_by_role("button", name="⚙ 设置", exact=True).click()
                    settings_dialog = page.get_by_role("dialog", name="设置", exact=True)
                    await settings_dialog.get_by_role("button", name="half agent 模型与生成", exact=True).click()
                    writer = settings_dialog.get_by_role("region", name="正文生成", exact=True)
                    await writer.get_by_text("消息适配：发送角色与标签包裹", exact=True).click()
                    await writer.get_by_label("剧情状态发送角色", exact=True).select_option("user")
                    await writer.get_by_label("剧情状态标签包裹", exact=True).uncheck()
                    await expect(settings_dialog.locator(".settings-save-status")).to_contain_text("已保存")
                    # Readback waits for the debounced global save, rather than a stale previous status.
                    for _ in range(60):
                        current = store.settings_document().half_agent.foreground.message_adapter.model_dump()
                        if current.get("mappings", {}).get("chat_completions", {}).get("state") == {"role": "user", "wrap": False}:
                            break
                        await asyncio.sleep(.05)
                    else:
                        raise AssertionError("Global adapter save did not persist")
                    assert store.message_adapter("story", sessions["half_agent"]["id"], "foreground")["config"]["mappings"] == {}
                    future = store.new_session(SessionCreate(card_id=card["id"]))
                    assert store.message_adapter("story", future["id"], "foreground")["config"] == current
                    await settings_dialog.get_by_role("button", name="关闭", exact=True).click()
                    for mode, session in sessions.items():
                        await page.evaluate("id=>localStorage.setItem('tavern-session',id)", session["id"])
                        await page.reload()
                        await expect(page.get_by_role("button", name="发送", exact=True)).to_be_visible()
                        expand = page.get_by_role("button", name="展开右侧栏", exact=True)
                        if await expand.is_visible():
                            await expand.click()
                        await page.locator(".inspector-tabs").get_by_role("button", name="会话设置", exact=True).click()
                        await page.get_by_role("region", name="会话设置", exact=True).get_by_role("button", name="消息适配", exact=True).click()
                        adapter = page.get_by_role("region", name="当前对话消息适配")
                        name = "剧情状态" if mode == "half_agent" else "交接记忆"
                        await adapter.get_by_label(name + "发送角色", exact=True).select_option("user")
                        await adapter.get_by_label(name + "标签包裹", exact=True).uncheck()
                        await adapter.get_by_role("button", name="保存消息适配", exact=True).click()
                        await expect(adapter.get_by_role("status")).to_have_text("已保存，下一次模型调用立即生效。")
                        lane = "foreground" if mode == "half_agent" else "full_agent"
                        kind = "state" if mode == "half_agent" else "checkpoint"
                        assert store.message_adapter("story", session["id"], lane)["config"]["mappings"]["chat_completions"][kind] == {"role": "user", "wrap": False}
                        assert store.session(session["id"])["revision"] == 0
                        await adapter.get_by_role("button", name="重新载入", exact=True).click()
                        await expect(adapter.get_by_label(name + "发送角色", exact=True)).to_have_value("user")
                        await expect(adapter.get_by_label(name + "标签包裹", exact=True)).not_to_be_checked()
                        await page.screenshot(path=str(output / (mode + ".png")))
                    await page.get_by_role("dialog").get_by_role("button", name="关闭", exact=True).click()
                    # Load the authoring conversation through the app's normal persistence selection.
                    await page.evaluate("id=>localStorage.setItem('tavern-active-card-conversation',id)", conversation["id"])
                    await page.reload()
                    await page.get_by_role("tab", name="制卡", exact=True).click()
                    await expect(page.get_by_role("textbox", name="制卡要求", exact=True)).to_be_visible()
                    await page.get_by_role("button", name="消息适配", exact=True).click()
                    adapter = page.get_by_role("region", name="当前对话消息适配")
                    await adapter.get_by_label("草稿概览发送角色", exact=True).select_option("user")
                    await adapter.get_by_label("草稿概览标签包裹", exact=True).check()
                    await adapter.get_by_role("button", name="保存消息适配", exact=True).click()
                    await expect(adapter.get_by_role("status")).to_have_text("已保存，下一次模型调用立即生效。")
                    response = await page.request.get(address + f"/api/card-editor/conversations/{conversation['id']}/message-adapter")
                    assert response.status == 200 and (await response.json())["lanes"][0]["id"] == "card_editor"
                    assert store.message_adapter("editor", conversation["id"], "card_editor")["config"]["mappings"]["chat_completions"]["editor_overview"] == {"role": "user", "wrap": True}
                    await page.screenshot(path=str(output / "editor.png"))
                    assert not errors, errors
                finally:
                    await browser.close()
        finally:
            server.should_exit = True
            thread.join(timeout=5)
            await app.state.editor.close()
            await app.state.service.close()
    print("Message adapter UI checks passed")


if __name__ == "__main__":
    asyncio.run(main())
