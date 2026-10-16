"""Real browser QA with isolated SQLite and a local deterministic provider."""
import asyncio
import json
import socket
import tempfile
from pathlib import Path
from threading import Thread
from types import SimpleNamespace

import uvicorn
from playwright.async_api import async_playwright, expect

from tavern.application.api import create_app
from tavern.core.models import SessionCreate
from tavern.memories.memory_records import Record, install_memory, source_hash
from tavern.storage.store import dump
from tests.support.memory_fixtures import memory_output


async def main():
    screenshots = Path('.cache/classified-memory-ui')
    screenshots.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as root:
        app = create_app(root)
        store = app.state.store
        cfg = store.settings()
        cfg.foreground.model = cfg.background.model = 'fixture'
        cfg.providers[0].api_key = 'local'
        store.save_settings(cfg)
        card = store.import_card(json.dumps({'name': '艾琳', 'first_mes': '艾琳把银戒借给你暂时保管。'}).encode(), 'card.json')
        session = store.new_session(SessionCreate(card_id=card['id']))
        snapshot = session['snapshot']
        install_memory(snapshot, {'version': 1, 'records': [Record(id='ring', category='current',
            content='银戒暂由玩家保管，所有者仍为艾琳。', sources=[{'message_id': session['head'],
            'fingerprint': source_hash(session['messages'][0]), 'excerpt': '银戒借给你暂时保管'}]).model_dump()]})
        with store.connection() as db:
            db.execute('UPDATE messages SET snapshot=? WHERE id=?', (dump(snapshot), session['head']))

        class Provider:
            async def chat_complete(self, messages, **kwargs):
                await asyncio.sleep(.6)
                return SimpleNamespace(content=memory_output(messages, '艾琳曾借出银戒。'), usage={})

            async def close(self):
                pass

        app.state.service.memory_provider_factory = Provider
        sock = socket.socket()
        sock.bind(('127.0.0.1', 0))
        address = f'http://127.0.0.1:{sock.getsockname()[1]}'
        server = uvicorn.Server(uvicorn.Config(app, log_level='error'))
        thread = Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
        thread.start()
        try:
            while not server.started:
                await asyncio.sleep(.05)
            async with async_playwright() as p:
                browser = await p.chromium.launch()
                page = await browser.new_page(viewport={'width': 1440, 'height': 900})
                errors = []
                page.on('pageerror', lambda e: errors.append(str(e)))
                await page.goto(address)
                await page.evaluate("id=>localStorage.setItem('tavern-session',id)", session['id'])
                await page.reload()
                await page.get_by_role('button', name='场景', exact=True).click()
                records = page.locator('.scene-memory .classified-memory')
                await expect(records).to_contain_text('银戒暂由玩家保管')
                current_group = records.locator('.memory-group').filter(has_text='当前进展')
                await expect(current_group.locator('.memory-record')).not_to_be_visible()
                await current_group.locator(':scope > summary').click()
                await expect(current_group.locator('.memory-record')).to_be_visible()
                await current_group.locator(':scope > summary').click()
                await expect(current_group.locator('.memory-record')).not_to_be_visible()
                await current_group.locator(':scope > summary').click()
                await current_group.get_by_text('原文依据 · 1 处', exact=True).click()
                await records.get_by_role('button', name='定位原文').click()
                await expect(page.locator('mark.source-sentence-highlight')).to_contain_text('银戒借给你暂时保管')
                await records.get_by_role('button', name='编辑条目').click()
                await records.get_by_label('记忆条目内容', exact=True).fill('银戒借出，约定清晨归还。')
                await records.get_by_role('button', name='保存条目').click()
                await expect(records).to_contain_text('手工修订')
                await expect(records).to_contain_text('约定清晨归还')
                search = records.get_by_role('searchbox', name='搜索记忆条目')
                await search.fill('清晨')
                await expect(records.locator('.memory-record')).to_have_count(1)
                await search.fill('银戒借给你暂时保管')
                await expect(records.locator('.memory-record')).to_have_count(0)
                await expect(records).to_contain_text('没有匹配的记忆条目')
                await search.fill('')
                await records.get_by_role('button', name='添加记忆条目').click()
                await records.get_by_label('记忆条目内容', exact=True).fill('测试新增事项，随后删除。')
                await records.get_by_role('button', name='保存条目').click()
                added = records.locator('.memory-record').filter(has_text='测试新增事项，随后删除。')
                await records.locator('.memory-group').filter(has_text='手工记忆').locator(':scope > summary').click()
                await expect(added).to_be_visible()
                await added.get_by_role('button', name='删除条目').click()
                await expect(added).to_have_count(0)
                await page.get_by_role('button', name='重新生成记忆', exact=True).click()
                dialog = page.get_by_role('dialog')
                await expect(dialog).to_contain_text('预计至少 1 次')
                assert await dialog.locator('.actions').evaluate("e => getComputedStyle(e).gap") == '10px'
                await dialog.get_by_role('button', name='开始重建').click()
                await expect(records).to_contain_text('艾琳曾借出银戒', timeout=15000)
                await expect(records).to_contain_text('约定清晨归还')
                await records.get_by_role('button', name='添加记忆条目').click()
                await records.get_by_label('记忆条目内容', exact=True).fill('待处理事项：' + '长内容检查，' * 350)
                search_box = await search.bounding_box()
                sidebar_box = await page.locator('.right-sidebar').bounding_box()
                assert search_box and sidebar_box
                assert sidebar_box['y'] + sidebar_box['height'] - search_box['y'] - search_box['height'] < 30
                await page.screenshot(path=str(screenshots / 'editor-desktop.png'))
                await records.get_by_role('button', name='取消编辑').click()
                await page.get_by_role('button', name='折叠左侧栏', exact=True).click()
                await page.get_by_role('button', name='折叠右侧栏', exact=True).click()
                for width, height, label in [(1440, 900, 'desktop'), (1280, 720, 'compact'), (390, 844, 'mobile'), (720, 450, 'zoom-200-equivalent')]:
                    await page.set_viewport_size({'width': width, 'height': height})
                    await page.get_by_role('button', name='变量与记忆', exact=True).click()
                    dialog = page.get_by_role('dialog')
                    await expect(dialog.locator('.classified-memory')).to_contain_text('当前进展')
                    await page.screenshot(path=str(screenshots / f'{label}.png'))
                    assert await dialog.evaluate('e=>e.scrollWidth<=e.clientWidth+1')
                    assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth+1 && document.documentElement.scrollHeight<=innerHeight+1')
                    await dialog.get_by_role('button', name='关闭', exact=True).click()
                await page.set_viewport_size({'width': 1280, 'height': 720})
                await page.get_by_role('button', name='展开左侧栏', exact=True).click()
                await page.get_by_role('button', name='设置', exact=True).click()
                dialog = page.get_by_role('dialog', name='设置', exact=True)
                await dialog.get_by_role('button', name='half agent 历史与记忆', exact=True).click()
                output = dialog.get_by_role('spinbutton', name='整理输出上限', exact=False)
                await expect(output).to_have_value('4096')
                await output.fill('3072')
                await expect(dialog.locator('.settings-save-status')).to_contain_text('已保存')
                await dialog.get_by_role('button', name='关闭', exact=True).click()
                await expect(dialog).not_to_be_visible()
                assert store.settings().memory_output_tokens == 3072
                assert store.settings().background.reply_tokens == cfg.background.reply_tokens
                assert not errors, errors
                await browser.close()
                print('Classified memory edit, source links, estimated rebuild, manual protection, 4 viewports passed.')
        finally:
            server.should_exit = True
            thread.join(timeout=10)


if __name__ == '__main__':
    asyncio.run(main())
