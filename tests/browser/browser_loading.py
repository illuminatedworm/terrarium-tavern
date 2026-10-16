"""Delayed local requests exercise loading, retry, navigation and transient save feedback."""
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


async def main():
    output = Path('.cache/loading-ui')
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='tavern-loading-') as root:
        app = create_app(root)
        store = app.state.store
        card = store.import_card(json.dumps({'name':'载入测试角色','first_mes':'故事开场已经准备好了。'}).encode(), 'fixture.json')
        a = store.new_session(SessionCreate(card_id=card['id']))
        b = store.new_session(SessionCreate(card_id=card['id'],agent_mode='full_agent'))
        store.update_session(a['id'], 0, title='对话甲')
        store.update_session(b['id'], 0, title='对话乙')
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
            async with async_playwright() as p:
                browser = await p.chromium.launch()
                try:
                    page = await browser.new_page(viewport={'width':1440,'height':1000})
                    errors = []
                    page.on('pageerror', lambda e: errors.append(str(e)))
                    await page.goto(address)
                    await page.get_by_role('button',name='设置',exact=True).click()
                    status = page.locator('.settings-save-status')
                    await expect(status).to_have_attribute('data-state','已保存')
                    await expect(status).to_have_css('opacity','0',timeout=4500)
                    await page.get_by_label('输出上限（tokens）',exact=True).fill('3000')
                    await expect(status).to_have_css('opacity','1')
                    await expect(status).to_have_attribute('data-state','已保存')
                    await expect(status).to_have_css('opacity','0',timeout=4500)
                    await page.get_by_label('上下文窗口（tokens）',exact=True).fill('2048')
                    await expect(status).to_have_attribute('data-state','保存失败')
                    await page.wait_for_timeout(2600)
                    await expect(status).to_have_css('opacity','1')
                    await page.get_by_label('上下文窗口（tokens）',exact=True).fill('32768')
                    await page.get_by_role('dialog',name='设置',exact=True).get_by_role('button',name='关闭',exact=True).click()
                    async def delayed(route):
                        await asyncio.sleep(.7)
                        await route.continue_()
                    await page.route('**/api/sessions/'+a['id'], delayed)
                    await page.locator('.session-open').filter(has_text='对话甲').click()
                    loading = page.get_by_role('status',name='故事载入')
                    await expect(loading).to_contain_text('正在载入对话')
                    await expect(page.locator('.library')).to_have_count(0)
                    await page.screenshot(path=str(output/'existing-loading.png'))
                    await expect(loading).to_have_count(0)
                    await expect(page.locator('.story-heading')).to_contain_text('Half Agent')
                    await page.unroute('**/api/sessions/'+a['id'], delayed)
                    # A failed open can retry the same session, without creating a new story.
                    async def failing(route):
                        await route.fulfill(status=503,content_type='application/json',body=json.dumps({'detail':'本地测试：稍后重试'}))
                    await page.route('**/api/sessions/'+b['id'], failing)
                    await page.locator('.session-open').filter(has_text='对话乙').click()
                    await expect(loading).to_contain_text('暂时无法载入故事')
                    await page.screenshot(path=str(output/'loading-error.png'))
                    await page.unroute('**/api/sessions/'+b['id'], failing)
                    await page.get_by_role('button',name='重试载入',exact=True).click()
                    await expect(loading).to_have_count(0)
                    await expect(page.locator('.story-heading')).to_contain_text('Full Agent')
                    # Slow A must not replace B after the user switches again.
                    await page.route('**/api/sessions/'+a['id'], delayed)
                    await page.locator('.session-open').filter(has_text='对话甲').click()
                    await expect(loading).to_be_visible()
                    await page.locator('.session-open').filter(has_text='对话乙').click()
                    await expect(loading).to_have_count(0)
                    await page.wait_for_timeout(900)
                    await expect(page.locator('.story-heading')).to_contain_text('Full Agent')
                    await page.unroute('**/api/sessions/'+a['id'], delayed)
                    # Creation stays in a dedicated screen until initialization and open both finish.
                    count = len(store.sessions())
                    creation_requests = []
                    page.on('request', lambda request: creation_requests.append((request.method, request.url)))
                    await page.get_by_role('button',name='开始新故事',exact=True).click()
                    await page.get_by_role('button',name='选择角色卡 载入测试角色',exact=True).click()
                    await page.route('**/api/sessions', delayed)
                    await page.get_by_role('button',name='进入故事',exact=True).click()
                    await expect(loading).to_contain_text('正在进入新故事')
                    await page.screenshot(path=str(output/'new-story-loading.png'))
                    await expect(loading).to_have_count(0,timeout=15000)
                    assert len(store.sessions()) == count + 1
                    created_id = next(item['id'] for item in store.sessions() if item['id'] not in {a['id'], b['id']})
                    assert not any(method == 'GET' and url.endswith('/api/sessions/' + created_id)
                                   for method, url in creation_requests)
                    await expect(page.locator('.story-heading')).to_contain_text('载入测试角色')
                    assert not errors, errors
                    print('Save fade, persistent errors, existing/new story loading, retry and stale navigation passed.')
                finally:
                    await browser.close()
        finally:
            server.should_exit = True
            await asyncio.to_thread(thread.join,10)


if __name__ == '__main__':
    asyncio.run(main())
