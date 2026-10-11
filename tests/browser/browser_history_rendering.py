"""Long-history rendering guard with disposable data and no remote providers."""
import asyncio
import json
import socket
import tempfile
from threading import Thread

import uvicorn
from playwright.async_api import async_playwright, expect

from tavern.application.api import create_app
from tavern.core.models import SessionCreate
from tavern.memories.memory_records import Record, source_hash
from tavern.storage.store import dump


async def main():
    with tempfile.TemporaryDirectory() as root:
        app = create_app(root)
        store = app.state.store
        cfg = store.settings()
        cfg.retrieval.channels = ['keyword']
        cfg.local_models.reranker_enabled = False
        store.save_settings(cfg)
        card = store.import_card(b'{"name":"History fixture","first_mes":"Old source sentence."}', 'card.json')
        with store.connection() as db:
            db.execute('UPDATE cards SET permission=1 WHERE id=?', (card['id'],))
        session = store.new_session(SessionCreate(card_id=card['id']))
        snapshot = session['snapshot']
        snapshot['memory_document'] = {'version': 1, 'records': [Record(
            category='current', content='A remembered source', sources=[{
                'message_id': session['head'], 'fingerprint': source_hash(session['messages'][0]),
                'excerpt': 'Old source sentence.',
            }]).model_dump()]}
        parent = session['head']
        with store.connection() as db:
            for i in range(1, 300):
                mid = f'fixture-{i}'
                text = f'Message {i}.\n\n' + 'A long paragraph for scrolling. ' * 30
                if i in (200, 260):
                    text = '<div>Isolated panel</div><script>document.body.dataset.ready="yes";</script>'
                db.execute(
                    'INSERT INTO messages(id,session,parent,role,raw,model,display,snapshot,created) '
                    'VALUES (?,?,?,?,?,?,?,?,?)',
                    (mid, session['id'], parent, 'assistant', text, text, text, dump(snapshot), i),
                )
                parent = mid
            db.execute('UPDATE sessions SET head=? WHERE id=?', (parent, session['id']))
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
                    for width, height, scale in [(1440, 900, 1), (1280, 720, 1), (390, 844, 1), (720, 450, 2)]:
                        context = await browser.new_context(
                            viewport={'width': width, 'height': height}, device_scale_factor=scale)
                        await context.add_init_script(
                            f'if (window === window.top) localStorage.setItem("tavern-session", {json.dumps(session["id"])});')
                        page = await context.new_page()
                        errors = []
                        page.on('pageerror', lambda error: errors.append(str(error)))
                        await page.goto(address)
                        rows = page.locator('.messages [data-message-id]')
                        await expect(page.locator('.story-history')).to_have_attribute('data-virtual', 'true')
                        last = page.locator('[data-message-id="fixture-299"]')
                        await expect(last).to_be_visible()
                        assert await rows.count() < 60
                        rail = page.get_by_role('slider', name='对话历史导航')
                        await rail.focus()
                        await rail.press('Home')
                        await expect(page.locator(f'[data-message-id="{session["head"]}"]')).to_be_visible()
                        box = await rail.bounding_box()
                        # Use the visible tick range; the slider's padded edges are not history positions.
                        ticks = rail.locator('.rail-tick:not(.current):not(.cached):not(.hovered)')
                        first_tick, last_tick = await ticks.first.bounding_box(), await ticks.last.bounding_box()
                        top = first_tick['y'] + first_tick['height'] / 2
                        bottom = last_tick['y'] + last_tick['height'] / 2
                        await page.mouse.click(box['x'] + box['width'] / 2, top + (bottom - top) * 200 / 299)
                        await expect(page.locator('[data-message-id="fixture-200"] iframe')).to_be_visible()
                        await rail.focus()
                        await rail.press('End')
                        await expect(last).to_be_visible()
                        await expect(page.locator('.messages iframe')).to_have_count(1)
                        # Activate normally, without the pointer's long-press choice menu.
                        await last.get_by_role('button', name='编辑', exact=True).press('Enter')
                        await last.locator('textarea').fill('Changed paragraph\n' * 20)
                        await rail.focus()
                        await rail.press('Home')
                        await expect(last.locator('textarea')).to_have_count(1)
                        await rail.press('End')
                        await last.get_by_role('button', name='取消', exact=True).click()
                        if width == 1440:
                            await page.get_by_role('button', name='场景', exact=True).click()
                            await page.locator('.memory-group > summary').click()
                            await page.locator('.memory-record details > summary').click()
                            await page.get_by_role('button', name='定位原文', exact=True).click()
                            await expect(page.locator('mark.source-sentence-highlight')).to_have_text('Old source sentence.')
                            assert await rows.count() < 60
                            await page.get_by_role('button', name='跳到最新', exact=True).click()
                            await expect(last).to_be_visible()
                        assert await page.evaluate(
                            'document.documentElement.scrollWidth <= innerWidth && document.documentElement.scrollHeight <= innerHeight')
                        assert not errors, errors
                        await context.close()
                finally:
                    await browser.close()
            assert len(store.session(session['id'])['messages']) == 300
            print('300 messages: bounded virtual rows, retained iframe, pinned edit, old-source navigation; 4 viewports passed.')
        finally:
            server.should_exit = True
            await asyncio.to_thread(thread.join, 10)


if __name__ == '__main__':
    asyncio.run(main())
