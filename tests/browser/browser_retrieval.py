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
                await page.get_by_role('button',name='设置',exact=True).click()
                dialog=page.get_by_role('dialog',name='设置',exact=True)
                await dialog.get_by_role('button',name='通用 提供商',exact=True).click()
                await dialog.get_by_role('button',name='添加提供商',exact=True).click()
                await dialog.get_by_label('名称',exact=True).last.fill('备用 API')
                await dialog.get_by_label('主域名',exact=True).last.fill('http://127.0.0.1:9999')
                await dialog.get_by_role('button',name='通用 本地模型',exact=True).click()
                await expect(dialog.get_by_role('heading',name='本地检索模型',exact=True)).to_be_visible()
                await expect(dialog.get_by_text('尚未下载',exact=True)).to_have_count(2)
                for index, (purpose, model_type) in enumerate([('向量模型', 'Embedding'), ('重排模型', 'Reranker')]):
                    model = dialog.get_by_role('combobox',name='模型',exact=True).nth(index)
                    prefix = f'Qwen/Qwen3-{model_type}-'
                    await expect(model).to_have_value(prefix + '0.6B')
                    async with page.expect_response(lambda response: response.request.method == 'PATCH' and response.url.endswith('/settings/shared')):
                        await model.select_option(prefix + '4B')
                    await expect(dialog.locator('.settings-save-status')).to_contain_text('已保存')
                    response = await page.request.get(f'http://127.0.0.1:{port}/api/settings')
                    assert (await response.json())['shared']['local_models']['embedding' if model_type == 'Embedding' else 'reranker']['model'] == prefix + '4B'
                    quantization = dialog.get_by_role('combobox',name='量化',exact=True).nth(index)
                    await expect(quantization).to_have_value('none')
                    async with page.expect_response(lambda response: response.request.method == 'PATCH' and response.url.endswith('/settings/shared')):
                        await quantization.select_option('int8')
                    await expect(dialog.locator('.settings-save-status')).to_contain_text('已保存')
                    response = await page.request.get(f'http://127.0.0.1:{port}/api/settings')
                    assert (await response.json())['shared']['local_models']['embedding' if model_type == 'Embedding' else 'reranker']['quantization'] == 'int8'
                    async with page.expect_response(lambda response: response.request.method == 'PATCH' and response.url.endswith('/settings/shared')):
                        await quantization.select_option('none')
                    await expect(dialog.locator('.settings-save-status')).to_contain_text('已保存')
                    async with page.expect_response(lambda response: response.request.method == 'PATCH' and response.url.endswith('/settings/shared')):
                        await model.select_option(prefix + '0.6B')
                    await expect(dialog.locator('.settings-save-status')).to_contain_text('已保存')
                output=Path('.cache/retrieval-ui')
                output.mkdir(parents=True,exist_ok=True)
                await page.screenshot(path=str(output/'settings.png'),full_page=True)
                await dialog.get_by_role('button',name='通用 网络与代理',exact=True).click()
                await expect(dialog.get_by_label('本地模型下载使用代理')).to_be_checked()
                await dialog.get_by_role('button',name='half agent 提示词',exact=True).click()
                await expect(dialog.get_by_label('摘要系统提示词',exact=True)).to_have_count(0)
                await dialog.get_by_role('button',name='检索线索提示词',exact=True).click()
                await expect(dialog.get_by_role('textbox',name='检索线索提示词',exact=True)).to_be_visible()
                await dialog.get_by_role('button',name='关闭',exact=True).click()
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
