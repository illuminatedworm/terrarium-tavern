"""Production Full Agent transport + real browser: previews, replay, isolation and retry."""

import argparse
import asyncio
import json
import socket
import tempfile
from pathlib import Path
from threading import Event, Thread

import httpx
import uvicorn
from playwright.async_api import async_playwright, expect

from tavern.application.api import create_app
from tavern.core.models import SessionCreate
from tavern.scripting.model_transport import ModelTransport
from tests.backend.test_full_agent_streaming import (
    body_delta,
    body_end,
    body_start,
    delta,
    story_events,
    tool_events,
)


class BrowserStream(httpx.AsyncByteStream):
    def __init__(self, *pieces):
        self.pieces, self.closed = pieces, False

    async def __aiter__(self):
        for piece in self.pieces:
            if isinstance(piece, Event):
                await asyncio.to_thread(piece.wait)
            elif isinstance(piece, Exception):
                raise piece
            else:
                yield ("data: " + json.dumps(piece, ensure_ascii=False) + "\n\n").encode()

    async def aclose(self):
        self.closed = True


async def main(single_viewport=False):
    output = Path(".cache/full-agent-streaming")
    output.mkdir(parents=True, exist_ok=True)
    gates = [Event() for _ in range(4)]
    with tempfile.TemporaryDirectory(prefix="tavern-full-stream-") as root:
        app = create_app(root)
        store = app.state.store
        card = store.import_card(
            json.dumps({"name": "流式预览角色", "first_mes": "开场：旧港的钟声响了。"}).encode(), "card.json"
        )
        cfg = store.settings_document()
        cfg.full_agent.model.model = "fixture"
        cfg.full_agent.tools.auto_retrieval = False
        cfg.shared.providers[0].api_key = "fixture"
        cfg.shared.model_request_mode = "stream"
        store.save_settings(cfg)
        session = store.new_session(SessionCreate(card_id=card["id"], agent_mode="full_agent"))
        other = store.new_session(SessionCreate(card_id=card["id"], agent_mode="full_agent"))
        store.update_session(session["id"], 0, title="流式正文测试")
        store.update_session(other["id"], 0, title="另一个故事")
        streams = [
            BrowserStream(delta("先整理过程资料"), gates[0], *tool_events()),
            BrowserStream(
                gates[1], body_start("第一段正在生成。"), gates[2], body_delta("这是第二段。"), *body_end()
            ),
            BrowserStream(
                body_start("失败后保留的未提交片段"), gates[3], httpx.ReadError("fixture disconnected")
            ),
            BrowserStream(*story_events("重试后的完整正文")),
        ]
        bodies = []

        def route(request):
            bodies.append(json.loads(request.content))
            assert bodies[-1]["stream"] is True
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"}, stream=streams[len(bodies) - 1]
            )

        def provider():
            settings = store.settings("full_agent")
            return ModelTransport(
                settings, "fixture", http_client=httpx.AsyncClient(transport=httpx.MockTransport(route))
            )

        app.state.service.full.provider_factory = provider
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        address = f"http://127.0.0.1:{sock.getsockname()[1]}"
        ready = Event()

        class ReadyServer(uvicorn.Server):
            async def startup(self, sockets=None):
                await super().startup(sockets)
                ready.set()

        server = ReadyServer(uvicorn.Config(app, log_level="error"))
        thread = Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
        thread.start()
        try:
            assert await asyncio.to_thread(ready.wait, 5), "Acceptance server did not start"
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch()
                page = await browser.new_page(viewport={"width": 1920, "height": 1080})
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                try:
                    await page.goto(address)
                    await page.evaluate("id => localStorage.setItem('tavern-session', id)", session["id"])
                    await page.reload()
                    send = page.get_by_role("button", name="发送", exact=True)
                    await expect(send).to_be_visible()
                    await page.get_by_role("textbox", name="用户输入", exact=True).fill("继续故事")
                    await send.click()
                    await expect(page.locator(".draft")).to_have_count(0)
                    assert store.session(session["id"])["head"] == session["head"]
                    gates[0].set()
                    await expect(page.locator(".activity-name")).to_contain_text(["write_handoff"])
                    await expect(page.locator(".draft")).to_have_count(0)
                    gates[1].set()
                    await expect(page.locator(".draft")).to_contain_text("第一段正在生成。")
                    await expect(page.locator(".message .badge").last).to_have_text("正文预览 · 未保存")
                    await page.reload()
                    await expect(page.locator(".draft .prose")).to_have_text("第一段正在生成。")
                    assert store.session(session["id"])["head"] == session["head"]
                    await page.get_by_role("button", name="折叠左侧栏", exact=True).click()
                    await page.get_by_role("button", name="折叠右侧栏", exact=True).click()
                    viewports = (
                        [(1920, 1080, "desktop")]
                        if single_viewport
                        else [
                            (2560, 1440, "large"),
                            (1920, 1080, "desktop"),
                            (360, 792, "mobile-dpr3-equivalent"),
                            (960, 540, "zoom-equivalent"),
                        ]
                    )
                    for width, height, name in viewports:
                        await page.set_viewport_size({"width": width, "height": height})
                        await expect(page.locator(".draft")).to_be_visible()
                        assert await page.evaluate(
                            "document.documentElement.scrollWidth <= innerWidth && "
                            "document.documentElement.scrollHeight <= innerHeight"
                        )
                        await page.screenshot(path=str(output / f"{name}.png"))
                    await page.set_viewport_size({"width": 1920, "height": 1080})
                    await page.get_by_role("button", name="展开左侧栏", exact=True).click()
                    await page.locator(".session-open").filter(has_text="另一个故事").click()
                    await expect(page.locator(".session-row.active")).to_contain_text("另一个故事")
                    await expect(page.locator(".draft")).to_have_count(0)
                    completed = Event()
                    pending = next(task for task in app.state.service.full.tasks.values() if not task.done())
                    pending.get_loop().call_soon_threadsafe(
                        pending.add_done_callback, lambda task: completed.set()
                    )
                    gates[2].set()
                    assert await asyncio.to_thread(completed.wait, 5), "Story finalization did not finish"
                    await expect(page.locator(".session-row.active")).to_contain_text("另一个故事")
                    await expect(page.locator(".message.assistant")).to_have_count(1)
                    await page.locator(".session-open").filter(has_text="流式正文测试").click()
                    await expect(page.locator(".message.assistant").last).to_contain_text(
                        "第一段正在生成。这是第二段。"
                    )
                    await expect(page.locator(".draft")).to_have_count(0)
                    await expect(page.locator(".message.assistant").last).not_to_contain_text(
                        "先整理过程资料"
                    )
                    await page.get_by_role("textbox", name="用户输入", exact=True).fill("第二轮")
                    await send.click()
                    await expect(page.locator(".draft")).to_contain_text("失败后保留的未提交片段")
                    gates[3].set()
                    retry = page.locator(".story").get_by_role("button", name="重新生成", exact=True)
                    await expect(retry).to_be_enabled()
                    await expect(page.locator(".message .badge").last).to_have_text("未提交草稿")
                    assert len(store.session(session["id"])["messages"]) == 3
                    await page.reload()
                    await expect(page.locator(".draft")).to_have_text("失败后保留的未提交片段")
                    await retry.click()
                    await retry.click()
                    await expect(page.locator(".message.assistant").last).to_contain_text("重试后的完整正文")
                    await expect(send).to_be_visible()
                    await expect(page.locator(".draft")).to_have_count(0)
                    assert "失败后保留的未提交片段" not in json.dumps(bodies[-1], ensure_ascii=False)
                    assert len(store.session(session["id"])["messages"]) == 5
                    assert all(stream.closed for stream in streams)
                    assert not errors, errors
                    print(
                        f"Full Agent explicit story previews, reload, session isolation, failure/retry and {len(viewports)} viewport(s) passed."
                    )
                except Exception:
                    await page.screenshot(path=str(output / "failure.png"), full_page=True)
                    (output / "failure-aria.txt").write_text(
                        await page.locator("body").aria_snapshot(), encoding="utf-8"
                    )
                    raise
                finally:
                    await browser.close()
        finally:
            for gate in gates:
                gate.set()
            server.should_exit = True
            await asyncio.to_thread(thread.join, 10)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--single-viewport", action="store_true", help="Check only desktop when no layout changed."
    )
    asyncio.run(main(parser.parse_args().single_viewport))
