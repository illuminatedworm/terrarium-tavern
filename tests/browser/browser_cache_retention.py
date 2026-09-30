"""Verify cache sidebar save/reload and layouts against isolated local settings."""
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
    output = Path('.cache/cache-retention')
    output.mkdir(parents=True, exist_ok=True)
    cases = [
        ('anthropic', 'claude-opus-5-5', 'runtime', '5m', '1h'),
        ('anthropic', 'claude-opus-5-5', 'cloud', '5m', '1h'),
        ('chat_completions', 'claude-opus-5-5', 'runtime', '5m', '1h'),
        ('gemini', 'gemini-3.8-flash', 'runtime', '5m', '1h'),
        ('chat_completions', 'gemini-3.8-flash', 'runtime', '5m', '15m'),
        ('responses', 'gpt-5.4', 'cloud', 'in_memory', '24h'),
        ('chat_completions', 'gpt-5.4', 'runtime', 'in_memory', '24h'),
        ('responses', 'gpt-6-sol', 'runtime', '30m', None),
        ('responses', 'gpt-5.5', 'cloud', '24h', None),
    ]
    with tempfile.TemporaryDirectory() as data:
        app = create_app(data)
        store = app.state.store
        card = store.import_card(json.dumps({'name': '缓存时效验收', 'first_mes': '开场'}).encode(), 'fixture.json')
        sessions = {mode: store.new_session(SessionCreate(card_id=card['id'], agent_mode=mode))['id']
                    for mode in ('half_agent', 'full_agent')}
        sock = socket.socket()
        sock.bind(('127.0.0.1', 0))
        address = f'http://127.0.0.1:{sock.getsockname()[1]}'
        server = uvicorn.Server(uvicorn.Config(app, log_level='error'))
        thread = Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
        thread.start()
        try:
            while not server.started:
                await asyncio.sleep(.05)
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch()
                page = await browser.new_page(viewport={'width': 1920, 'height': 1080})
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                await page.goto(address)
                for mode, sid in sessions.items():
                    for api, model, control, lowest, target in cases:
                        doc = store.settings_document()
                        doc.shared.providers[0].api_mode = api
                        profile = doc.full_agent.model if mode == 'full_agent' else doc.half_agent.foreground
                        profile.model = model
                        profile.cache.mode, profile.cache.retention = control, 'default'
                        store.save_settings(doc)
                        await page.evaluate('sid => localStorage.setItem("tavern-session", sid)', sid)
                        await page.reload()
                        await page.get_by_role('button', name='缓存', exact=True).click()
                        select = page.get_by_label('缓存时效档位', exact=True)
                        await expect(select).to_have_value(lowest)
                        if target:
                            await select.select_option(target)
                            await page.get_by_role('button', name='保存缓存时效', exact=True).click()
                            await expect(page.get_by_text('缓存时效已保存，从下一次请求生效。', exact=True)).to_be_visible()
                            await expect(page.get_by_role('button', name='保存缓存时效', exact=True)).to_be_disabled()
                            restored = store.settings_document()
                            saved = restored.full_agent.model if mode == 'full_agent' else restored.half_agent.foreground
                            assert saved.cache.retention == target
                            await page.reload()
                            await page.get_by_role('button', name='缓存', exact=True).click()
                            await expect(select).to_have_value(target)
                        if api == 'anthropic' and control == 'runtime':
                            for width, height, label in [(2560, 1440, 'large'), (1920, 1080, 'desktop'),
                                                          (360, 792, 'mobile-dpr3'), (960, 540, 'zoom200-equivalent')]:
                                if width < 1200 and await page.get_by_role('button', name='折叠左侧栏', exact=True).is_visible():
                                    await page.get_by_role('button', name='折叠左侧栏', exact=True).click()
                                await page.set_viewport_size({'width': width, 'height': height})
                                await expect(select).to_be_visible()
                                await select.click()
                                await select.press('Escape')
                                box = await select.bounding_box()
                                assert box and box['x'] >= 0 and box['x'] + box['width'] <= width
                                assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                                await page.screenshot(path=str(output / f'{mode}-{label}.png'))
                            await page.set_viewport_size({'width': 1920, 'height': 1080})
                    # Unsupported controls remain readable without a misleading selector.
                    doc = store.settings_document()
                    doc.shared.providers[0].api_mode = 'responses'
                    profile = doc.full_agent.model if mode == 'full_agent' else doc.half_agent.foreground
                    profile.model = 'unknown-alias'
                    profile.cache.mode = 'cloud'
                    store.save_settings(doc)
                    await page.reload()
                    await page.get_by_role('button', name='缓存', exact=True).click()
                    await expect(page.get_by_text('当前接入方式没有可选的缓存时效参数。', exact=True)).to_be_visible()
                    await expect(page.get_by_label('缓存时效档位', exact=True)).to_have_count(0)
                assert errors == [], errors
                await browser.close()
                print('Passed 18 protocol/mode cases, persistence, unsupported controls and 8 layout cases.')
        finally:
            server.should_exit = True
            thread.join(timeout=5)


if __name__ == '__main__':
    asyncio.run(main())
