"""MVU sidebar isolation and InitVar badge checks with disposable story data."""

import argparse
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
from tavern.desktop import DesktopWindow
from tavern.desktop_browser import system_browser
from tavern.scripting.network_guard import NETWORK_GUARD
from tavern.storage.store import dump


async def main(all_viewports=False, host_only=False):
    output = Path(".agents/.cache/mvu-panel")
    output.mkdir(parents=True, exist_ok=True)
    panel = """<style>#terminal {color:cyan;background:#06151a;padding:16px}</style>
<details id="terminal"><summary>诊断终端</summary><div>生命体征：<span id="hp"></span></div>
<details id="nested"><summary>扫描详情</summary>状态正常</details></details>
<script>document.getElementById('hp').textContent = Mvu.getMvuData({type:'chat'}).stat_data.hp;</script>"""
    document = """<head>
    <style>
    :root { --panel-color: cyan; }

        #terminal {color:var(--panel-color);background:#06151a;padding:16px}
    </style>
    <script>
        document.addEventListener('DOMContentLoaded', () => {
            document.getElementById('hp').textContent = Mvu.getMvuData({type:'chat'}).stat_data.hp;
        });
    </script>
</head>

<body>
    <details id="terminal"><summary>诊断终端</summary>

        <div>生命体征：<span id="hp"></span></div>
        <details id="nested"><summary>扫描详情</summary>状态正常</details>
    </details>
</body>"""
    formats = [panel, f"```html\n{panel}\n```", f"```\n{document}\n```", document]
    opening = "客人推门进来。\n\n" + panel + "\n医生收起工具。"
    with tempfile.TemporaryDirectory() as root:
        app = create_app(root)
        store = app.state.store
        card = store.import_card(
            dump(
                {
                    "name": "MVU 面板检查",
                    "first_mes": opening,
                    "character_book": {
                        "entries": [
                            {
                                "id": "initial",
                                "comment": "[InitVar] 默认开场终端数据",
                                "enabled": False,
                                "content": "hp: 45",
                            },
                            {"id": "ordinary", "comment": "普通条目", "content": "叙事设定"},
                        ]
                    },
                }
            ).encode(),
            "card.json",
        )
        with store.connection() as db:
            db.execute("UPDATE cards SET permission=1 WHERE id=?", (card["id"],))
        session = store.new_session(SessionCreate(card_id=card["id"]))
        snapshot = session["snapshot"]
        snapshot["state"] = {"stat_data": {"hp": 45}}
        with store.connection() as db:
            db.execute(
                "UPDATE messages SET display=?, snapshot=? WHERE id=?",
                (opening, dump(snapshot), session["head"]),
            )
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
                await asyncio.sleep(0.05)
            assert server.started
            async with async_playwright() as playwright:
                browser = (
                    await playwright.chromium.launch(**system_browser().launch_options)
                    if not host_only
                    else None
                )
                try:
                    viewports = [(2560, 1440, 1), (1920, 1080, 1), (360, 792, 3), (960, 540, 2)]
                    for index, (width, height, scale) in enumerate(
                        [] if host_only else viewports if all_viewports else viewports[:1]
                    ):
                        current = store.session(session["id"])
                        store.set_session_script_permission(session["id"], current["revision"], False)
                        with store.connection() as db:
                            db.execute(
                                "UPDATE messages SET display=?,snapshot=? WHERE id=?",
                                (
                                    "客人推门进来。\n\n" + formats[index] + "\n医生收起工具。",
                                    dump(snapshot),
                                    session["head"],
                                ),
                            )
                        context = await browser.new_context(
                            viewport={"width": width, "height": height}, device_scale_factor=scale
                        )
                        await context.add_init_script(NETWORK_GUARD)
                        await context.add_init_script(
                            f'if(window===window.top) localStorage.setItem("tavern-session",{json.dumps(session["id"])});'
                        )
                        page = await context.new_page()
                        if index == 0:
                            await page.route(
                                "**/static/runtime.js",
                                lambda route: route.fulfill(status=503, body="unavailable"),
                            )
                        errors = []
                        page.on("pageerror", lambda error: errors.append(str(error)))
                        await page.goto(address)
                        await page.locator(".story").wait_for()
                        if await page.get_by_role("button", name="展开右侧栏", exact=True).is_visible():
                            await page.get_by_role("button", name="展开右侧栏", exact=True).click()
                        await expect(page.get_by_role("tab", name="MVU 变量", exact=True)).to_have_attribute(
                            "aria-selected", "true"
                        )
                        sidebar = page.locator('section[aria-label="MVU 变量"]')
                        await expect(sidebar.get_by_role("status")).to_contain_text("面板已暂停")
                        await expect(sidebar.locator("iframe")).to_have_count(0)
                        await expect(sidebar).not_to_contain_text("box-sizing")
                        await sidebar.get_by_role("button", name="启用此对话的脚本", exact=True).click()
                        if index == 0:
                            await expect(sidebar.get_by_role("alert")).to_contain_text("503")
                            await page.unroute("**/static/runtime.js")
                            await sidebar.get_by_role("button", name="重新加载面板", exact=True).click()
                        frame = page.frame_locator('section[aria-label="MVU 变量"] iframe')
                        await expect(frame.locator("#hp")).to_have_text("45")
                        await expect(frame.locator("#terminal")).to_have_attribute("open", "")
                        await expect(frame.locator("#nested")).not_to_have_attribute("open", "")
                        assert await frame.locator("#terminal").evaluate(
                            "el => getComputedStyle(el).backgroundColor === 'rgb(6, 21, 26)'"
                        )
                        await expect(frame.locator("body")).not_to_contain_text("客人推门进来")
                        await expect(frame.locator("body")).not_to_contain_text("医生收起工具")
                        await expect(page.locator(".messages")).to_contain_text("客人推门进来。")
                        await expect(page.locator(".messages")).to_contain_text("医生收起工具。")
                        await expect(page.locator(".messages iframe")).to_have_count(0)
                        if index == 0:
                            changed = json.loads(dump(snapshot))
                            changed["state"]["stat_data"]["hp"] = 73
                            with store.connection() as db:
                                db.execute(
                                    "UPDATE messages SET snapshot=? WHERE id=?",
                                    (dump(changed), session["head"]),
                                )
                                db.execute("UPDATE cards SET permission=0 WHERE id=?", (card["id"],))
                            await page.reload()
                            await expect(frame.locator("#hp")).to_have_text("73")
                            ordinary = await browser.new_context()
                            try:
                                await ordinary.add_init_script(
                                    f'if(window===window.top) localStorage.setItem("tavern-session",{json.dumps(session["id"])});'
                                )
                                plain = await ordinary.new_page()
                                await plain.goto(address)
                                # Desktop browsers keep running card panels without the host guard.
                                plain_frame = plain.frame_locator('section[aria-label="MVU 变量"] iframe')
                                await expect(plain_frame.locator("#hp")).to_have_text("73")
                                # An Android WebView lacking document-start injection pauses them instead.
                                await ordinary.add_init_script(
                                    "window.TavernAndroid = {postMessage() {}, onmessage: null};"
                                )
                                await plain.reload()
                                await expect(plain.locator('.frame-shell [role="status"]')).to_contain_text(
                                    "当前 WebView 无法隔离"
                                )
                                await expect(plain.locator(".frame-shell iframe")).to_have_count(0)
                                await expect(plain.locator(".messages")).to_contain_text("客人推门进来")
                                assert store.session_card(session["id"])["permission"]
                            finally:
                                await ordinary.close()
                        await page.screenshot(path=str(output / f"panel-{width}.png"))
                        await page.get_by_role("tab", name="场景", exact=True).click()
                        await page.get_by_role("button", name="编辑对话世界书", exact=True).click()
                        badge = page.locator('[data-lore-id="initial"] .mvu-entry-label')
                        await expect(badge).to_have_text("已识别 MVU 条目")
                        await badge.scroll_into_view_if_needed()
                        await expect(badge).to_be_visible()
                        await expect(badge).to_be_in_viewport()
                        await expect(
                            page.locator('[data-lore-id="ordinary"] .mvu-entry-label')
                        ).to_have_count(0)
                        assert await badge.evaluate("""el => {
                            const badge = el.getBoundingClientRect(), card = el.closest('.lore-editor').getBoundingClientRect();
                            return badge.left >= card.left && badge.right <= card.right && badge.bottom <= card.bottom
                              && badge.top > card.top + card.height / 2;
                        }""")
                        await page.screenshot(path=str(output / f"badge-{width}.png"))
                        await badge.click()
                        await expect(
                            page.get_by_role("dialog", name="编辑世界书条目", exact=True)
                        ).to_be_visible()
                        assert await page.evaluate(
                            "document.documentElement.scrollWidth <= innerWidth && document.documentElement.scrollHeight <= innerHeight"
                        )
                        assert not errors, errors
                        await context.close()
                    # Exercise the actual owned-window policy and native close event,
                    # rather than treating a test browser's backend as a protected host.
                    profile = Path(root) / "desktop-profile"
                    (profile / "Default").mkdir(parents=True)
                    downloads = Path(root) / "downloads"
                    downloads.mkdir()
                    (profile / "Default/Preferences").write_text(
                        json.dumps(
                            {"download": {"default_directory": str(downloads), "prompt_for_download": False}}
                        ),
                        encoding="utf-8",
                    )
                    window = DesktopWindow(profile, address)
                    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    udp.bind(("127.0.0.1", 0))
                    udp.setblocking(False)
                    try:
                        await window.open()
                        outcomes = await window.page.evaluate(
                            r"""async port => {
                            const probe = async () => {
                                try {
                                    const peer = new RTCPeerConnection({iceServers:[{urls:'stun:127.0.0.1:PORT'}]});
                                    peer.createDataChannel('probe');
                                    await peer.setLocalDescription(await peer.createOffer());
                                    peer.close();
                                    return false;
                                } catch (error) { return /卡片不能/.test(String(error)); }
                            };
                            const code = probe.toString().replace('PORT', String(port));
                            const html = '<meta charset="utf-8"><script>(' + code + ')().then(value=>parent.postMessage({rtcProbe:value},"*"))<\/script>';
                            const blob = URL.createObjectURL(new Blob([html], {type:'text/html'}));
                            const results = [await (0,eval)('(' + code + ')()')];
                            for (const [key, value] of [['srcdoc', html], ['src', blob], ['src', 'data:text/html;charset=utf-8,' + encodeURIComponent(html)]]) {
                                const child = document.createElement('iframe');
                                const result = new Promise(resolve => {
                                    const receive = event => {
                                        if (event.source !== child.contentWindow || !('rtcProbe' in (event.data || {}))) return;
                                        removeEventListener('message', receive);
                                        resolve(event.data.rtcProbe);
                                    };
                                    addEventListener('message', receive);
                                });
                                child[key] = value;
                                document.body.append(child);
                                results.push(await result);
                                child.remove();
                            }
                            URL.revokeObjectURL(blob);
                            return results;
                        }""",
                            udp.getsockname()[1],
                        )
                        assert outcomes == [True] * 4, outcomes
                        try:
                            packet = udp.recv(1024)
                        except BlockingIOError:
                            packet = b""
                        assert not packet, "WebRTC emitted a loopback UDP packet"
                        cdp = await window.context.browser.new_browser_cdp_session()
                        downloaded = asyncio.get_running_loop().create_future()
                        cdp.on(
                            "Browser.downloadProgress",
                            lambda event: (
                                downloaded.set_result(event)
                                if event["state"] == "completed" and not downloaded.done()
                                else None
                            ),
                        )
                        await cdp.send(
                            "Browser.setDownloadBehavior", {"behavior": "default", "eventsEnabled": True}
                        )
                        await window.page.evaluate("""() => {
                            const anchor = document.createElement('a');
                            anchor.href = URL.createObjectURL(new Blob(['desktop-export-proof'], {type:'text/plain'}));
                            anchor.download = 'desktop-probe.txt'; document.body.append(anchor);
                            anchor.click(); anchor.remove();
                        }""")
                        await asyncio.wait_for(downloaded, 5)
                        assert (downloads / "desktop-probe.txt").read_text() == "desktop-export-proof"
                        confirmation = asyncio.get_running_loop().create_future()
                        window.context.on("dialog", lambda dialog: confirmation.set_result(dialog))
                        await window.page.evaluate("""() => {
                            const button = document.createElement('button'); button.id='host-probe-activation';
                            button.textContent='probe'; button.style='position:fixed;inset:10px auto auto 10px;z-index:2147483647';
                            document.body.append(button);
                            window.onbeforeunload = event => {event.preventDefault(); event.returnValue='';};
                        }""")
                        await window.page.locator("#host-probe-activation").click()
                        await window.page.close(run_before_unload=True)
                        dialog = await asyncio.wait_for(confirmation, 3)
                        assert dialog.type == "beforeunload"
                        await dialog.dismiss()
                        assert not window.page.is_closed()
                        await window.page.evaluate("window.onbeforeunload = null")
                        await window.page.close()
                        await asyncio.wait_for(window.closed.wait(), 2)
                    finally:
                        udp.close()
                        await window.close()
                    assert (downloads / "desktop-probe.txt").read_text() == "desktop-export-proof"
                finally:
                    if browser:
                        await browser.close()
            print(
                "Owned desktop host: WebRTC isolation, persistent download, unsaved-changes confirmation and close passed."
                if host_only
                else f"Card permission, MVU, plain browser gate and owned-window isolation passed; {len(viewports) if all_viewports else 1} viewport(s)."
            )
        finally:
            server.should_exit = True
            await asyncio.to_thread(thread.join, 10)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--all-viewports", action="store_true", help="布局变更时检查全部四个视口")
    parser.add_argument("--host-only", action="store_true", help="只检查受控桌面宿主")
    arguments = parser.parse_args()
    asyncio.run(main(arguments.all_viewports, arguments.host_only))
