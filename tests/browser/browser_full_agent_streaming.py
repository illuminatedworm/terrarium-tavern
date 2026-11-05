"""Production Full Agent transport + real browser: previews, replay, isolation and retry."""

import argparse
import asyncio
import io
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
from tavern.desktop_browser import system_browser
from tavern.novel.source import import_file
from tavern.scripting.model_transport import ModelTransport
from tavern.storage.store import uid
from tests.backend.test_full_agent import visual_spec
from tests.backend.test_full_agent_streaming import (
    body_delta,
    body_end,
    body_start,
    story_events,
    terminal,
    tool_events,
)


class BrowserStream(httpx.AsyncByteStream):
    def __init__(self, *pieces):
        self.pieces, self.closed = pieces, False
        self.started = Event()

    async def __aiter__(self):
        self.started.set()
        for piece in self.pieces:
            if isinstance(piece, Event):
                await asyncio.to_thread(piece.wait)
            elif isinstance(piece, Exception):
                raise piece
            else:
                yield ("data: " + json.dumps(piece, ensure_ascii=False) + "\n\n").encode()

    async def aclose(self):
        self.closed = True


async def main(single_viewport=False, report_activity_only=False):
    output = Path(".cache/full-agent-streaming")
    output.mkdir(parents=True, exist_ok=True)
    gates = [Event() for _ in range(5)]
    with tempfile.TemporaryDirectory(prefix="tavern-full-stream-") as root:
        app = create_app(root)
        store = app.state.store
        card = store.import_card(
            json.dumps({"name": "流式预览角色", "first_mes": "开场：旧港的钟声响了。"}).encode()
        )
        cfg = store.settings_document()
        cfg.full_agent.model.model = "fixture"
        cfg.full_agent.tools.auto_retrieval = False
        cfg.shared.providers[0].api_key = "fixture"
        cfg.shared.model_request_mode = "stream"
        store.save_settings(cfg)
        session = store.new_session(SessionCreate(card_id=card["id"], agent_mode="full_agent"))
        with store.connection() as db:
            db.execute("UPDATE sessions SET disabled_tools='[]' WHERE id=?", (session["id"],))
        other = store.new_session(SessionCreate(card_id=card["id"], agent_mode="full_agent"))
        research = app.state.service.full.novel
        source = uid()
        metadata = import_file(store.root, source, io.BytesIO("钟声来自旧港。".encode()), "原著.txt")
        research.data.bind(session["id"], 0, source, metadata)
        research.data.configure(session["id"], 1, enabled=True)
        job_id, _ = research.data.create(
            session["id"],
            "fixture-report",
            1,
            "",
            "钟声来自哪里？",
            "",
            research.snapshot_config(session["id"]),
        )
        job = research.data.job(job_id)
        job["status"] = "completed"
        research.data.save(job, mail={"kind": "report", "content": "钟声来自旧港。", "operation": "report"})
        store.update_session(session["id"], 0, title="流式正文测试")
        store.update_session(other["id"], 0, title="另一个故事")
        second_segment = tool_events("submit_story", {"content": "第二轮续写的第二段。"})
        for piece in second_segment:
            for call in piece["choices"][0]["delta"].get("tool_calls", []):
                call["index"] = 2
                if "id" in call:
                    call["id"] = "story-2"
        streams = [
            BrowserStream(gates[0], *tool_events()),
            BrowserStream(
                gates[1], body_start("第一段正在生成。"), gates[2], body_delta("这是第二段。"), *body_end()
            ),
            BrowserStream(
                *story_events("第二轮已接收的第一段。\n\n---")[:-1],
                *tool_events("present_ui", {"spec": visual_spec()})[:-1],
                *second_segment,
            ),
            BrowserStream(gates[3], body_start("第三段的残片"), RuntimeError("第三段连接中断")),
            BrowserStream(gates[4], terminal()),
            BrowserStream(*story_events("第三轮的新回复")),
        ]
        bodies = []

        async def route(request):
            bodies.append(json.loads(request.content))
            assert bodies[-1]["stream"] is True
            # MockTransport bypasses HTTPcore's real body-sent notification.
            if trace := request.extensions.get("trace"):
                await trace("http11.send_request_body.complete", {})
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
                browser = await playwright.chromium.launch(**system_browser().launch_options)
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
                    runtime = page.locator(".agent-runtime-activity")
                    await expect(runtime).to_contain_text("R1 · 原著调查报告已送达")
                    await expect(runtime).to_contain_text("主模型请求已发出")
                    assert await runtime.locator("summary, code").count() == 0
                    await expect(page.locator(".draft")).to_have_count(0)
                    await page.screenshot(path=str(output / "report-dispatched.png"))
                    assert store.session(session["id"])["head"] == session["head"]
                    gates[0].set()
                    await expect(page.locator(".activity-name")).to_contain_text(["write_handoff"])
                    await expect(page.locator(".draft")).to_have_count(0)
                    gates[1].set()
                    await expect(page.locator(".draft")).to_contain_text("第一段正在生成。")
                    await expect(page.locator(".message .badge").last).to_have_text("本轮预览 · 未提交")
                    await page.reload()
                    await expect(runtime).to_contain_text("主模型请求已发出")
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
                        await expect(runtime).to_contain_text("主模型请求已发出")
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
                    await expect(runtime).to_have_count(0)
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
                    await expect(
                        page.locator(".agent-message-activity .agent-runtime-activity")
                    ).to_contain_text("主模型请求已发出")
                    if report_activity_only:
                        assert all(stream.closed for stream in streams[:2])
                        assert not errors, errors
                        print(
                            "Novel report dispatch, pending reload, selected-session isolation and durable activity passed."
                        )
                        return
                    await page.get_by_role("textbox", name="用户输入", exact=True).fill("第二轮")
                    await send.click()
                    await expect(page.locator(".draft")).to_contain_text("第二轮续写的第二段。")
                    await expect(page.locator(".draft .story-segment-divider")).to_have_count(1)
                    await expect(page.locator(".draft .prose hr")).to_have_count(1)
                    graphic = page.locator(".draft .story-visual")
                    await expect(graphic).to_contain_text("完整图形尾标")
                    await expect(graphic).to_contain_text("预览 · 尚未提交")
                    assert len(store.session(session["id"])["messages"]) == 3
                    assert await page.locator(".draft > .prose, .draft > .story-visuals").evaluate_all(
                        "nodes => nodes.map(n => n.classList.contains('story-visuals') ? 'visual' : 'story')"
                    ) == ["story", "visual", "story"]
                    gates[3].set()
                    retry = page.locator(".story").get_by_role("button", name="重新生成", exact=True)
                    await expect(retry).to_be_enabled()
                    await expect(page.locator(".message .badge").last).to_have_text("未提交草稿")
                    assert len(store.session(session["id"])["messages"]) == 3
                    await page.reload()
                    await expect(page.locator(".draft")).to_contain_text("第二轮已接收的第一段。")
                    await expect(page.locator(".draft")).to_contain_text("第二轮续写的第二段。")
                    await expect(page.locator(".draft .story-segment-divider")).to_have_count(1)
                    await expect(graphic).to_contain_text("完整图形尾标")
                    await page.screenshot(path=str(output / "story-segments.png"))
                    failed = store.session(session["id"])["turns"][0]["id"]
                    assert "第三段" in store.turn(failed)["error"]
                    composer = page.get_by_role("textbox", name="用户输入", exact=True)
                    await composer.fill("下一轮的新发言")
                    await send.click()
                    await expect(page.locator(".composer-notice")).to_contain_text("请先选择如何完成上一轮")
                    async with page.expect_request(
                        lambda request: request.method == "POST" and request.url.endswith("/turns")
                    ) as submitted:
                        await page.get_by_role("button", name="继续本轮", exact=True).click()
                    request = (await submitted.value).post_data_json
                    assert request["resume_turn_id"] == failed and request["text"] == "第二轮"
                    await expect(page.locator(".composer-notice")).to_contain_text("正在继续上一轮")
                    await expect(composer).to_have_value("下一轮的新发言")
                    assert len(store.session(session["id"])["messages"]) == 3
                    assert await asyncio.to_thread(streams[4].started.wait, 5), "Continuation did not start"
                    assert "下一轮的新发言" not in json.dumps(bodies[4], ensure_ascii=False)
                    await expect(graphic).to_have_count(1)
                    await expect(graphic).to_contain_text("完整图形尾标")
                    gates[4].set()
                    await expect(page.locator(".message.assistant").last).to_contain_text("第三轮的新回复")
                    await expect(send).to_be_visible()
                    await expect(page.locator(".draft")).to_have_count(0)
                    await expect(composer).to_have_value("")
                    assert "第三段的残片" not in json.dumps(bodies[4], ensure_ascii=False)
                    history = store.session(session["id"])["messages"]
                    assert [message["raw"] for message in history[-4:]] == [
                        "第二轮",
                        "第二轮已接收的第一段。\n\n---\n\n第二轮续写的第二段。",
                        "下一轮的新发言",
                        "第三轮的新回复",
                    ]
                    assert len(bodies) == 6
                    await expect(page.locator(".message.assistant .story-segment-divider")).to_have_count(1)
                    await expect(page.locator(".message.assistant .story-visual")).to_have_count(1)
                    assert "下一轮的新发言" in json.dumps(bodies[5], ensure_ascii=False)
                    assert all(stream.closed for stream in streams)
                    assert not errors, errors
                    print(
                        f"Full Agent previews, reload, session isolation, continuation before next input and {len(viewports)} viewport(s) passed."
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
    parser.add_argument(
        "--report-activity-only", action="store_true", help="Stop after report dispatch and history checks."
    )
    args = parser.parse_args()
    asyncio.run(main(args.single_viewport, args.report_activity_only))
