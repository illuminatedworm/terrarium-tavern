"""Ordinary API settings and composer follow the same native thinking options."""
import asyncio
import socket
import tempfile
from pathlib import Path
from threading import Thread

import uvicorn
from playwright.async_api import async_playwright, expect

from tavern.application.api import create_app
from tavern.core.models import SessionCreate
from tests.browser.select_menu import choose_menu, choose_model, menu_option, menu_popup


async def main():
    output = Path('.agents/.local/reasoning-validation')
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='tavern-reasoning-') as root:
        app = create_app(root)
        store = app.state.store
        settings = store.settings_document()
        settings.full_agent.model.model = 'fixture-openai'
        settings.half_agent.foreground.model = 'claude-opus-4-6'
        settings.half_agent.foreground.api_mode = 'anthropic'
        settings.half_agent.background.model = 'gemini-2.5-flash'
        settings.half_agent.background.api_mode = 'gemini'
        settings.shared.card_editor.model = 'gemini-3.8-flash-high'
        store.save_settings(settings)
        card = store.import_card('{"name":"思考档位验证","first_mes":"开场。"}'.encode(), 'card.json')
        session = store.new_session(SessionCreate(card_id=card['id'], agent_mode='full_agent'))
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
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch()
                try:
                    page = await browser.new_page(viewport={'width': 1920, 'height': 1080})
                    errors = []
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    await page.goto(address)
                    await page.evaluate("id=>localStorage.setItem('tavern-session',id)", session['id'])
                    await page.reload()

                    async def tiers(picker, values):
                        popup = await menu_popup(picker)
                        options = popup.locator('[role=option]:not([aria-disabled=true])')
                        await expect(options).to_have_count(len(values))
                        assert await options.evaluate_all('items=>items.map(item=>item.dataset.value)') == values
                        await popup.press('Escape')

                    async def saved():
                        await expect(page.locator('.settings-save-status')).to_contain_text('已保存')

                    async def select(label):
                        button = page.get_by_role('button', name=label, exact=True)
                        if page.viewport_size['width'] > 760:
                            await button.click()
                        else:
                            group, target = {
                                'full agent 模型与生成': ('full agent', 'full_agent.model'),
                                'half agent 模型与生成': ('half agent', 'half_agent.model'),
                                '通用 角色卡编辑 agent': ('通用', 'shared.editor'),
                            }[label]
                            await page.locator('.settings-mode-buttons').get_by_role('button', name=group, exact=True).click()
                            await page.get_by_role('combobox', name='设置页面', exact=True).select_option(target)

                    quick = page.get_by_role('button', name='本次思考强度', exact=True)
                    openai = ['default', 'none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max']
                    await tiers(quick, ['inherit', *openai])
                    await choose_menu(quick, 'xhigh')
                    await page.get_by_role('button', name='设置', exact=True).click()
                    dialog = page.get_by_role('dialog', name='设置', exact=True)
                    await select('full agent 模型与生成')
                    effort = dialog.get_by_role('button', name='思考强度', exact=True)
                    protocol = dialog.get_by_label('接口方式', exact=True)
                    model = dialog.get_by_role('button', name='模型名称', exact=True)
                    await tiers(effort, openai)
                    await choose_menu(effort, 'xhigh')
                    await saved()
                    await protocol.select_option('responses')
                    await tiers(effort, openai)
                    await saved()
                    await protocol.select_option('anthropic')
                    await tiers(effort, ['default', 'none', 'low', 'medium', 'high'])
                    await expect(effort).to_have_attribute('data-value', 'xhigh')
                    await expect(await menu_option(effort, 'xhigh')).to_have_attribute('aria-disabled', 'true')
                    await (await menu_popup(effort)).press('Escape')
                    await saved()
                    assert store.settings_document().full_agent.model.reasoning_effort == 'xhigh'
                    await choose_model(model, 'gemini-3.8-flash-high')
                    await protocol.select_option('gemini')
                    await tiers(effort, ['default', 'low', 'medium', 'high'])
                    await choose_menu(effort, 'high')
                    await saved()
                    await page.screenshot(path=str(output / 'gemini-native.png'))
                    await dialog.get_by_role('button', name='关闭', exact=True).click()
                    await expect(dialog).to_have_count(0)
                    await tiers(quick, ['inherit', 'default', 'low', 'medium', 'high'])
                    await expect(quick).to_have_attribute('data-value', 'xhigh')
                    await expect(await menu_option(quick, 'xhigh')).to_have_attribute('aria-disabled', 'true')
                    await choose_menu(quick, 'medium')
                    await page.reload()
                    await expect(quick).to_have_attribute('data-value', 'medium')
                    await tiers(quick, ['inherit', 'default', 'low', 'medium', 'high'])

                    await page.get_by_role('button', name='设置', exact=True).click()
                    await select('half agent 模型与生成')
                    writer = dialog.get_by_role('region', name='正文生成', exact=True)
                    tracker = dialog.get_by_role('region', name='后台整理', exact=True)
                    await tiers(writer.get_by_role('button', name='思考强度', exact=True), ['default', 'none', 'low', 'medium', 'high', 'max'])
                    await tiers(tracker.get_by_role('button', name='思考强度', exact=True),
                                ['default', 'none', 'minimal', 'low', 'medium', 'high', 'xhigh'])
                    await expect(await menu_option(tracker.get_by_role('button', name='思考强度', exact=True), 'low')).to_contain_text('1024')
                    await (await menu_popup(tracker.get_by_role('button', name='思考强度', exact=True))).press('Escape')
                    await choose_model(writer.get_by_role('button', name='模型名称', exact=True), 'claude-opus-5-5')
                    await tiers(writer.get_by_role('button', name='思考强度', exact=True), ['default', 'low', 'medium', 'high', 'xhigh', 'max'])
                    await saved()
                    await page.screenshot(path=str(output / 'half-agent.png'))
                    await select('通用 角色卡编辑 agent')
                    await tiers(effort, ['default', 'low', 'medium', 'high'])

                    # The additional protocol note stays accessible in the existing fixed modal.
                    for name, viewport in [
                        ('desktop-wide', {'width': 2560, 'height': 1440}),
                        ('desktop', {'width': 1920, 'height': 1080}),
                        ('zoom-200', {'width': 960, 'height': 540}),
                        ('mobile-equivalent', {'width': 360, 'height': 792}),
                    ]:
                        await page.set_viewport_size(viewport)
                        await select('full agent 模型与生成')
                        await tiers(effort, ['default', 'low', 'medium', 'high'])
                        height = await dialog.evaluate('e=>e.getBoundingClientRect().height')
                        await select('通用 角色卡编辑 agent')
                        await tiers(effort, ['default', 'low', 'medium', 'high'])
                        assert abs(await dialog.evaluate('e=>e.getBoundingClientRect().height') - height) < 1
                        assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                        assert await page.evaluate('document.documentElement.scrollHeight <= innerHeight')
                        await effort.scroll_into_view_if_needed()
                        await expect(effort).to_be_visible()
                        await page.screenshot(path=str(output / f'{name}.png'))
                    assert not errors, errors
                    print('Thinking options: four protocols, Full Agent, Writer, Tracker, Card Editor, '
                          'composer, saved incompatible selections, reload and responsive views passed.')
                finally:
                    await browser.close()
        finally:
            server.should_exit = True
            thread.join(timeout=5)


if __name__ == '__main__':
    asyncio.run(main())
