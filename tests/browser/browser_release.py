"""Release UI acceptance against a fresh, disposable store and local model stub."""
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import httpx
from playwright.async_api import async_playwright

BASE = "http://127.0.0.1:8015"
OUTPUT = Path(".cache/release-validation")


class Provider(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers["content-length"])))
        content = ("旅人停在灯塔前，海风吹过长廊。\n\n" * 24
                   + "<tavern_retrieval>旅人在灯塔前等待守卫。</tavern_retrieval>")
        if payload.get("stream"):
            chunk = {"id": "fixture", "object": "chat.completion.chunk", "created": 0, "model": "fixture",
                     "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": "stop"}]}
            body = (f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n").encode()
            mime = "text/event-stream"
        else:
            body = json.dumps({"id": "fixture", "choices": [{"message": {"role": "assistant", "content":
                json.dumps({"text": "旅人到达灯塔。", "memories": []})}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 111, "completion_tokens": 20, "total_tokens": 131}}).encode()
            mime = "application/json"
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


async def assert_viewport(page, label):
    dimensions = await page.evaluate("""() => ({
        width:innerWidth,height:innerHeight,
        scrollWidth:document.documentElement.scrollWidth,
        scrollHeight:document.documentElement.scrollHeight,
        bodyWidth:document.body.scrollWidth,bodyHeight:document.body.scrollHeight
    })""")
    assert dimensions["scrollWidth"] <= dimensions["width"], (label, dimensions)
    assert dimensions["scrollHeight"] <= dimensions["height"], (label, dimensions)
    assert dimensions["bodyWidth"] <= dimensions["width"], (label, dimensions)
    assert dimensions["bodyHeight"] <= dimensions["height"], (label, dimensions)


async def main():
    OUTPUT.mkdir(parents=True, exist_ok=True)
    errors, checked = [], []
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        for width, height, zoom in [(1440, 900, 1), (1280, 720, 1), (390, 844, 1), (1440, 900, 2)]:
            # Browser zoom reduces the CSS viewport; a scale factor preserves the
            # physical size of the screenshot while exercising identical reflow.
            context = await browser.new_context(viewport={"width": width // zoom, "height": height // zoom},
                                                device_scale_factor=zoom)
            page = await context.new_page()
            page.on("pageerror", lambda e: errors.append(str(e)))
            await page.goto(BASE)
            label = f"{width}x{height}-zoom{zoom}"
            await assert_viewport(page, label + ":home")
            if width // zoom < 760:
                await page.get_by_role("button", name="切换左侧栏" if width // zoom < 620 else "展开左侧栏", exact=True).click()
            await page.get_by_role("button", name="⚙ 设置", exact=True).click()
            dialog = page.get_by_role("dialog", name="设置")
            initial = (await dialog.bounding_box())["height"]
            configuration = (await (await page.request.get(BASE + '/api/settings')).json())
            async def navigate(item):
                group = {'full_agent':'full agent','half_agent':'half agent','shared':'通用'}[item['scope']]
                if width // zoom <= 760:
                    await dialog.locator('.settings-mode-buttons').get_by_role('button',name=group,exact=True).click()
                    await dialog.get_by_role('combobox',name='设置页面',exact=True).select_option(item['id'])
                else:
                    await dialog.locator('.settings-pages').get_by_role('button',name=group+' '+item['label'],exact=True).click()
            for item in configuration['pages']:
                await navigate(item)
                await assert_viewport(page, label + item['id'])
                assert abs((await dialog.bounding_box())["height"] - initial) < 1
                checked.append(label + ":" + item['id'])
            await page.locator(".storage-report").wait_for()
            await page.screenshot(path=str(OUTPUT / f"settings-{label}.png"))
            await navigate(next(item for item in configuration['pages'] if item['id']=='half_agent.prompts'))
            await dialog.get_by_role("button", name="放大编辑：系统提示词", exact=True).click()
            editor = page.get_by_role("dialog", name="编辑：系统提示词", exact=True)
            editor_height = (await editor.bounding_box())["height"]
            await editor.get_by_role("textbox").fill("很长的提示词。\n" * 100)
            assert abs((await editor.bounding_box())["height"] - editor_height) < 1
            await assert_viewport(page, label + ":expanded-editor")
            await editor.get_by_role("button", name="关闭放大编辑", exact=True).click()
            await page.get_by_role('alertdialog').get_by_role('button',name='放弃修改').click()
            await dialog.get_by_role("button", name="关闭", exact=True).click()
            await page.get_by_role("tab", name="角色卡", exact=True).click()
            await page.locator('.card-side-item').first.click()
            for tab in await page.locator('.editor-tabs button').all():
                await tab.click()
                await assert_viewport(page, label + ":card-editor")
            if width // zoom < 760:
                await page.get_by_role("button", name="切换左侧栏" if width // zoom < 620 else "展开左侧栏", exact=True).click()
            await page.get_by_role("tab", name="会话", exact=True).click()
            await page.locator('.session-open').first.click()
            await page.locator('.messages').wait_for()
            async with page.expect_response(lambda response: response.url.endswith("/preview")
                and response.request.method == "POST" and response.request.post_data_json.get("text") == "前往灯塔") as counted:
                await page.get_by_placeholder("说些什么，或描述你的行动…").fill("前往灯塔")
            stats = await (await counted.value).json()
            assert stats["token_counting"]["unit"] == "tokens"
            await page.get_by_text(f"本次预计输入 {stats['estimated_tokens']:,} tokens", exact=True).wait_for()
            await page.get_by_role("button", name="发送", exact=True).click()
            await page.locator('.message.assistant').nth(1).wait_for(timeout=30000)
            await page.get_by_role("button", name="发送", exact=True).wait_for()
            await assert_viewport(page, label + ":dialogue")
            await page.locator('.messages').evaluate("e => {e.scrollTop=0; e.dispatchEvent(new Event('scroll'));}")
            jump = page.get_by_role("button", name="跳到最新", exact=True)
            await jump.wait_for()
            assert await jump.evaluate("e=>getComputedStyle(e).position") == "absolute"
            await page.screenshot(path=str(OUTPUT / f"story-{label}.png"))
            await jump.click()
            await jump.wait_for(state="hidden")
            await page.get_by_role("button", name="重新生成", exact=False).click()
            await page.get_by_label("重新生成补充要求", exact=True).fill("更简洁")
            await page.locator(".regenerate-bar").get_by_role("button", name="重新生成", exact=False).click()
            await page.get_by_role("button", name="发送", exact=True).wait_for(timeout=30000)
            await page.get_by_role("button", name="后台记录", exact=False).click()
            await page.locator('.work-turn').first.wait_for()
            await assert_viewport(page, label + ":work-records")
            await page.get_by_role("button", name="返回对话", exact=True).click()
            for button, title in [("状态与记忆", "状态与记忆"), ("剧情分支", "新建对话"), ("上下文", "上下文预览")]:
                await page.get_by_role("button", name=button, exact=False).first.click()
                modal = page.get_by_role("dialog")
                await modal.get_by_role("heading", name=title, exact=True).wait_for()
                if title == "上下文预览":
                    await modal.get_by_text("预计输入（tokens）", exact=True).wait_for()
                    await modal.get_by_text("参考分词器 o200k_base", exact=False).wait_for()
                    assert "UTF-8 字节" not in await modal.inner_text()
                await assert_viewport(page, label + ":" + title)
                await modal.get_by_role("button", name="关闭", exact=True).click()
            if width // zoom < 760:
                await page.get_by_role("button", name="展开右侧栏", exact=True).click()
            await page.get_by_role("button", name="MVU 变量", exact=True).click()
            assert await page.locator('section[aria-label="MVU 变量"] > details').get_attribute("open") is None
            await assert_viewport(page, label + ":mvu")
            if width // zoom < 760:
                await page.get_by_role("button", name="折叠右侧栏", exact=True).click()
                await page.get_by_role("button", name="切换左侧栏" if width // zoom < 620 else "展开左侧栏", exact=True).click()
            await page.get_by_role("button", name="玩家角色", exact=False).click()
            await assert_viewport(page, label + ":persona")
            await page.get_by_role("button", name="阅读排版", exact=False).click()
            await assert_viewport(page, label + ":reading")
            await page.get_by_role("dialog").get_by_role("button", name="关闭", exact=True).click()
            await page.get_by_role("button", name="关于 Terrarium Tavern", exact=False).click()
            await assert_viewport(page, label + ":about")
            await page.get_by_role("dialog").get_by_role("button", name="关闭", exact=True).click()
            await page.get_by_role("button", name="＋ 开始新故事", exact=True).click()
            await assert_viewport(page, label + ":story-picker")
            await page.locator('.story-card-option').first.click()
            await assert_viewport(page, label + ":story-setup")
            await context.close()
        await browser.close()
    assert not errors, errors
    report = {"viewports": 4, "settings_pages": len(checked), "page_errors": errors,
              "checks": checked, "zoom_method": "equivalent CSS viewport with 2x device scale"}
    (OUTPUT / "browser.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    from tavern.core.models import SessionCreate
    from tavern.storage.store import Store

    OUTPUT.mkdir(parents=True, exist_ok=True)
    provider = ThreadingHTTPServer(("127.0.0.1", 8016), Provider)
    thread = Thread(target=provider.serve_forever, daemon=True)
    thread.start()
    with tempfile.TemporaryDirectory(prefix="release-ui-", dir=".cache") as data:
        store = Store(Path(data))
        cfg = store.settings()
        cfg.providers[0].base_url = "http://127.0.0.1:8016/v1"
        cfg.providers[0].api_key = "local-fixture"
        cfg.providers[0].use_proxy = False
        cfg.foreground.model = cfg.background.model = "fixture"
        store.save_settings(cfg)
        for filename in Path("samples").glob("*.json"):
            card = store.import_card(filename.read_bytes(), filename.name)
            store.new_session(SessionCreate(card_id=card["id"]))
        with (OUTPUT / "server.log").open("w", encoding="utf-8") as log:
            process = subprocess.Popen([sys.executable, "-m", "tavern", "--port", "8015", "--data", data],
                stdout=log, stderr=log, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            try:
                for _ in range(100):
                    try:
                        httpx.get(BASE + "/api/health", trust_env=False, timeout=1).raise_for_status()
                        break
                    except httpx.HTTPError:
                        if process.poll() is not None:
                            raise RuntimeError("Release test server failed to start")
                        time.sleep(.1)
                asyncio.run(main())
            finally:
                try:
                    httpx.post(BASE + "/api/shutdown", json={"confirm": True}, trust_env=False, timeout=3)
                    process.wait(timeout=10)
                except (httpx.HTTPError, subprocess.TimeoutExpired):
                    process.terminate()
                    process.wait(timeout=5)
                provider.shutdown()
                provider.server_close()
