"""Exercise the Writer Responses cache marker in the story UI."""

import asyncio
import json
import socket
import tempfile
from threading import Thread

import uvicorn
from playwright.async_api import async_playwright, expect

from tavern.application.api import create_app
from tavern.core.models import SessionCreate
from tavern.storage.store import dump, uid


async def main():
    with tempfile.TemporaryDirectory() as root:
        app = create_app(root)
        store = app.state.store
        settings = store.settings()
        settings.foreground.api_mode = "responses"
        settings.foreground.model = "fixture"
        store.save_settings(settings)
        card = store.import_card(json.dumps({"name": "缓存位置测试", "first_mes": "开场"}).encode(), "card.json")
        session = store.new_session(SessionCreate(card_id=card["id"]))
        parent, replies = session["head"], []
        with store.connection() as db:
            for turn in range(3):
                for role in ("user", "assistant"):
                    mid = uid()
                    text = f"{role} {turn}"
                    db.execute("INSERT INTO messages(id,session,parent,role,raw,model,display,snapshot,created) "
                               "VALUES (?,?,?,?,?,?,?,?,?)",
                               (mid, session["id"], parent, role, text, text, text,
                                dump(session["snapshot"]), turn + 1))
                    parent = mid
                    if role == "assistant":
                        replies.append(mid)
            db.execute("UPDATE sessions SET head=? WHERE id=?", (parent, session["id"]))

        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        address = f"http://127.0.0.1:{sock.getsockname()[1]}"
        server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
        thread = Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
        thread.start()
        try:
            while not server.started:
                await asyncio.sleep(0.05)
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch()
                try:
                    page = await browser.new_page(viewport={"width": 1200, "height": 850})
                    await page.goto(address)
                    await page.evaluate("id=>localStorage.setItem('tavern-session',id)", session["id"])
                    await page.reload()
                    target = page.locator(f'[data-message-id="{replies[-2]}"]')
                    latest = page.locator(f'[data-message-id="{replies[-1]}"]')
                    await expect(target.locator(".cache-breakpoint-dot.planned")).to_be_visible()
                    action = latest.locator(".cache-set-action")
                    assert await action.get_attribute("title")
                    async with page.expect_response(lambda response: response.url.endswith(
                            f"/api/sessions/{session['id']}/cache-breakpoint") and response.request.method == "PUT") as saved:
                        await action.click()
                    assert (await saved.value).status == 200
                    await expect(latest.locator(".cache-breakpoint-dot.planned.manual")).to_be_visible()
                    assert store.session(session["id"])["cache_breakpoint_auto"] is False
                    if not await page.get_by_role("button", name="缓存", exact=True).is_visible():
                        await page.get_by_role("button", name="展开右侧栏", exact=True).click()
                    await page.get_by_role("button", name="缓存", exact=True).click()
                    panel = page.get_by_role("region", name="缓存断点控制")
                    await expect(panel.get_by_label("每新增几轮跟进")).to_have_value("2")
                    await expect(panel.get_by_label("倒数第几个 AI 回复")).to_have_value("2")
                    await panel.get_by_label("缓存跟进方式").select_option("true")
                    async with page.expect_response(lambda response: response.url.endswith(
                            f"/api/sessions/{session['id']}/cache-breakpoint") and response.request.method == "PUT") as saved:
                        await panel.get_by_role("button", name="保存跟进策略", exact=True).click()
                    assert (await saved.value).status == 200
                    await expect(target.locator(".cache-breakpoint-dot.planned")).to_be_visible()
                    assert store.session(session["id"])["cache_breakpoint_auto"] is True
                    await page.set_viewport_size({"width": 390, "height": 844})
                    box = await target.locator(".cache-breakpoint-dot").bounding_box()
                    assert box and box["x"] >= 0 and box["width"] >= 18

                    short = store.new_session(SessionCreate(card_id=card["id"]))
                    await page.evaluate("id=>localStorage.setItem('tavern-session',id)", short["id"])
                    await page.reload()
                    await expect(page.locator(".cache-breakpoint-fallback .cache-breakpoint-dot")).to_be_visible()
                finally:
                    await browser.close()
        finally:
            server.should_exit = True
            thread.join(timeout=5)


if __name__ == "__main__":
    asyncio.run(main())
