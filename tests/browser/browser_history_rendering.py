"""Long-history rendering guard with disposable data and no remote providers."""

import asyncio
import json
import socket
import sys
import tempfile
from threading import Event, Thread

import uvicorn
from kohakuterrarium.testing.llm import ScriptedLLM
from playwright.async_api import async_playwright, expect

from tavern.application.api import create_app
from tavern.core.models import SessionCreate
from tavern.memories.memory_records import Record, source_hash
from tavern.storage.store import dump
from tests.backend.test_full_agent import ScriptHost
from tests.backend.test_message_versions import generate


async def main(all_viewports=False):
    with tempfile.TemporaryDirectory() as root:
        app = create_app(root)
        store = app.state.store
        cfg = store.settings()
        cfg.retrieval.channels = ["keyword"]
        cfg.foreground.model = cfg.background.model = "fixture"
        cfg.local_models.reranker_enabled = False
        store.save_settings(cfg)
        card = store.import_card(
            b'{"name":"History fixture","first_mes":"Old source sentence."}', "card.json"
        )
        with store.connection() as db:
            db.execute("UPDATE cards SET permission=1 WHERE id=?", (card["id"],))
        session = store.new_session(SessionCreate(card_id=card["id"]))
        snapshot = session["snapshot"]
        snapshot["memory_document"] = {
            "version": 3,
            "records": [
                Record(
                    category="current",
                    content="A remembered source",
                    sources=[
                        {
                            "message_id": session["head"],
                            "fingerprint": source_hash(session["messages"][0]),
                            "excerpt": "Old source sentence.",
                        }
                    ],
                ).model_dump()
            ],
        }
        parent = session["head"]
        with store.connection() as db:
            for i in range(1, 300):
                mid = f"fixture-{i}"
                text = f"Message {i}.\n\n" + "A long paragraph for scrolling. " * 30
                if i in (200, 260):
                    text = '<div>Isolated panel</div><script>document.body.dataset.ready="yes";</script>'
                db.execute(
                    "INSERT INTO messages(id,session,parent,role,raw,model,display,snapshot,created) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        mid,
                        session["id"],
                        parent,
                        "user" if i == 298 else "assistant",
                        text,
                        text,
                        text,
                        dump(snapshot),
                        i,
                    ),
                )
                parent = mid
            db.execute("UPDATE sessions SET head=? WHERE id=?", (parent, session["id"]))
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        address = f"http://127.0.0.1:{sock.getsockname()[1]}"
        short_card = store.import_card(b'{"name":"Retry fixture","first_mes":"Opening"}', "retry.json")
        short = generate(
            store, store.new_session(SessionCreate(card_id=short_card["id"])), text="Original reply"
        )
        release = Event()

        class GatedModel(ScriptedLLM):
            fail = True

            async def chat(self, messages, **kwargs):
                if not await asyncio.to_thread(release.wait, 15):
                    raise RuntimeError("Browser did not release the request")
                if self.fail:
                    raise RuntimeError("fixture generation failed")
                async for chunk in super().chat(messages, **kwargs):
                    yield chunk

        model = GatedModel(["Replayed reply"])
        app.state.service.half.runtime = ScriptHost()
        app.state.service.provider_factory = lambda: model
        ready = Event()

        class ReadyServer(uvicorn.Server):
            async def startup(self, sockets=None):
                await super().startup(sockets)
                ready.set()

        server = ReadyServer(uvicorn.Config(app, log_level="error"))
        thread = Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
        thread.start()
        try:
            if not await asyncio.to_thread(ready.wait, 10):
                raise RuntimeError("Browser fixture server did not start")
            async with async_playwright() as p:
                browser = await p.chromium.launch()
                try:
                    viewports = [
                        (1440, 900, 1),
                        (1280, 720, 1),
                        (390, 844, 1),
                        (720, 450, 2),
                    ]
                    for width, height, scale in viewports if all_viewports else viewports[:1]:
                        context = await browser.new_context(
                            viewport={"width": width, "height": height}, device_scale_factor=scale
                        )
                        await context.add_init_script(
                            f'if (window === window.top && !localStorage.getItem("tavern-session")) localStorage.setItem("tavern-session", {json.dumps(session["id"])});'
                        )
                        page = await context.new_page()
                        errors = []
                        page.on("pageerror", lambda error: errors.append(str(error)))
                        await page.goto(address)
                        rows = page.locator(".messages [data-message-id]")
                        await expect(page.locator(".story-history")).to_have_attribute("data-virtual", "true")
                        last = page.locator('[data-message-id="fixture-299"]')
                        await expect(last).to_be_visible()
                        assert await rows.count() < 60
                        rail = page.get_by_role("slider", name="对话历史导航")
                        await rail.focus()
                        await rail.press("Home")
                        await expect(page.locator(f'[data-message-id="{session["head"]}"]')).to_be_visible()
                        box = await rail.bounding_box()
                        # Use the visible tick range; the slider's padded edges are not history positions.
                        ticks = rail.locator(".rail-tick:not(.current):not(.cached):not(.hovered)")
                        first_tick, last_tick = (
                            await ticks.first.bounding_box(),
                            await ticks.last.bounding_box(),
                        )
                        top = first_tick["y"] + first_tick["height"] / 2
                        bottom = last_tick["y"] + last_tick["height"] / 2
                        await page.mouse.click(box["x"] + box["width"] / 2, top + (bottom - top) * 200 / 299)
                        await expect(page.locator('[data-message-id="fixture-200"] iframe')).to_be_visible()
                        await rail.focus()
                        await rail.press("End")
                        await expect(last).to_be_visible()
                        await expect(page.locator(".messages iframe")).to_have_count(1)
                        await expect(last.get_by_role("button", name="编辑", exact=True)).to_have_count(0)
                        latest_input = page.locator('[data-message-id="fixture-298"]')
                        await latest_input.get_by_role("button", name="编辑", exact=True).press("Enter")
                        await latest_input.locator("textarea").fill("Changed paragraph\n" * 20)
                        await rail.focus()
                        await rail.press("Home")
                        await expect(latest_input.locator("textarea")).to_have_count(1)
                        await rail.press("End")
                        await latest_input.get_by_role("button", name="取消", exact=True).click()
                        if width == 1440:
                            await page.get_by_role("tab", name="场景", exact=True).click()
                            await page.locator(".memory-group > summary").click()
                            await page.locator(".memory-record details > summary").click()
                            await page.get_by_role("button", name="定位原文", exact=True).click()
                            await expect(page.locator("mark.source-sentence-highlight")).to_have_text(
                                "Old source sentence."
                            )
                            assert await rows.count() < 60
                            await page.get_by_role("button", name="跳到最新", exact=True).click()
                            await expect(last).to_be_visible()
                        assert await page.evaluate(
                            "document.documentElement.scrollWidth <= innerWidth && document.documentElement.scrollHeight <= innerHeight"
                        )
                        if width == 1440:
                            await page.evaluate(
                                '(sid) => localStorage.setItem("tavern-session", sid)', short["id"]
                            )
                            await page.reload()
                            old_input = page.locator(f'[data-message-id="{short["messages"][-2]["id"]}"]')
                            old_reply = page.locator(f'[data-message-id="{short["head"]}"]')
                            await expect(old_reply).to_be_visible()
                            requests = []
                            page.on(
                                "request",
                                lambda request: (
                                    requests.append(request.post_data_json)
                                    if request.method == "POST" and request.url.endswith("/turns")
                                    else None
                                ),
                            )
                            await old_input.get_by_role("button", name="编辑", exact=True).click()
                            await old_input.locator("textarea").fill("First replacement")
                            await old_input.locator("textarea").press("Control+Enter")
                            pending = page.locator('[data-message-id="__pending__"]')
                            await expect(pending).to_contain_text("待提交输入")
                            await expect(pending).to_contain_text("First replacement")
                            assert store.session(short["id"])["head"] == short["head"]
                            release.set()
                            await expect(
                                pending.get_by_role("button", name="编辑", exact=True)
                            ).to_be_enabled()
                            await page.reload()
                            pending = page.locator('[data-message-id="__pending__"]')
                            await expect(pending).to_contain_text("First replacement")
                            await pending.get_by_role("button", name="编辑", exact=True).click()
                            await pending.locator("textarea").fill("Second replacement")
                            model.fail = False
                            await pending.get_by_role("button", name="修改并重试", exact=True).click()
                            await expect(page.locator(".message.assistant .prose").last).to_contain_text(
                                "Replayed reply"
                            )
                            await expect(pending).to_have_count(0)
                            current = store.session(short["id"])
                            assert current["messages"][-2]["raw"] == "Second replacement"
                            assert len(current["messages"]) == len(short["messages"])
                            assert [request["replacement_text"] for request in requests] == [
                                "First replacement",
                                "Second replacement",
                            ]
                            assert all(
                                not request.get("resume_turn_id") and not request["regenerate_include_reply"]
                                for request in requests
                            )
                            await page.get_by_role("button", name="上一个版本", exact=True).click()
                            await expect(old_reply).to_be_visible()
                            await expect(old_input).to_contain_text(short["messages"][-2]["raw"])
                            await page.get_by_role("button", name="下一个版本", exact=True).click()
                            restored = page.locator(f'[data-message-id="{current["messages"][-2]["id"]}"]')
                            await expect(restored).to_contain_text("Second replacement")
                        assert not errors, errors
                        await context.close()
                finally:
                    await browser.close()
            assert len(store.session(session["id"])["messages"]) == 300
            print(
                "History rendering and input retry passed: pending input, reload, failed-input edit and whole-exchange versions."
            )
        finally:
            release.set()
            server.should_exit = True
            await asyncio.to_thread(thread.join, 10)


if __name__ == "__main__":
    asyncio.run(main("--all-viewports" in sys.argv))
