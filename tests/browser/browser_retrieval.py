"""New-session browser smoke test, no user data or real API credentials."""
import asyncio
import json
import socket
import tempfile
from pathlib import Path
from threading import Thread

import uvicorn
from kohakuterrarium.testing.llm import ScriptedLLM
from playwright.async_api import async_playwright, expect

from tavern.application.api import create_app
from tavern.core.models import SessionCreate


async def main():
    with tempfile.TemporaryDirectory() as data:
        app = create_app(data)
        store, service = app.state.store, app.state.service
        card = store.import_card(json.dumps({'name':'检索测试角色','first_mes':'欢迎来到雾港。',
            'character_book':{'entries':[{'id':'a','keys':['灯塔'],'content':'银钥匙打开北塔铁门。'}]}}).encode(), 'c.json')
        session = store.new_session(SessionCreate(card_id=card['id']))
        service.provider_factory = lambda: ScriptedLLM(['守卫递来钥匙。<tavern_retrieval>北塔 银钥匙 守卫</tavern_retrieval>'])
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0))
            port = sock.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=port,log_level='error'))
        thread = Thread(target=server.run,daemon=True)
        thread.start()
        try:
            while not server.started:
                await asyncio.sleep(.05)
            async with async_playwright() as p:
                browser = await p.chromium.launch()
                page = await browser.new_page(viewport={'width':1440,'height':1000})
                errors=[]
                page.on('pageerror',lambda error:errors.append(str(error)))
                await page.goto(f'http://127.0.0.1:{port}')
                await page.get_by_role('button',name='⚙ 设置',exact=True).click()
                dialog=page.get_by_role('dialog',name='设置',exact=True)
                await expect(dialog.get_by_role('heading',name='API 提供商',exact=True)).to_be_visible()
                await dialog.get_by_role('button',name='添加提供商',exact=True).click()
                await dialog.get_by_label('名称',exact=True).last.fill('备用 API')
                await dialog.get_by_label('接口地址',exact=True).last.fill('http://127.0.0.1:9999/v1')
                await dialog.get_by_role('button',name='混合召回与本地模型',exact=True).click()
                await expect(dialog.get_by_role('heading',name='召回方案',exact=True)).to_be_visible()
                await expect(dialog.get_by_text('尚未下载',exact=True)).to_have_count(2)
                output=Path('.cache/retrieval-ui')
                output.mkdir(parents=True,exist_ok=True)
                await page.screenshot(path=str(output/'settings.png'),full_page=True)
                await dialog.get_by_role('button',name='网络与代理',exact=True).click()
                await expect(dialog.get_by_label('Hugging Face 下载使用代理')).to_be_checked()
                await dialog.get_by_role('button',name='系统提示词',exact=True).click()
                await expect(dialog.get_by_label('摘要系统提示词',exact=True)).to_have_count(0)
                await expect(dialog.get_by_label('检索线索提示词',exact=True)).to_be_visible()
                await dialog.get_by_role('button',name='保存设置',exact=True).click()
                await expect(dialog).to_be_hidden()
                # Generate through the same app route, then reload and inspect committed reading UI.
                response=await page.request.post(f'http://127.0.0.1:{port}/api/sessions/{session["id"]}/turns',data={
                    'text':'银钥匙在哪里','operation_id':'browser-retrieval','expected_revision':0})
                assert response.ok, await response.text()
                turn=await response.json()
                for _ in range(200):
                    current=store.turn(turn['id'])
                    if current['status'] in ('committed','error'):
                        break
                    await asyncio.sleep(.05)
                assert current['status']=='committed',current.get('error')
                await page.reload()
                # Select the new conversation by its sidebar row.
                await page.locator('.session-open').first.click()
                if not await page.locator('.retrieval-clue').count():
                    await page.get_by_text('检索测试角色',exact=False).first.click()
                await expect(page.locator('.retrieval-clue')).to_have_count(1)
                assert await page.locator('.retrieval-clue').get_attribute('open') is None
                assert '<tavern_retrieval>' not in await page.locator('.message').last.inner_text()
                output=Path('.cache/retrieval-ui')
                output.mkdir(parents=True,exist_ok=True)
                await page.screenshot(path=str(output/'desktop.png'),full_page=True)
                await page.get_by_role('button',name='折叠右侧栏',exact=True).click()
                await page.get_by_role('button',name='折叠左侧栏',exact=True).click()
                await page.set_viewport_size({'width':420,'height':850})
                await page.screenshot(path=str(output/'mobile.png'),full_page=True)
                assert not errors,errors
                await browser.close()
        finally:
            server.should_exit=True
            await asyncio.to_thread(thread.join,10)
            assert not thread.is_alive(), 'Isolated test service did not stop'


if __name__=='__main__':
    asyncio.run(main())
