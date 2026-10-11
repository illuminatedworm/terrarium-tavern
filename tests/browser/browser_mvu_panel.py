"""MVU sidebar isolation and InitVar badge checks with disposable story data."""

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
    output = Path('.agents/.cache/mvu-panel')
    output.mkdir(parents=True, exist_ok=True)
    panel = '''<style>#terminal {color:cyan;background:#06151a;padding:16px}</style>
<details id="terminal"><summary>诊断终端</summary><div>生命体征：<span id="hp"></span></div>
<details id="nested"><summary>扫描详情</summary>状态正常</details></details>
<script>document.getElementById('hp').textContent = Mvu.getMvuData({type:'chat'}).stat_data.hp;</script>'''
    document = '''<head>
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
</body>'''
    formats = [panel, f'```html\n{panel}\n```', f'```\n{document}\n```', document]
    opening = '客人推门进来。\n\n' + panel + '\n医生收起工具。'
    with tempfile.TemporaryDirectory() as root:
        app = create_app(root)
        store = app.state.store
        card = store.import_card(dump({
            'name': 'MVU 面板检查', 'first_mes': opening,
            'character_book': {'entries': [
                {'id': 'initial', 'comment': '[InitVar] 默认开场终端数据', 'enabled': False,
                 'content': 'hp: 45', 'extensions': {'terrarium': {'audience': 'runtime_init'}}},
                {'id': 'ordinary', 'comment': '普通条目', 'content': '叙事设定'},
            ]},
        }).encode(), 'card.json')
        with store.connection() as db:
            db.execute('UPDATE cards SET permission=1 WHERE id=?', (card['id'],))
        session = store.new_session(SessionCreate(card_id=card['id']))
        snapshot = session['snapshot']
        snapshot['state'] = {'stat_data': {'hp': 45}}
        with store.connection() as db:
            db.execute('UPDATE messages SET display=?, snapshot=? WHERE id=?',
                       (opening, dump(snapshot), session['head']))
        sock = socket.socket()
        sock.bind(('127.0.0.1', 0))
        address = f'http://127.0.0.1:{sock.getsockname()[1]}'
        server = uvicorn.Server(uvicorn.Config(app, log_level='error'))
        thread = Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
        thread.start()
        try:
            for _ in range(100):
                if server.started:
                    break
                await asyncio.sleep(.05)
            assert server.started
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch()
                try:
                    for index, (width, height, scale) in enumerate([(2560, 1440, 1), (1920, 1080, 1), (360, 792, 3), (960, 540, 2)]):
                        current = store.session(session['id'])
                        store.set_session_script_permission(session['id'], current['revision'], False)
                        with store.connection() as db:
                            db.execute('UPDATE messages SET display=?,snapshot=? WHERE id=?', (
                                '客人推门进来。\n\n' + formats[index] + '\n医生收起工具。', dump(snapshot), session['head']))
                        context = await browser.new_context(
                            viewport={'width': width, 'height': height}, device_scale_factor=scale)
                        await context.add_init_script(
                            f'if(window===window.top) localStorage.setItem("tavern-session",{json.dumps(session["id"])});')
                        page = await context.new_page()
                        if index == 0:
                            await page.route('**/static/runtime.js', lambda route: route.fulfill(status=503, body='unavailable'))
                        errors = []
                        page.on('pageerror', lambda error: errors.append(str(error)))
                        await page.goto(address)
                        await page.locator('.story').wait_for()
                        if await page.get_by_role('button', name='展开右侧栏', exact=True).is_visible():
                            await page.get_by_role('button', name='展开右侧栏', exact=True).click()
                        await page.get_by_role('button', name='MVU 变量', exact=True).click()
                        sidebar = page.locator('section[aria-label="MVU 变量"]')
                        await expect(sidebar.get_by_role('status')).to_contain_text('面板已暂停')
                        await expect(sidebar.locator('iframe')).to_have_count(0)
                        await expect(sidebar).not_to_contain_text('box-sizing')
                        await sidebar.get_by_role('button', name='启用此对话的脚本', exact=True).click()
                        if index == 0:
                            await expect(sidebar.get_by_role('alert')).to_contain_text('503')
                            await page.unroute('**/static/runtime.js')
                            await sidebar.get_by_role('button', name='重新加载面板', exact=True).click()
                        frame = page.frame_locator('section[aria-label="MVU 变量"] iframe')
                        await expect(frame.locator('#hp')).to_have_text('45')
                        await expect(frame.locator('#terminal')).to_have_attribute('open', '')
                        await expect(frame.locator('#nested')).not_to_have_attribute('open', '')
                        assert await frame.locator('#terminal').evaluate(
                            "el => getComputedStyle(el).backgroundColor === 'rgb(6, 21, 26)'")
                        await expect(frame.locator('body')).not_to_contain_text('客人推门进来')
                        await expect(frame.locator('body')).not_to_contain_text('医生收起工具')
                        await expect(page.locator('.messages')).to_contain_text('客人推门进来。')
                        await expect(page.locator('.messages')).to_contain_text('医生收起工具。')
                        await expect(page.locator('.messages iframe')).to_have_count(0)
                        if index == 0:
                            changed = json.loads(dump(snapshot))
                            changed['state']['stat_data']['hp'] = 73
                            with store.connection() as db:
                                db.execute('UPDATE messages SET snapshot=? WHERE id=?', (dump(changed), session['head']))
                                db.execute('UPDATE cards SET permission=0 WHERE id=?', (card['id'],))
                            await page.reload()
                            await expect(frame.locator('#hp')).to_have_text('73')
                        await page.screenshot(path=str(output / f'panel-{width}.png'))
                        await page.get_by_role('button', name='场景', exact=True).click()
                        await page.get_by_role('button', name='编辑对话世界书', exact=True).click()
                        badge = page.locator('[data-lore-id="initial"] .mvu-entry-label')
                        await expect(badge).to_have_text('已识别 MVU 条目')
                        await badge.scroll_into_view_if_needed()
                        await expect(badge).to_be_visible()
                        await expect(badge).to_be_in_viewport()
                        await expect(page.locator('[data-lore-id="ordinary"] .mvu-entry-label')).to_have_count(0)
                        assert await badge.evaluate('''el => {
                            const badge = el.getBoundingClientRect(), card = el.closest('.lore-editor').getBoundingClientRect();
                            return badge.left >= card.left && badge.right <= card.right && badge.bottom <= card.bottom
                              && badge.top > card.top + card.height / 2;
                        }''')
                        await page.screenshot(path=str(output / f'badge-{width}.png'))
                        await badge.click()
                        await expect(page.get_by_role('dialog', name='编辑世界书条目', exact=True)).to_be_visible()
                        assert await page.evaluate(
                            'document.documentElement.scrollWidth <= innerWidth && document.documentElement.scrollHeight <= innerHeight')
                        assert not errors, errors
                        await context.close()
                finally:
                    await browser.close()
            print('Raw/fenced HTML, full documents, permission, SDK retry, refreshed state, intact narrative and badge: 4 viewports passed.')
        finally:
            server.should_exit = True
            await asyncio.to_thread(thread.join, 10)


if __name__ == '__main__':
    asyncio.run(main())
