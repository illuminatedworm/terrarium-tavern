"""Plain/Markdown/HTML-only variable displays, with scripts disabled and real snapshots."""

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
from tavern.storage.store import dump


async def main():
    output = Path(".agents/.cache/mvu-formats")
    output.mkdir(parents=True, exist_ok=True)
    text = """前文完整保留。

**生命：{{format_message_variable::stat_data.hp.0}}**

余额：{{get_message_variable::stat_data.money}}

```html
<style>#plain-status{color:rgb(0,180,190);background:rgb(6,21,26);padding:12px}</style>
<div id="plain-status">生命：{{get_message_variable::stat_data.hp[0]}}</div>
```

中间剧情完整保留。

```html
<html><head><style>#items{color:rgb(200,170,100)}</style></head><body>
<div id="items">{{get_message_variable::stat_data.items}}</div>
</body></html>
```

末尾剧情完整保留。

变量文本：{{get_message_variable::stat_data.literal}}
"""
    with tempfile.TemporaryDirectory() as root:
        app = create_app(root)
        store = app.state.store
        card = store.import_card(dump({"name": "非脚本状态显示", "first_mes": text}).encode(), "card.json")
        session = store.new_session(SessionCreate(card_id=card["id"]))
        snapshot = session["snapshot"]
        snapshot["state"] = {
            "stat_data": {
                "hp": [45, "生命说明"],
                "money": 0,
                "items": ["钥匙", "地图"],
                "literal": "<script>window.shouldNotExecute = true;</script>",
            }
        }
        with store.connection() as db:
            db.execute("UPDATE messages SET snapshot=? WHERE id=?", (dump(snapshot), session["head"]))
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
        thread = Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
        thread.start()
        try:
            for _ in range(100):
                if server.started:
                    break
                await asyncio.sleep(0.05)
            async with async_playwright() as p:
                browser = await p.chromium.launch()
                try:
                    for width, height, scale in [
                        (2560, 1440, 1),
                        (1920, 1080, 1),
                        (360, 792, 3),
                        (960, 540, 2),
                    ]:
                        context = await browser.new_context(
                            viewport={"width": width, "height": height}, device_scale_factor=scale
                        )
                        await context.add_init_script(
                            f'if(window===window.top) localStorage.setItem("tavern-session",{json.dumps(session["id"])});'
                        )
                        page = await context.new_page()
                        scripts, errors = [], []
                        page.on(
                            "request",
                            lambda request: (
                                scripts.append(request.url) if "/static/runtime.js" in request.url else None
                            ),
                        )
                        page.on("pageerror", lambda error: errors.append(str(error)))
                        await page.goto(f"http://127.0.0.1:{sock.getsockname()[1]}")
                        messages = page.locator(".messages")
                        await expect(messages.locator("iframe")).to_have_count(2)
                        for phrase in [
                            "前文完整保留。",
                            "中间剧情完整保留。",
                            "末尾剧情完整保留。",
                            "余额：0",
                        ]:
                            await expect(messages).to_contain_text(phrase)
                        await expect(messages.locator("strong").filter(has_text="生命：45")).to_have_count(1)
                        first = messages.frame_locator("iframe").nth(0)
                        second = messages.frame_locator("iframe").nth(1)
                        await expect(first.locator("#plain-status")).to_have_text("生命：45")
                        await expect(second.locator("#items")).to_have_text('["钥匙","地图"]')
                        assert await first.locator("#plain-status").evaluate(
                            "el => getComputedStyle(el).backgroundColor === 'rgb(6, 21, 26)'"
                        )
                        assert await first.locator("body").evaluate(
                            "el => getComputedStyle(el).color === 'rgb(185, 183, 190)'"
                        )
                        assert await second.locator("#items").evaluate(
                            "el => getComputedStyle(el).color === 'rgb(200, 170, 100)'"
                        )
                        for frame in await messages.locator("iframe").all():
                            await expect(frame).to_have_attribute("sandbox", "")
                        assert await page.evaluate("window.shouldNotExecute === undefined")
                        assert not scripts and not errors, (scripts, errors)
                        await page.screenshot(path=str(output / f"mixed-{width}.png"))
                        assert await page.evaluate(
                            "document.documentElement.scrollWidth <= innerWidth && document.documentElement.scrollHeight <= innerHeight"
                        )
                        await context.close()
                    assert store.session(session["id"])["snapshot"]["state"] == snapshot["state"]
                    assert store.card(card["id"])["permission"] == 0
                finally:
                    await browser.close()
            print(
                "Plain text, Markdown, two CSS-only frames, complete prose, unchanged arrays and no script execution: 4 viewports passed."
            )
        finally:
            server.should_exit = True
            await asyncio.to_thread(thread.join, 10)


if __name__ == "__main__":
    asyncio.run(main())
