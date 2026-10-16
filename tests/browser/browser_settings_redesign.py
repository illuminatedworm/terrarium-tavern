"""Real-browser settings isolation, save boundaries, undo and responsive acceptance."""
import asyncio
import json
import socket
import tempfile
from pathlib import Path
from threading import Thread

import uvicorn
from playwright.async_api import async_playwright, expect

from tavern.application.api import create_app
from tavern.core.models import ContextBlock
from tests.browser.select_menu import choose_model


async def main():
    output = Path('.cache/settings-redesign')
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='tavern-settings-browser-') as root:
        app = create_app(root)
        settings = app.state.store.settings_document()
        recipe = settings.half_agent.context_recipe
        recipe.blocks = [b for b in recipe.blocks if b.source not in
                         ('system_prompt', 'description', 'personality', 'scenario')]
        recipe.blocks.insert(2, ContextBlock(id='character', source='character', depth=2, priority=90))
        app.state.store.save_settings(settings)
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
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch()
                try:
                    page = await browser.new_page(viewport={'width':1920,'height':1080})
                    errors = []
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    await page.goto(address)
                    await page.get_by_role('button', name='设置', exact=True).click()
                    dialog = page.get_by_role('dialog', name='设置', exact=True)
                    await page.get_by_role('button', name='full agent 模型与生成', exact=True).wait_for()
                    await expect(page.get_by_role('status')).to_have_text('✓ 已保存')
                    initial_height = await dialog.evaluate('e=>e.getBoundingClientRect().height')
                    async def read():
                        return (await (await page.request.get(address+'/api/settings')).json())
                    async def select(label):
                        await page.get_by_role('button', name=label, exact=True).click()
                    async def saved():
                        await expect(page.locator('.settings-save-status')).to_contain_text('已保存')
                    before = await read()
                    # Acknowledged edits may not overwrite a newer input made in flight.
                    async def delayed(route):
                        await asyncio.sleep(.25)
                        await route.continue_()
                    await page.route('**/api/settings/full_agent', delayed)
                    output_field = page.get_by_label('输出上限（tokens）', exact=True)
                    await output_field.fill('3000')
                    await expect(page.locator('.settings-save-status')).to_contain_text('正在保存')
                    await output_field.fill('4096')
                    await saved()
                    await expect(output_field).to_have_value('4096')
                    current = await read()
                    assert current['full_agent']['model']['reply_tokens'] == 4096
                    assert current['half_agent'] == before['half_agent']
                    await page.unroute('**/api/settings/full_agent', delayed)
                    await page.get_by_role('button', name='撤回本页修改', exact=True).click()
                    await expect(output_field).to_have_value('2048')
                    await saved()
                    # Server validation is visible; invalid drafts never reach persisted settings.
                    context = page.get_by_label('上下文窗口（tokens）', exact=True)
                    await context.fill('2048')
                    await expect(page.locator('.settings-save-status')).to_have_text('保存失败')
                    assert (await read())['full_agent']['model']['context_tokens'] == 32768
                    await select('half agent 模型与生成')
                    await expect(page.get_by_role('alertdialog')).to_have_count(0)
                    async with page.expect_response(lambda response: response.request.method == 'PATCH' and response.url.endswith('/settings/half_agent')):
                        await choose_model(page.get_by_role('region',name='后台整理',exact=True).get_by_role('button', name='模型名称', exact=True), 'valid-other-scope')
                    assert (await read())['half_agent']['background']['model'] == 'valid-other-scope'
                    await select('full agent 模型与生成')
                    await context.fill('32768')
                    await saved()
                    # Text drafts are outside the automatic-save object.
                    await select('full agent 提示词')
                    text = page.get_by_role('textbox', name='系统提示词', exact=True)
                    field = page.locator('.saved-text-field').first
                    assert await field.evaluate('e => { const box = e.querySelector("textarea").getBoundingClientRect(); const button = e.querySelector(".expand-editor").getBoundingClientRect(); return button.left >= box.left && box.right - button.right >= 20 && button.top >= box.top && button.bottom <= box.bottom }')
                    await text.fill('尚未保存的独立提示词')
                    save_button = page.get_by_role('button', name='保存此文本：系统提示词', exact=True)
                    await expect(save_button).to_be_visible()
                    assert await field.evaluate('e => { const save = e.querySelector(".text-save").getBoundingClientRect(); const expand = e.querySelector(".expand-editor").getBoundingClientRect(); const box = e.querySelector("textarea").getBoundingClientRect(); return save.right < expand.left && save.bottom <= box.bottom && save.top >= box.top }')
                    await page.screenshot(path=str(output/'prompt-inline.png'))
                    assert (await read())['full_agent']['system_prompt'] == before['full_agent']['system_prompt']
                    await select('half agent 模型与生成')
                    confirm = page.get_by_role('alertdialog')
                    await expect(confirm).to_be_visible()
                    assert await confirm.get_by_role('button').all_text_contents() == ['继续编辑', '放弃修改']
                    await expect(confirm.get_by_role('button', name='继续编辑')).to_be_focused()
                    await confirm.get_by_role('button', name='继续编辑').click()
                    await expect(text).to_have_value('尚未保存的独立提示词')
                    async def fail_text(route):
                        await route.abort('failed')
                    await page.route('**/api/settings/full_agent', fail_text)
                    await page.get_by_role('button', name='保存此文本：系统提示词', exact=True).click()
                    await expect(page.locator('.saved-text-error')).to_be_visible()
                    assert (await read())['full_agent']['system_prompt'] == before['full_agent']['system_prompt']
                    await page.unroute('**/api/settings/full_agent', fail_text)
                    await page.get_by_role('button', name='保存此文本：系统提示词', exact=True).click()
                    await expect(page.get_by_role('button', name='保存此文本：系统提示词', exact=True)).to_have_count(0)
                    await saved()
                    assert (await read())['full_agent']['system_prompt'] == '尚未保存的独立提示词'
                    assert (await read())['half_agent']['system_prompt'] == before['half_agent']['system_prompt']
                    await page.get_by_role('button', name='撤回本页修改', exact=True).click()
                    await expect(text).to_have_value(before['full_agent']['system_prompt'])
                    # Expanded editing uses the same explicit commit, and closing is guarded.
                    await page.get_by_role('button', name='放大编辑：系统提示词', exact=True).click()
                    expanded = page.get_by_role('dialog', name='编辑：系统提示词', exact=True)
                    await expanded.get_by_role('textbox').fill('应当放弃')
                    await expanded.get_by_role('button', name='关闭放大编辑').click()
                    await expect(confirm).to_be_visible()
                    await page.screenshot(path=str(output/'unsaved-confirm.png'))
                    await confirm.get_by_role('button', name='放弃修改').click()
                    await expect(expanded).to_have_count(0)
                    await expect(text).to_have_value(before['full_agent']['system_prompt'])
                    await text.fill('关闭设置也必须提示')
                    await dialog.get_by_role('button', name='关闭', exact=True).click()
                    await expect(confirm).to_be_visible()
                    await confirm.get_by_role('button', name='放弃修改').click()
                    await expect(dialog).to_have_count(0)
                    await page.get_by_role('button', name='设置', exact=True).click()
                    await select('half agent 模型与生成')
                    await choose_model(page.get_by_role('region',name='后台整理',exact=True).get_by_role('button', name='模型名称', exact=True), 'isolated-background')
                    await saved()
                    current = await read()
                    assert current['half_agent']['background']['model'] == 'isolated-background'
                    assert current['full_agent']['model'] == before['full_agent']['model']
                    assert current['shared']['card_editor'] == before['shared']['card_editor']
                    await page.screenshot(path=str(output/'half-agent-desktop.png'))
                    # Undo on one page does not roll back changes on another page.
                    await select('full agent 检索与世界书')
                    await page.get_by_label('单次新增条目上限', exact=True).fill('9')
                    await saved()
                    await select('full agent 模型与生成')
                    await page.get_by_label('输出上限（tokens）', exact=True).fill('3000')
                    await saved()
                    await page.get_by_role('button', name='撤回本页修改', exact=True).click()
                    await saved()
                    assert (await read())['full_agent']['lore_max_entries'] == 9
                    assert (await read())['full_agent']['model']['reply_tokens'] == 2048
                    # Page search opens folded fields and focuses the control.
                    await page.get_by_role('searchbox', name='搜索设置').fill('提示缓存')
                    await page.locator('.settings-search-results').get_by_role('button').filter(has_text='full agent').click()
                    await expect(page.get_by_role('combobox', name='缓存控制', exact=True)).to_be_focused()
                    # Cache JSON is atomic, invalid drafts are visible and guarded.
                    control = page.get_by_role('combobox', name='缓存控制', exact=True)
                    await expect(control).to_have_value('runtime')
                    await page.get_by_label('参数格式', exact=True).select_option('custom')
                    await saved()
                    await page.get_by_label('断点字段名', exact=True).fill('custom_cache')
                    await page.get_by_label('断点 JSON 值', exact=True).fill('{"enabled": true}')
                    request_json = page.get_by_label('附加请求参数（JSON，可留 {}）', exact=True)
                    await request_json.fill('{"prompt_cache_options":{"mode":"explicit"}}')
                    await saved()
                    current = await read()
                    assert current['full_agent']['model']['cache']['value'] == {'enabled': True}
                    assert current['full_agent']['model']['cache']['request'] == {'prompt_cache_options': {'mode': 'explicit'}}
                    await request_json.fill('{')
                    await expect(request_json).to_have_attribute('aria-invalid', 'true')
                    await expect(page.locator('.settings-save-status')).to_contain_text('有参数格式错误')
                    await select('half agent 模型与生成')
                    await expect(page.get_by_role('alertdialog')).to_be_visible()
                    await page.get_by_role('button', name='继续编辑', exact=True).click()
                    await request_json.fill('{}')
                    await saved()
                    assert (await read())['full_agent']['model']['cache']['request'] == {}
                    await page.screenshot(path=str(output/'cache-custom-desktop.png'))
                    await page.get_by_label('参数格式', exact=True).select_option('auto')
                    await saved()
                    original_model = (await read())['full_agent']['model']['model']
                    for protocol in ('chat_completions', 'responses', 'anthropic', 'gemini'):
                        await select('通用 提供商')
                        await expect(page.get_by_label('接口方式', exact=True)).to_have_count(0)
                        await expect(page.get_by_label('主域名', exact=True)).to_have_value('https://api.openai.com')
                        await select('full agent 模型与生成')
                        await page.get_by_label('接口方式', exact=True).first.select_option(protocol)
                        await saved()
                        assert (await read())['full_agent']['model']['api_mode'] == protocol
                        await select('full agent 模型与生成')
                        await choose_model(page.get_by_role('button', name='模型名称', exact=True), 'gemini-3.8-flash-high' if protocol == 'gemini' else 'claude-opus-4-6' if protocol == 'anthropic' else 'gpt-5.6-sol')
                        await saved()
                        await page.get_by_text('更多参数：采样细项、分词器与提示缓存', exact=True).click()
                        if protocol == 'gemini':
                            await expect(page.get_by_text('由运行时创建和引用 Gemini 缓存对象', exact=False)).to_be_visible()
                        await expect(page.get_by_role('combobox', name='缓存控制', exact=True)).to_have_value('runtime')
                    await select('full agent 模型与生成')
                    await page.get_by_label('接口方式', exact=True).first.select_option('chat_completions')
                    await saved()
                    await select('full agent 模型与生成')
                    await choose_model(page.get_by_role('button', name='模型名称', exact=True), original_model)
                    await saved()
                    # Recipe metadata auto-saves; custom text commits independently.
                    await select('half agent 上下文编排')
                    # A saved move keeps the selected block and its exact array position.
                    blocks = (await read())['half_agent']['context_recipe']['blocks']
                    original_state_at = next(i for i,b in enumerate(blocks) if b['source'] == 'state')
                    await page.locator('.settings-block-list').get_by_role('button').filter(has_text='· 当前状态与变量').click()
                    await page.get_by_role('button', name='上移', exact=True).click()
                    await expect(page.get_by_role('combobox', name='内容来源', exact=True)).to_have_value('state')
                    await saved()
                    blocks = (await read())['half_agent']['context_recipe']['blocks']
                    assert next(i for i,b in enumerate(blocks) if b['source'] == 'state') == original_state_at - 1
                    await page.get_by_role('button', name='撤回本页修改', exact=True).click()
                    await saved()
                    await page.locator('.settings-block-list').get_by_role('button').filter(has_text='· 角色设定（合并）').click()
                    await page.get_by_role('button', name='拆分角色设定为独立块', exact=True).click()
                    await expect(page.get_by_role('combobox', name='内容来源', exact=True)).to_have_value('system_prompt')
                    await saved()
                    split = (await read())['half_agent']['context_recipe']['blocks'][2:6]
                    assert [b['source'] for b in split] == ['system_prompt', 'description', 'personality', 'scenario']
                    assert all(b['depth'] == 2 and b['priority'] == 90 for b in split)
                    await page.get_by_role('button', name='撤回本页修改', exact=True).click()
                    await saved()
                    await page.get_by_role('button',name='添加内容块',exact=True).click()
                    await saved()
                    # Select custom content by its label, independently of array length.
                    await page.get_by_role('button').filter(has_text='· 自定义文本').last.click()
                    await page.get_by_role('textbox',name='自定义内容',exact=True).fill('独立保存编排文本')
                    assert next(b for b in (await read())['half_agent']['context_recipe']['blocks'] if b['source'] == 'custom')['content'] == ''
                    await page.get_by_role('button',name='保存此文本：自定义内容',exact=True).click()
                    await expect(page.get_by_role('button',name='保存此文本：自定义内容',exact=True)).to_have_count(0)
                    assert next(b for b in (await read())['half_agent']['context_recipe']['blocks'] if b['source'] == 'custom')['content'] == '独立保存编排文本'
                    await page.get_by_role('button',name='撤回本页修改',exact=True).click()
                    await saved()
                    assert (await read())['half_agent']['context_recipe'] == before['half_agent']['context_recipe']
                    # All pages render without changing modal height or introducing root overflow.
                    dimensions = {}
                    for item in (await read())['pages']:
                        group = {'full_agent':'full agent','half_agent':'half agent','shared':'通用'}[item['scope']]
                        await select(group+' '+item['label'])
                        assert await dialog.evaluate('e=>e.getBoundingClientRect().height') == initial_height
                        dimensions[item['id']] = await page.locator('.settings-page-content').evaluate('e=>({height:e.clientHeight,content:e.scrollHeight})')
                        assert await dialog.evaluate('e=>e.scrollWidth<=e.clientWidth+1')
                    await select('full agent 模型与生成')
                    for width,height,name in [(1920,1080,'desktop'),(2560,1440,'large-desktop'),(1513,1039,'concept-size'),(360,792,'mobile'),(960,540,'zoom-equivalent')]:
                        await page.set_viewport_size({'width':width,'height':height})
                        await page.screenshot(path=str(output/(name+'.png')))
                        assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth && document.documentElement.scrollHeight<=innerHeight')
                        assert await dialog.evaluate('e=>e.scrollWidth<=e.clientWidth+1')
                        if width >= 1513:
                            assert await page.get_by_label('输出上限（tokens）',exact=True).evaluate('e=>{const r=e.getBoundingClientRect();return r.top>=0&&r.bottom<innerHeight}')
                        if width == 360:
                            assert await page.evaluate("document.querySelector('.settings-header').getBoundingClientRect().bottom <= document.querySelector('.settings-mobile-nav').getBoundingClientRect().top + 1")
                            await page.get_by_role('combobox', name='设置页面', exact=True).select_option('full_agent.prompts')
                            await expect(page.get_by_role('textbox',name='系统提示词',exact=True)).to_be_visible()
                            await page.screenshot(path=str(output/'mobile-prompt.png'))
                            await page.get_by_role('combobox', name='设置页面', exact=True).select_option('full_agent.model')
                    assert not errors, errors
                    (output/'after-dimensions.json').write_text(json.dumps(dimensions,ensure_ascii=False,indent=2),encoding='utf-8')
                    print('Settings isolation, in-flight edits, explicit text saves, close guards, undo, search and five viewports passed.')
                except Exception:
                    await page.screenshot(path=str(output/'failure.png'), full_page=True)
                    (output/'failure-aria.txt').write_text(await page.locator('body').aria_snapshot(), encoding='utf-8')
                    raise
                finally:
                    await browser.close()
        finally:
            server.should_exit = True
            await asyncio.to_thread(thread.join, 10)


if __name__ == '__main__':
    asyncio.run(main())
