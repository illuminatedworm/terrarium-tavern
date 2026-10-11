"""Resource lifecycle and compatible rendering, using disposable data and no model calls."""
import asyncio
import base64
import json
import socket
import tempfile
from threading import Thread

import uvicorn
from playwright.async_api import async_playwright, expect

from tavern.application.api import create_app
from tavern.core.models import SessionCreate
from tavern.storage.store import dump


async def main():
    image = 'data:image/svg+xml;base64,' + base64.b64encode(
        ('<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"><rect width="10" height="10" fill="red"/><!--'
         + 'shared image ' * 300 + '--></svg>').encode()).decode()
    panel = '''<meta name="tavern-capabilities" content="display-only live-state shared-assets">
<style>#hp{color:rgb(0,180,190)}</style><div id="hp"></div><input id="draft" placeholder="草稿">
<img id="asset" src="IMAGE"><button id="update">更新</button><button id="parse">解析</button><span id="parsed"></span>
<script>
window.instance=crypto.randomUUID();
const render=()=>document.querySelector('#hp').textContent=getVariables().stat_data.hp;
render();eventOn('tavern_state_updated',render);
document.querySelector('#update').onclick=()=>replaceVariables({stat_data:{hp:6}});
document.querySelector('#parse').onclick=async()=>{const result=await Mvu.parseMessage("_.add('hp', 1);",{stat_data:{hp:6}});document.querySelector('#parsed').textContent=result.stat_data.hp;};
</script>'''.replace('IMAGE', image)
    suspend = '''<meta name="tavern-capabilities" content="display-only suspend">
<div>可恢复面板</div><input id="local-draft"><script>
window.instance=crypto.randomUUID();
window.TavernPanel={save:()=>({draft:document.querySelector('#local-draft').value}),restore:s=>{document.querySelector('#local-draft').value=s.draft;}};
</script>'''
    legacy = '<div>旧面板</div><input id="legacy-draft"><script>window.instance=Math.random().toString()</script>'
    with tempfile.TemporaryDirectory() as root:
        app = create_app(root)
        store = app.state.store
        card = store.import_card(dump({'name': '资源夹具', 'first_mes': panel}).encode(), 'card.json')
        with store.connection() as db:
            db.execute('UPDATE cards SET permission=1 WHERE id=?', (card['id'],))
        session = store.new_session(SessionCreate(card_id=card['id']))
        snapshot = session['snapshot']
        snapshot['state'] = {'stat_data': {'hp': 5}}
        parent = session['head']
        with store.connection() as db:
            db.execute('UPDATE messages SET snapshot=? WHERE id=?', (dump(snapshot), parent))
            for i in range(1, 130):
                text = suspend if i == 120 else legacy if i == 121 else f'剧情 {i}。' + '完整正文。' * 100
                mid = f'resource-{i}'
                db.execute('INSERT INTO messages(id,session,parent,role,raw,model,display,snapshot,created) VALUES (?,?,?,?,?,?,?,?,?)',
                           (mid, session['id'], parent, 'assistant', text, text, text, dump(snapshot), i))
                parent = mid
            db.execute('UPDATE sessions SET head=? WHERE id=?', (parent, session['id']))
        sock = socket.socket()
        sock.bind(('127.0.0.1', 0))
        server = uvicorn.Server(uvicorn.Config(app, log_level='error'))
        thread = Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
        thread.start()
        try:
            for _ in range(100):
                if server.started:
                    break
                await asyncio.sleep(.05)
            async with async_playwright() as p:
                browser = await p.chromium.launch()
                try:
                    for width, height, scale in [(2560, 1440, 1), (1920, 1080, 1), (360, 792, 3), (960, 540, 2)]:
                        context = await browser.new_context(viewport={'width': width, 'height': height}, device_scale_factor=scale)
                        await context.add_init_script(f'if(window===top)localStorage.setItem("tavern-session",{json.dumps(session["id"])});')
                        page = await context.new_page()
                        errors, requests = [], []
                        page.on('pageerror', lambda error: errors.append(str(error)))
                        page.on('request', lambda request: requests.append(request.url))
                        await page.goto(f'http://127.0.0.1:{sock.getsockname()[1]}')
                        await expect(page.locator('.story-history')).to_have_attribute('data-virtual', 'true')
                        expand = page.get_by_role('button', name='展开右侧栏', exact=True)
                        if await expand.count():
                            await expand.click()
                        locator = page.locator('section[aria-label="MVU 变量"] iframe')
                        await expect(locator).to_have_count(1)
                        frame = await (await locator.element_handle()).content_frame()
                        await expect(frame.locator('#hp')).to_have_text('5' if width == 2560 else '6')
                        identity = await frame.evaluate('window.instance')
                        assert identity
                        assert await frame.locator('#hp').evaluate("el=>getComputedStyle(el).color==='rgb(0, 180, 190)'")
                        await expect(frame.locator('#asset')).to_have_js_property('naturalWidth', 10)
                        assert not any('/static/mvu-core.js' in url for url in requests)
                        assert await frame.evaluate('typeof MVUReference') == 'undefined'
                        assert await frame.evaluate('TavernRuntime.checkpoint().loaded.length') == 0
                        assert len(await locator.get_attribute('srcdoc')) < 5000
                        await frame.locator('#draft').evaluate("el=>el.value='保留的输入'")
                        # One real interaction commits a new head; live state refresh preserves the frame.
                        if width == 2560:
                            await frame.locator('#update').evaluate('el=>el.click()')
                            try:
                                await expect(frame.locator('#hp')).to_have_text('6')
                            except AssertionError:
                                print('Interaction diagnostics:', errors, store.session(session['id'])['turns'])
                                raise
                            assert await frame.evaluate('window.instance') == identity
                            await expect(frame.locator('#draft')).to_have_value('保留的输入')
                        await frame.locator('#parse').evaluate('el=>el.click()')
                        await expect(frame.locator('#parsed')).to_have_text('7')
                        assert sum('/static/mvu-core.js' in url for url in requests) == 1
                        await expect(locator).to_have_attribute('sandbox', 'allow-scripts')
                        assert await frame.evaluate('(()=>{try{return !!parent.document}catch{return false}})()') is False
                        if width <= 760:
                            await page.get_by_role('button', name='折叠右侧栏', exact=True).click()
                        # Navigate using actual rail positions, then release and restore the opt-in frame.
                        rail = page.get_by_role('slider', name='对话历史导航')
                        await rail.focus()
                        ticks = rail.locator('.rail-tick:not(.current):not(.cached):not(.hovered)')
                        first, last = await ticks.first.bounding_box(), await ticks.last.bounding_box()
                        box = await rail.bounding_box()
                        top = first['y'] + first['height'] / 2
                        bottom = last['y'] + last['height'] / 2

                        async def jump(index):
                            await page.mouse.click(box['x'] + box['width'] / 2, top + (bottom - top) * index / 129)
                            await page.get_by_role('button', name=f'跳转到第 {index + 1} 个对话位置', exact=True).press('Enter')
                            # At 200% zoom the message header can fill the short reading area.
                            # A reader scrolls beyond the pinned header to reach the form itself.
                            await page.locator('.messages').dispatch_event('wheel', {'deltaY': 120})
                            await page.locator(f'[data-message-id="resource-{index}"] .frame-shell').scroll_into_view_if_needed()

                        await jump(120)
                        local = page.locator('[data-message-id="resource-120"] iframe')
                        await expect(local).to_be_in_viewport()
                        local_frame = await (await local.element_handle()).content_frame()
                        await expect(local_frame.locator('#local-draft')).to_have_count(1)
                        await local_frame.locator('#local-draft').evaluate("el=>el.value='未提交表单'")
                        local_id = await local_frame.evaluate('window.instance')
                        await jump(121)
                        old = page.locator('[data-message-id="resource-121"] iframe')
                        await expect(old).to_have_count(1)
                        old_frame = await (await old.element_handle()).content_frame()
                        await expect(old_frame.locator('#legacy-draft')).to_have_count(1)
                        await old_frame.locator('#legacy-draft').evaluate("el=>el.value='旧卡输入'")
                        old_id = await old_frame.evaluate('window.instance')
                        await rail.focus()
                        await rail.press('Home')
                        await expect(local).to_have_count(0)
                        await expect(old).to_have_count(1)
                        assert await old_frame.evaluate('window.instance') == old_id
                        await expect(old_frame.locator('#legacy-draft')).to_have_value('旧卡输入')
                        await jump(120)
                        await expect(local).to_be_in_viewport()
                        restored = await (await local.element_handle()).content_frame()
                        await expect(restored.locator('#local-draft')).to_have_value('未提交表单')
                        assert await restored.evaluate('window.instance') != local_id
                        assert sum('/static/runtime.js' in url for url in requests) == 1
                        assert not errors, errors
                        assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth && document.documentElement.scrollHeight<=innerHeight')
                        await context.close()
                finally:
                    await browser.close()
            print('Live state without reload, lazy core, isolated shared assets, suspended form recovery and retained legacy state: 4 viewports passed.')
        finally:
            server.should_exit = True
            await asyncio.to_thread(thread.join, 10)


if __name__ == '__main__':
    asyncio.run(main())
