"""Check visible lore records and request order against isolated local data."""

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
from tavern.storage.store import dump, uid


async def main():
    screenshots = Path(".cache/lore-records-ui")
    screenshots.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as root:
        app = create_app(root)
        store = app.state.store
        card = store.import_card(json.dumps({
            "name": "注入位置检查", "first_mes": "开场白：“左引号与右引号。”\n\n“怎么说呢……”", "description": "角色设定",
            "mes_example": "示例台词", "character_book": {"entries": [
                {"id": "constant", "constant": True, "content": "常驻内容", "position": 0},
                {"id": "first", "keys": ["灯塔"], "content": "灯塔资料"},
                {"id": "later", "keys": ["门"], "content": "门的资料"},
            ]},
        }).encode(), "card.json")
        session = store.new_session(SessionCreate(card_id=card["id"]))
        greeting = session["head"]
        first = {"id": "first", "anchor_id": greeting, "title": "灯塔资料",
                 "content": "完整灯塔资料\n<script>不会运行</script>", "source_ids": [greeting]}
        later_user = uid()
        later = {"id": "later", "anchor_id": later_user, "title": "门的资料",
                 "content": "完整门的资料", "source_ids": [later_user]}
        with store.connection() as db:
            parent = greeting
            for mid, role, raw, records in [
                (uid(), "user", "灯塔", [first]), (uid(), "assistant", "第一轮回复", [first]),
                (later_user, "user", "门", [first, later]),
                (uid(), "assistant", "第二轮回复", [first, later]),
            ]:
                snapshot = session["snapshot"] | {"lore_injections": records,
                                                   "lore": {r["id"]: {"present": True} for r in records}}
                db.execute("INSERT INTO messages(id,session,parent,role,raw,model,display,snapshot,created) VALUES (?,?,?,?,?,?,?,?,?)",
                           (mid, session["id"], parent, role, raw, raw, raw, dump(snapshot), 1))
                parent = mid
            db.execute("UPDATE sessions SET head=? WHERE id=?", (parent, session["id"]))
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        address = f"http://127.0.0.1:{sock.getsockname()[1]}"
        server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
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
                    page = await browser.new_page(viewport={"width": 1440, "height": 1000})
                    errors = []
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    await page.goto(address)
                    await page.evaluate("id=>localStorage.setItem('tavern-session',id)", session["id"])
                    await page.reload()
                    records = page.locator('.lore-injection-records')
                    await expect(records).to_have_count(2)
                    for anchor in [greeting, later_user]:
                        record = page.locator(f'.lore-injection-records[data-anchor-id="{anchor}"]')
                        assert await record.evaluate("e=>e.nextElementSibling.dataset.messageId") == anchor
                    first_record = records.first
                    await first_record.locator(':scope > summary').click()
                    await first_record.locator('.lore-injection-item > summary').click()
                    await expect(first_record.locator('pre')).to_have_text(first["content"])
                    assert await first_record.locator('script').count() == 0
                    await expect(page.locator('.messages')).not_to_contain_text('常驻内容')
                    await page.screenshot(path=str(screenshots / 'dialogue-desktop.png'))
                    await page.get_by_role('button', name='◫ 上下文', exact=True).click()
                    dialog = page.get_by_role('dialog')
                    await expect(dialog.get_by_role('heading', name='上下文预览', exact=True)).to_be_visible()
                    await expect(dialog.locator('.context-group').first).to_be_visible()
                    sources = await dialog.locator('.context-group').evaluate_all(
                        "nodes=>nodes.map(e=>e.querySelector('summary > strong').textContent)")
                    assert '顶部常驻世界书' in sources
                    first_rolling = sources.index('中途注入世界书')
                    assert sources[first_rolling - 1] == '示例'
                    assert sources[first_rolling + 1] == '历史'
                    await page.screenshot(path=str(screenshots / 'preview-desktop.png'))
                    await dialog.get_by_role('button', name='关闭', exact=True).click()
                    await page.get_by_role('button', name='折叠左侧栏', exact=True).click()
                    await page.get_by_role('button', name='折叠右侧栏', exact=True).click()
                    await page.set_viewport_size({"width": 390, "height": 844})
                    await page.screenshot(path=str(screenshots / 'dialogue-mobile.png'))
                    assert await records.evaluate_all('nodes=>nodes.every(e=>e.scrollWidth<=e.clientWidth+1)')
                    await page.reload()
                    await expect(records).to_have_count(2)
                    for width, height, zoom in [(1440, 900, 1), (1280, 720, 1), (390, 844, 1), (720, 450, 2)]:
                        await page.set_viewport_size({"width": width, "height": height})
                        # A half-size CSS viewport is equivalent to 200% browser zoom for layout.
                        first_record = records.first
                        await first_record.locator(':scope > summary').click()
                        remove = first_record.get_by_role('button', name='移除注入：灯塔资料')
                        await remove.click()
                        confirm = page.get_by_role('dialog', name='确认移除世界书注入')
                        await expect(confirm).to_be_visible()
                        box = await confirm.bounding_box()
                        assert box and box['x'] >= 0 and box['y'] >= 0
                        assert box['x'] + box['width'] <= width + 1
                        assert box['y'] + box['height'] <= height + 1
                        await page.screenshot(path=str(screenshots / f'confirm-{width}-{zoom}.png'))
                        await confirm.get_by_role('button', name='取消', exact=True).click()
                        await expect(records).to_have_count(2)
                        await first_record.locator(':scope > summary').click()
                    await page.set_viewport_size({"width": 1440, "height": 900})
                    composer = page.locator('.composer textarea')
                    await composer.fill('“怎么说呢……”')
                    await page.evaluate('document.fonts.ready')
                    assert await composer.input_value() == '“怎么说呢……”'
                    assert 'Tavern Quote Marks' in await composer.evaluate('e=>getComputedStyle(e).fontFamily')
                    assert await page.locator('.prose').first.text_content() == '开场白：“左引号与右引号。”\n“怎么说呢……”\n'
                    await page.screenshot(path=str(screenshots / 'quote-marks.png'))
                    await records.first.locator(':scope > summary').click()
                    await records.first.get_by_role('button', name='移除注入：灯塔资料').click()
                    await page.get_by_role('dialog', name='确认移除世界书注入').get_by_role('button', name='确认移除', exact=True).click()
                    await expect(records).to_have_count(1)
                    await page.reload()
                    await expect(records).to_have_count(1)
                    snapshot = store.session(session['id'])['snapshot']
                    assert snapshot['lore']['first'] == {'present': True}
                    assert snapshot['lore_injections'][0] == {'id': 'first', 'anchor_id': greeting, 'manually_removed': True}
                    assert not errors, errors
                    print('Lore records, anchors, preview order, reload and mobile layout passed.')
                finally:
                    await browser.close()
        finally:
            server.should_exit = True
            await asyncio.to_thread(thread.join, 10)


if __name__ == "__main__":
    asyncio.run(main())
