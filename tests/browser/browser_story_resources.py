"""Resource lifecycle and compatible rendering, using disposable data and no model calls."""

import asyncio
import base64
import json
import re
import socket
import sys
import tempfile
from io import BytesIO
from threading import Event, Thread
from unittest.mock import patch

import httpx
import uvicorn
from PIL import Image
from playwright.async_api import async_playwright, expect

from tavern.application.api import create_app
from tavern.core.models import SessionCreate
from tavern.storage.store import dump


async def main(images_only=False):
    image = (
        "data:image/svg+xml;base64,"
        + base64.b64encode(
            (
                '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"><rect width="10" height="10" fill="red"/><!--'
                + "shared image " * 300
                + "--></svg>"
            ).encode()
        ).decode()
    )
    panel = """<meta name="tavern-capabilities" content="display-only live-state shared-assets">
<style>#hp{color:rgb(0,180,190)}</style><div id="hp"></div><input id="draft" placeholder="草稿">
<img id="asset" src="IMAGE"><button id="update">更新</button><button id="parse">解析</button><span id="parsed"></span>
<script>
window.instance=crypto.randomUUID();
const render=()=>document.querySelector('#hp').textContent=getVariables().stat_data.hp;
render();eventOn('tavern_state_updated',render);
document.querySelector('#update').onclick=()=>replaceVariables({stat_data:{hp:6}});
document.querySelector('#parse').onclick=async()=>{const result=await Mvu.parseMessage("_.add('hp', 1);",{stat_data:{hp:6}});document.querySelector('#parsed').textContent=result.stat_data.hp;};
</script>""".replace("IMAGE", image)
    remote = "https://images.example/gallery"
    requested = []
    if images_only:
        panel += """<img id="macro-image" src="ROOT/{{format_message_variable::stat_data.art}}.png">
<img id="dynamic-image"><img id="responsive-image"><div id="background-image"></div>
<button id="change-image">换图</button><button id="missing-image">失效图片</button>
<script>
const imageRoot='ROOT';
const dynamic=document.querySelector('#dynamic-image');dynamic.src=imageRoot+'/initial.png';
document.querySelector('#responsive-image').srcset=imageRoot+'/small.png 1x, '+imageRoot+'/large.png 2x';
document.querySelector('#background-image').style.backgroundImage='url('+imageRoot+'/background.png)';
document.body.insertAdjacentHTML('beforeend',TavernRuntime.transform('[art:scene]',[
  {findRegex:String.raw`/\\[art:(?<kind>\\w+)\\]/g`,replaceString:'<img id="regex-image" src="ROOT/$<kind>.png">'}
],2,'display'));
document.querySelector('#change-image').onclick=()=>dynamic.setAttribute('src',imageRoot+'/changed.png');
document.querySelector('#missing-image').onclick=()=>{dynamic.src=imageRoot+'/missing.png';};
</script>""".replace("ROOT", remote)
        panel = (
            "普通正文。![Markdown图片](ROOT/{{format_message_variable::stat_data.art}}-markdown.png)\n\n"
            '<img id="plain-image" src="ROOT/{{format_message_variable::stat_data.art}}-plain.png">\n\n'
        ).replace("ROOT", remote) + panel
    suspend = """<meta name="tavern-capabilities" content="display-only suspend">
<div>可恢复面板</div><input id="local-draft"><script>
window.instance=crypto.randomUUID();
window.TavernPanel={save:()=>({draft:document.querySelector('#local-draft').value}),restore:s=>{document.querySelector('#local-draft').value=s.draft;}};
</script>"""
    legacy = (
        '<div>旧面板</div><input id="legacy-draft"><script>window.instance=Math.random().toString()</script>'
    )
    with tempfile.TemporaryDirectory() as root:
        app = create_app(root)
        store = app.state.store
        if images_only:
            pixels = BytesIO()
            Image.new("RGB", (2, 3)).save(pixels, "PNG")

            async def validate(url):
                if not url.startswith(remote + "/"):
                    raise ValueError("测试禁止外部网络")

            async def route(request):
                requested.append(str(request.url))
                return (
                    httpx.Response(404)
                    if request.url.path.endswith("/missing.png")
                    else httpx.Response(200, headers={"content-type": "image/png"}, content=pixels.getvalue())
                )

            app.state.service.dependencies.client = lambda: httpx.AsyncClient(
                transport=httpx.MockTransport(route)
            )
            validation = patch("tavern.scripting.dependencies.validate_remote", validate)
            validation.start()
        card = store.import_card(dump({"name": "资源夹具", "first_mes": panel}).encode(), "card.json")
        with store.connection() as db:
            db.execute("UPDATE cards SET permission=1 WHERE id=?", (card["id"],))
        session = store.new_session(SessionCreate(card_id=card["id"]))
        snapshot = session["snapshot"]
        snapshot["state"] = {"stat_data": {"hp": 5, "art": "macro"}}
        parent = session["head"]
        with store.connection() as db:
            db.execute("UPDATE messages SET snapshot=? WHERE id=?", (dump(snapshot), parent))
            for i in range(1, 1 if images_only else 130):
                text = suspend if i == 120 else legacy if i == 121 else f"剧情 {i}。" + "完整正文。" * 100
                mid = f"resource-{i}"
                db.execute(
                    "INSERT INTO messages(id,session,parent,role,raw,model,display,snapshot,created) VALUES (?,?,?,?,?,?,?,?,?)",
                    (mid, session["id"], parent, "assistant", text, text, text, dump(snapshot), i),
                )
                parent = mid
            db.execute("UPDATE sessions SET head=? WHERE id=?", (parent, session["id"]))
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        ready = Event()

        class Server(uvicorn.Server):
            async def startup(self, sockets=None):
                await super().startup(sockets)
                ready.set()

        server = Server(uvicorn.Config(app, log_level="error"))
        thread = Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
        thread.start()
        try:
            assert await asyncio.to_thread(ready.wait, 10), "测试服务启动超时"
            async with async_playwright() as p:
                browser = await p.chromium.launch()
                try:
                    viewports = (
                        [(1920, 1080, 1)]
                        if images_only
                        else [(2560, 1440, 1), (1920, 1080, 1), (360, 792, 3), (960, 540, 2)]
                    )
                    for width, height, scale in viewports:
                        context = await browser.new_context(
                            viewport={"width": width, "height": height}, device_scale_factor=scale
                        )
                        await context.add_init_script(
                            f'if(window===top)localStorage.setItem("tavern-session",{json.dumps(session["id"])});'
                        )
                        page = await context.new_page()
                        errors, requests = [], []
                        page.on("pageerror", lambda error: errors.append(str(error)))
                        page.on("request", lambda request: requests.append(request.url))
                        await page.goto(f"http://127.0.0.1:{sock.getsockname()[1]}")
                        if not images_only:
                            await expect(page.locator(".story-history")).to_have_attribute(
                                "data-virtual", "true"
                            )
                        expand = page.get_by_role("button", name="展开右侧栏", exact=True)
                        if await expand.count():
                            await expand.click()
                        locator = page.locator('section[aria-label="MVU 变量"] iframe')
                        await expect(locator).to_have_count(1)
                        frame = await (await locator.element_handle()).content_frame()
                        await expect(frame.locator("#hp")).to_have_text(
                            "5" if width == 2560 or images_only else "6"
                        )
                        identity = await frame.evaluate("window.instance")
                        assert identity
                        assert await frame.locator("#hp").evaluate(
                            "el=>getComputedStyle(el).color==='rgb(0, 180, 190)'"
                        )
                        await expect(frame.locator("#asset")).to_have_js_property("naturalWidth", 10)
                        if images_only:
                            await expect(
                                page.locator('.messages img[alt="Markdown图片"]')
                            ).to_have_js_property("naturalWidth", 2)
                            plain = page.locator(".messages #plain-image")
                            await expect(
                                plain if await plain.count() else frame.locator("#plain-image")
                            ).to_have_js_property("naturalWidth", 2)
                            for name in ["macro-image", "regex-image", "dynamic-image"]:
                                await expect(frame.locator("#" + name)).to_have_js_property("naturalWidth", 2)
                            await frame.wait_for_function(
                                "document.querySelector('#responsive-image').naturalWidth > 0"
                            )
                            assert await frame.locator("#background-image").evaluate(
                                "el=>el.style.backgroundImage.startsWith('url(\"data:image/png;base64,')"
                            )
                            await frame.locator("#change-image").click()
                            await expect(frame.locator("#dynamic-image")).to_have_js_property(
                                "naturalWidth", 2
                            )
                            await expect(frame.locator("#dynamic-image")).to_have_js_property(
                                "src", remote + "/changed.png"
                            )
                            denied = await page.request.post(
                                f"http://127.0.0.1:{sock.getsockname()[1]}/api/sessions/{session['id']}/images",
                                data={
                                    "message_id": session["messages"][0]["id"],
                                    "url": "https://other.example/private.png",
                                },
                            )
                            assert denied.status == 400
                            await frame.locator("#missing-image").click()
                            await expect(page.get_by_role("alert")).to_contain_text("404")
                            await page.get_by_role("button", name="重新加载面板", exact=True).click()
                            frame = await (await locator.element_handle()).content_frame()
                            await expect(frame.locator("#dynamic-image")).to_have_js_property(
                                "naturalWidth", 2
                            )
                            assert not any(url.startswith("https://images.example") for url in requests)
                            assert all(
                                "$" not in url and "%24" not in url and "%7B" not in url for url in requested
                            )
                            assert requested.count(remote + "/initial.png") == 1
                            assert not errors, errors
                            await context.close()
                            continue
                        assert not any("/static/mvu-core.js" in url for url in requests)
                        assert await frame.evaluate("typeof MVUReference") == "undefined"
                        assert await frame.evaluate("TavernRuntime.checkpoint().loaded.length") == 0
                        assert len(await locator.get_attribute("srcdoc")) < 5000
                        await frame.locator("#draft").evaluate("el=>el.value='保留的输入'")
                        # One real interaction commits a new head; live state refresh preserves the frame.
                        if width == 2560:
                            await frame.locator("#update").evaluate("el=>el.click()")
                            try:
                                await expect(frame.locator("#hp")).to_have_text("6")
                            except AssertionError:
                                print(
                                    "Interaction diagnostics:", errors, store.session(session["id"])["turns"]
                                )
                                raise
                            assert await frame.evaluate("window.instance") == identity
                            await expect(frame.locator("#draft")).to_have_value("保留的输入")
                        await frame.locator("#parse").evaluate("el=>el.click()")
                        await expect(frame.locator("#parsed")).to_have_text("7")
                        assert sum("/static/mvu-core.js" in url for url in requests) == 1
                        await expect(locator).to_have_attribute("sandbox", "allow-scripts")
                        assert (
                            await frame.evaluate("(()=>{try{return !!parent.document}catch{return false}})()")
                            is False
                        )
                        if width <= 1200:
                            await page.get_by_role("button", name="折叠右侧栏", exact=True).click()
                        # Navigate using actual rail positions, then release and restore the opt-in frame.
                        rail = page.get_by_role("slider", name="对话历史导航")
                        await rail.focus()
                        ticks = rail.locator(".rail-tick:not(.current):not(.cached):not(.hovered)")
                        first, last = await ticks.first.bounding_box(), await ticks.last.bounding_box()
                        box = await rail.bounding_box()
                        top = first["y"] + first["height"] / 2
                        bottom = last["y"] + last["height"] / 2

                        async def jump(index):
                            # Pointer only: the rail click opens nearby rounds, then the exact round is clicked.
                            # Navigation aligns the message start, not its frame, so only the target is checked.
                            await page.mouse.click(
                                box["x"] + box["width"] / 2, top + (bottom - top) * index / 129
                            )
                            await page.get_by_role(
                                "button", name=f"跳转到第 {index + 1} 个对话位置", exact=True
                            ).click()
                            await expect(
                                page.locator(f'[data-message-id="resource-{index}"]')
                            ).to_be_in_viewport()

                        async def reach(index):
                            # At 200% zoom the message header can fill the short reading area, so the reader
                            # takes over scrolling. Frames start at 280px and shrink once the card reports its
                            # height; forms are only used after that, through real pointer actions.
                            await page.locator(".messages").dispatch_event("wheel", {"deltaY": 120})
                            shell = page.locator(f'[data-message-id="resource-{index}"] .frame-shell')
                            await shell.scroll_into_view_if_needed()
                            node = shell.locator("iframe")
                            await expect(node).not_to_have_attribute("style", re.compile(r"\bheight: 280px"))
                            return node, await (await node.element_handle()).content_frame()

                        await jump(120)
                        local, local_frame = await reach(120)
                        await local_frame.locator("#local-draft").click()
                        await local_frame.locator("#local-draft").fill("未提交表单")
                        local_id = await local_frame.evaluate("window.instance")
                        await jump(121)
                        old, old_frame = await reach(121)
                        await old_frame.locator("#legacy-draft").click()
                        await old_frame.locator("#legacy-draft").fill("旧卡输入")
                        old_id = await old_frame.evaluate("window.instance")
                        await rail.focus()
                        await rail.press("Home")
                        await expect(local).to_have_count(0)
                        await expect(old).to_have_count(1)
                        assert await old_frame.evaluate("window.instance") == old_id
                        await expect(old_frame.locator("#legacy-draft")).to_have_value("旧卡输入")
                        await jump(120)
                        _, restored = await reach(120)
                        await restored.locator("#local-draft").click()
                        await expect(restored.locator("#local-draft")).to_have_value("未提交表单")
                        assert await restored.evaluate("window.instance") != local_id
                        assert sum("/static/runtime.js" in url for url in requests) == 1
                        assert not errors, errors
                        assert await page.evaluate(
                            "document.documentElement.scrollWidth<=innerWidth && document.documentElement.scrollHeight<=innerHeight"
                        )
                        # A squeezed story column once turned the composer into a tall strip over the prose.
                        reading = await page.evaluate("""() => {
                            const list = document.querySelector('.messages').getBoundingClientRect();
                            const composer = document.querySelector('.composer').getBoundingClientRect();
                            return (composer.top - list.top) / list.height;
                        }""")
                        assert reading >= 0.4, reading
                        await context.close()
                finally:
                    await browser.close()
            print(
                "Dynamic image templates, snapshot macros, src/srcset/CSS, failure/retry and sandbox image bridge passed."
                if images_only
                else "Live state without reload, lazy core, isolated shared assets, suspended form recovery and retained legacy state: 4 viewports passed."
            )
        finally:
            server.should_exit = True
            await asyncio.to_thread(thread.join, 10)
            if images_only:
                validation.stop()


if __name__ == "__main__":
    asyncio.run(main(images_only="--images-only" in sys.argv))
