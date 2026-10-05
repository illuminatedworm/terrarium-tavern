"""Verify last-round edit controls and persisted edits in both agent modes."""

import asyncio
import socket
import tempfile
from pathlib import Path
from threading import Thread

import uvicorn
from playwright.async_api import async_playwright, expect

from tavern.application.api import create_app
from tavern.core.models import TurnRequest
from tests.browser.browser_history_navigation import seed


async def main():
    output = Path(".cache/message-editing")
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as root:
        app = create_app(root)
        store = app.state.store
        card = store.import_card(b'{"name":"Editing fixture","first_mes":"Opening"}', "card.json")
        cases = [(mode, *seed(store, card["id"], mode, 2)) for mode in ("half_agent", "full_agent")]
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        address = f"http://127.0.0.1:{sock.getsockname()[1]}"
        server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
        thread = Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
        thread.start()
        try:
            while not server.started:
                await asyncio.sleep(.05)
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch()
                try:
                    page = await browser.new_page()
                    errors = []
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    await page.goto(address)
                    for mode, sid, messages, _ in cases:
                        await page.evaluate("sid => localStorage.setItem('tavern-session', sid)", sid)
                        user_id, reply_id = messages[-2]["id"], messages[-1]["id"]
                        user = page.locator(f'[data-message-id="{user_id}"]')
                        reply = page.locator(f'[data-message-id="{reply_id}"]')
                        for width, height in ((1440, 900), (390, 844)):
                            await page.set_viewport_size({"width": width, "height": height})
                            await page.reload()
                            user_edit = user.get_by_role("button", name="编辑", exact=True)
                            reply_edit = reply.get_by_role("button", name="编辑", exact=True)
                            await expect(user_edit).to_be_visible()
                            await expect(reply_edit).to_be_visible()
                            await expect(user_edit).to_be_enabled()
                            await expect(reply_edit).to_be_enabled()
                            await expect(user_edit.locator("svg")).to_be_visible()
                            await expect(reply_edit.locator("svg")).to_be_visible()
                            user_box, reply_box = await user_edit.bounding_box(), await reply_edit.bounding_box()
                            assert user_box["width"] == reply_box["width"] >= 32
                            assert user_box["height"] == reply_box["height"] >= 32
                            for older in messages[:-2]:
                                await expect(page.locator(f'[data-message-id="{older["id"]}"]').get_by_role("button", name="编辑", exact=True)).to_have_count(0)
                            user_text = f"Edited user message {mode} {width}"
                            reply_text = f"Edited AI reply {mode} {width}"
                            await user_edit.click()
                            await expect(reply_edit).to_be_disabled()
                            await user.get_by_role("textbox").fill(user_text)
                            async with page.expect_response(lambda response: response.request.method == "PUT" and response.url.endswith(f"/messages/{user_id}")) as saved:
                                await user.get_by_role("button", name="保存修改", exact=True).click()
                                await expect(page.get_by_text("这次修改要提醒 AI 吗？", exact=True)).to_be_visible()
                                await page.get_by_role("button", name="不提醒 AI", exact=True).click()
                            assert (await saved.value).status == 200
                            await expect(user.get_by_role("textbox")).to_have_count(0)
                            await expect(reply_edit).to_be_enabled()
                            await reply_edit.click()
                            await expect(user_edit).to_be_disabled()
                            await reply.get_by_role("textbox").fill(reply_text)
                            await reply.get_by_role("button", name="保存修改", exact=True).click()
                            await page.get_by_role("button", name="提醒 AI", exact=True).click()
                            await expect(reply.get_by_role("textbox")).to_have_count(0)
                            await page.reload()
                            await expect(user).to_contain_text(user_text)
                            await expect(reply).to_contain_text(reply_text)
                            await expect(user_edit).to_be_enabled()
                            await expect(reply_edit).to_be_enabled()
                            saved_session = store.session(sid)
                            assert saved_session["head"] == reply_id
                            assert [message["id"] for message in saved_session["messages"]] == [message["id"] for message in messages]
                            assert saved_session["messages"][-2]["raw"] == user_text
                            assert saved_session["messages"][-1]["raw"] == reply_text
                            await page.screenshot(path=str(output / f"{mode}-{width}.png"), full_page=True)
                        for status in ("error", "cancelled", "interrupted"):
                            session = store.session(sid)
                            tid, _ = store.create_turn(sid, TurnRequest(text="Pending input", operation_id=status, expected_revision=session["revision"]))
                            store.set_turn(tid, status=status)
                            await page.reload()
                            pending = page.locator(".message.user").last
                            pending_edit = pending.get_by_role("button", name="编辑", exact=True)
                            await expect(pending_edit).to_be_visible()
                            await expect(pending_edit.locator("svg")).to_be_visible()
                            await pending_edit.click()
                            await pending.get_by_role("textbox").fill(f"Edited pending input {status}")
                            await pending.get_by_role("button", name="保存", exact=True).click()
                            await expect(pending.get_by_role("textbox")).to_have_count(0)
                            await page.reload()
                            await expect(page.locator(".message.user").last).to_contain_text(f"Edited pending input {status}")
                            assert store.turn(tid)["request"]["text"] == f"Edited pending input {status}"
                        print(f"{mode}: desktop/mobile last-round edits and failed/cancelled/interrupted input edits passed.", flush=True)
                    assert not errors, errors
                finally:
                    await browser.close()
        finally:
            server.should_exit = True
            thread.join(timeout=10)


if __name__ == "__main__":
    asyncio.run(main())
