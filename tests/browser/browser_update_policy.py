"""Saving mode, per-turn authorization and review on responsive real pages."""
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
from tests.backend.test_full_agent import Model


async def main():
    output = Path('.agents/.local/update-policy-ui')
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output) as data:
        app = create_app(data)
        store = app.state.store
        card = store.import_card(json.dumps({"name": "无变量的魔法世界", "description": "原有角色设定",
                                            "first_mes": "你来到魔法学校。"}, ensure_ascii=False).encode(), 'card.json')
        settings = store.settings_document()
        settings.full_agent.model.model = 'fixture'
        settings.full_agent.tools.auto_retrieval = False
        store.save_settings(settings)
        session = store.new_session(SessionCreate(card_id=card['id'], agent_mode='full_agent', channel_origin='standard'))
        app.state.service.full.provider_factory = lambda: Model(
            ('write_note', {'key': 'timeline/autumn', 'content': '明天见面', 'kind': 'plan'}), '她记下了约定。')
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
                page = await browser.new_page(viewport={'width': 1920, 'height': 1080})
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                await page.add_init_script(f"localStorage.setItem('tavern-session', '{session['id']}')")
                await page.goto(address)
                await page.locator('.story').wait_for()
                await page.get_by_role('button', name='设置', exact=True).click()
                dialog = page.get_by_role('dialog', name='设置', exact=True)
                saving = page.get_by_label('默认修改保存方式', exact=True)
                await expect(saving).to_have_value('automatic')
                assert await saving.locator('option').all_text_contents() == ['自动保存', '审核后保存']
                await saving.select_option('review')
                await expect(page.locator('.settings-save-status')).to_contain_text('已保存')
                for label in ('half agent 模型与生成', 'full agent 模型与生成'):
                    await page.get_by_role('button', name=label, exact=True).click()
                    await expect(saving).to_have_value('review' if label.startswith('full') else 'automatic')
                await saving.scroll_into_view_if_needed()
                await page.screenshot(path=str(output / 'global-desktop.png'))
                await dialog.get_by_role('button', name='关闭', exact=True).click()
                await expect(dialog).not_to_be_visible()
                await page.get_by_role('button', name='会话设置', exact=True).click()
                options = page.get_by_role('region', name='会话设置', exact=True)
                await expect(options.get_by_label('修改保存方式', exact=True)).to_have_value('')
                await expect(options).to_contain_text('当前生效：审核后保存')
                assert '状态' not in await options.locator('.policy-control').inner_text()
                await options.get_by_label('修改保存方式', exact=True).select_option('review')
                await options.get_by_role('button', name='保存', exact=True).click()
                await page.screenshot(path=str(output / 'session-desktop.png'))
                await page.get_by_placeholder('说些什么，或描述你的行动…').fill('记住明天的约定')
                await page.get_by_role('button', name='发送', exact=True).click()
                await expect(page.locator('.messages')).to_contain_text('她记下了约定。', timeout=30000)
                await expect(page.get_by_label('审核本轮修改')).to_have_count(0)
                assert store.session(session['id'])['snapshot']['full_agent']['notes']['timeline/autumn']['content'] == '明天见面'
                app.state.service.full.provider_factory = lambda: Model(
                    ('read_character', {'ref': 'description'}),
                    ('revise_content', {'operations': [{'op': 'set_character', 'field': 'description', 'value': '新的魔法教师设定'}],
                                        'comment': '用户要求'}), '她介绍了自己的经历。')
                await page.get_by_label('本轮允许修改设定', exact=True).check()
                await page.get_by_placeholder('说些什么，或描述你的行动…').fill('把角色改为魔法教师')
                await page.get_by_role('button', name='发送', exact=True).click()
                review = page.get_by_label('审核本轮修改')
                await expect(review).to_be_visible(timeout=30000)
                await expect(review).to_contain_text('原有角色设定')
                await expect(review).to_contain_text('新的魔法教师设定')
                await expect(page.get_by_label('本轮允许修改设定', exact=True)).to_be_disabled()
                assert 'model_references' not in await review.inner_text()
                for name, width, height in [('desktop-large', 2560, 1440), ('desktop', 1920, 1080),
                                            ('zoom-200-equivalent', 960, 540)]:
                    if width < 1200:
                        await page.get_by_role('button', name='折叠左侧栏', exact=True).click()
                        await page.get_by_role('button', name='折叠右侧栏', exact=True).click()
                    await page.set_viewport_size({'width': width, 'height': height})
                    await review.scroll_into_view_if_needed()
                    assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                    assert await page.evaluate('document.documentElement.scrollHeight <= innerHeight')
                    await page.screenshot(path=str(output / f'review-{name}.png'))
                    await review.get_by_role('button', name='接受并保存', exact=True).click(trial=True)
                mobile = await browser.new_context(viewport={'width': 360, 'height': 792}, device_scale_factor=3)
                await mobile.add_init_script(f"localStorage.setItem('tavern-session', '{session['id']}')")
                mobile_page = await mobile.new_page()
                mobile_page.on('pageerror', lambda error: errors.append(str(error)))
                await mobile_page.goto(address)
                mobile_review = mobile_page.get_by_label('审核本轮修改')
                await expect(mobile_review).to_be_visible(timeout=30000)
                await mobile_review.scroll_into_view_if_needed()
                assert await mobile_page.evaluate('devicePixelRatio') == 3
                assert await mobile_page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                assert await mobile_page.evaluate('document.documentElement.scrollHeight <= innerHeight')
                await mobile_page.screenshot(path=str(output / 'review-mobile-dpr3.png'))
                await mobile_review.get_by_role('button', name='接受并保存', exact=True).click(trial=True)
                await mobile.close()
                await page.set_viewport_size({'width': 1920, 'height': 1080})
                await page.reload()
                await expect(review).to_be_visible(timeout=30000)
                await review.get_by_role('button', name='接受并保存', exact=True).click()
                await expect(review).to_have_count(0)
                await expect(page.get_by_label('本轮允许修改设定', exact=True)).not_to_be_checked()
                await expect(page.get_by_label('本轮更新变量', exact=True)).not_to_be_checked()
                assert not errors, errors
                await browser.close()
                print('PASS: global/session wording, note-only save, content review/reload, one-turn permission, 4 viewport sizes')
        finally:
            server.should_exit = True
            thread.join(10)


if __name__ == '__main__':
    asyncio.run(main())
