"""Retry the first failed Full Agent reply after saving settings and reopening."""
import asyncio
import socket
import tempfile
from pathlib import Path
from threading import Thread

import uvicorn
from playwright.async_api import async_playwright, expect

from tavern.application.api import create_app
from tavern.core.models import SessionCreate, TurnRequest
from tests.backend.test_full_agent import Model, ScriptHost
from tests.browser.select_menu import choose_menu


async def main():
    with tempfile.TemporaryDirectory() as root:
        app = create_app(root)
        store, service = app.state.store, app.state.service
        settings = store.settings_document()
        settings.full_agent.tools.auto_retrieval = False
        store.save_settings(settings)
        card = store.import_card('{"name":"重试入口","first_mes":"开场。"}'.encode(), 'card.json')
        session = store.new_session(SessionCreate(card_id=card['id'], agent_mode='full_agent'))
        service.full.runtime = ScriptHost()
        service.full.provider_factory = lambda: Model(('read_state', {}), RuntimeError('测试失败'))
        failed = service.submit(session['id'], TurnRequest(text='保留的用户发言', operation_id='first', expected_revision=0))
        await service.tasks[failed['id']]
        # The seed ran on this loop; the fixture server owns subsequent tasks.
        service.full.tasks.pop(failed['id'])
        assert store.turn(failed['id'])['resumable']
        service.full.provider_factory = lambda: Model('按新设置重试成功。')
        listener = socket.socket()
        listener.bind(('127.0.0.1', 0))
        address = f'http://127.0.0.1:{listener.getsockname()[1]}'
        server = uvicorn.Server(uvicorn.Config(app, log_level='error'))
        thread = Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
        thread.start()
        try:
            for _ in range(100):
                if server.started:
                    break
                await asyncio.sleep(.05)
            async with async_playwright() as p:
                browser = await p.chromium.launch()
                page = await browser.new_page(viewport={'width': 1920, 'height': 1080})
                await page.goto(address)
                await page.evaluate("id=>localStorage.setItem('tavern-session',id)", session['id'])
                await page.reload()
                retry = page.get_by_role('button', name='↻ 重新生成', exact=True)
                await expect(retry).to_be_visible()
                await page.get_by_role('button', name='会话设置', exact=True).click()
                await page.get_by_role('textbox', name='本轮偏好', exact=True).fill('新的创作要求')
                await page.get_by_role('button', name='保存', exact=True).click()
                await expect(page.get_by_text('已保存', exact=True)).to_be_visible()
                await expect(retry).to_be_visible()
                await page.reload()
                await expect(retry).to_be_visible()
                await expect(page.locator('.message.user')).to_contain_text('保留的用户发言')
                await choose_menu(page.get_by_role('button', name='本次思考强度', exact=True), 'xhigh')
                await retry.click()
                await retry.click()
                await expect(page.locator('.message').last).to_contain_text('按新设置重试成功', timeout=15000)
                current = store.session(session['id'])
                request = store.turn(current['turns'][0]['id'])['request']
                assert request['resume_turn_id'] is None
                assert request['expected_revision'] == 1
                assert request['reasoning_effort'] == 'xhigh'
                assert [m['role'] for m in current['messages']] == ['assistant', 'user', 'assistant']
                output = Path('.agents/.local/retry-validation')
                output.mkdir(parents=True, exist_ok=True)
                await page.screenshot(path=str(output / 'retried.png'))
                await browser.close()
                print('First failed reply: retry survives settings save and reload; fresh request uses xhigh; no duplicate input.')
        finally:
            server.should_exit = True
            await asyncio.to_thread(thread.join, 10)


if __name__ == '__main__':
    asyncio.run(main())
