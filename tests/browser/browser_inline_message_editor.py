"""Real pointer gestures, autosizing, save decisions and responsive edit layouts."""
import asyncio
import json
import socket
import tempfile
from pathlib import Path
from threading import Thread

import uvicorn
from playwright.async_api import async_playwright, expect

from tavern.application.api import create_app
from tavern.conversation.message_editing import NOTIFICATIONS
from tests.browser.browser_history_navigation import seed


async def hold(page, button):
    await button.scroll_into_view_if_needed()
    box = await button.bounding_box()
    await page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    await page.mouse.down()
    await expect(page.locator('.edit-choice-menu:popover-open')).to_be_visible()


async def release_on(page, option):
    box = await option.bounding_box()
    await page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2, steps=8)
    await expect(option).to_have_class("selected")
    await page.mouse.up()


async def main():
    output = Path('.cache/message-editing')
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='inline-edit-test-') as root:
        app = create_app(root)
        store = app.state.store
        card = store.import_card(json.dumps({"name": "消息编辑验收", "first_mes": "晚风吹过港口。"}, ensure_ascii=False).encode(), 'card.json')
        cases = [(mode, *seed(store, card["id"], mode, 2)) for mode in ('half_agent', 'full_agent')]
        original = "灯塔的光掠过海面，照亮码头边的石阶。\n\n“明天我们再来这里。”她轻声说。\n\n你收起地图，望向远处即将靠岸的船。"
        for _, _, messages, _ in cases:
            with store.connection() as db:
                db.execute('UPDATE messages SET raw=?,model=?,display=? WHERE id=?',
                           (original, original, original, messages[-1]['id']))
        sock = socket.socket()
        sock.bind(('127.0.0.1', 0))
        address = f'http://127.0.0.1:{sock.getsockname()[1]}'
        server = uvicorn.Server(uvicorn.Config(app, log_level='error'))
        thread = Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
        thread.start()
        report = []
        try:
            while not server.started:
                await asyncio.sleep(.05)
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch()
                context = await browser.new_context(has_touch=True)
                page = await context.new_page()
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                await page.goto(address)
                for mode, sid, messages, _ in cases:
                    mid = messages[-1]['id']
                    await page.evaluate('sid => localStorage.setItem("tavern-session",sid)', sid)
                    await page.set_viewport_size({"width": 1920, "height": 1080})
                    await page.reload()
                    message = page.locator(f'[data-message-id="{mid}"]')
                    button = message.get_by_role('button', name='编辑', exact=True)
                    editor = message.get_by_role('textbox', name='编辑消息原文')
                    await expect(button).to_be_visible()
                    reading = await message.locator('.prose').evaluate('e => {const s=getComputedStyle(e),r=e.getBoundingClientRect();return {x:r.x,y:r.y,width:r.width,font:s.font,color:s.color}}')
                    await page.screenshot(path=str(output / f'{mode}-reading-1920.png'))
                    await button.click()
                    await expect(editor).to_be_focused()
                    await page.wait_for_function('p => Math.abs(document.querySelector(`[data-message-id="${p.mid}"] textarea`).getBoundingClientRect().y-p.y)<1', arg={"mid":mid,"y":reading['y']})
                    editing = await editor.evaluate('e => {const s=getComputedStyle(e),r=e.getBoundingClientRect();return {x:r.x,y:r.y,width:r.width,font:s.font,color:s.color,background:s.backgroundColor,height:e.clientHeight,scrollHeight:e.scrollHeight}}')
                    await page.screenshot(path=str(output / f'{mode}-editor-baseline-1920.png'))
                    assert abs(reading['x'] - editing['x']) < 1
                    assert abs(reading['y'] - editing['y']) < 1
                    assert reading['width'] == editing['width'] and reading['font'] == editing['font'] and reading['color'] == editing['color']
                    assert editing['background'] == 'rgba(0, 0, 0, 0)'
                    await page.screenshot(path=str(output / f'{mode}-editor-1920.png'))
                    short_height = editing['height']
                    await editor.fill('长段落与中文标点，保持完整内容。\n\n' * 40)
                    await expect(editor).to_have_value('长段落与中文标点，保持完整内容。\n\n' * 40)
                    long_height = await editor.evaluate('e => e.clientHeight')
                    assert long_height > short_height * 5
                    assert await editor.evaluate('e => e.scrollHeight <= e.clientHeight + 1')
                    await editor.fill('短文本')
                    await page.wait_for_function('document.querySelector(".inline-message-editor textarea").clientHeight < 60')
                    await editor.press('Control+Enter')
                    await expect(page.get_by_text('这次修改要提醒 AI 吗？', exact=True)).to_be_visible()
                    assert store.session(sid)['messages'][-1]['raw'] == original
                    await page.get_by_role('button', name='提醒 AI', exact=True).press('Escape')
                    await expect(editor).to_have_value('短文本')
                    # IME must not submit or open the save question.
                    await editor.dispatch_event('keydown', {"key":"Enter", "ctrlKey":True, "isComposing":True})
                    await expect(page.locator('.edit-save-question:popover-open')).to_have_count(0)
                    await message.get_by_role('button', name='保存修改', exact=True).click()
                    await page.get_by_role('button', name='不提醒 AI', exact=True).click()
                    await expect(editor).to_have_count(0)
                    assert NOTIFICATIONS not in store.session(sid)['snapshot']

                    for notify in (True, False):
                        await hold(page, button)
                        await page.screenshot(path=str(output / f'{mode}-hold-{notify}.png'))
                        option = page.get_by_role('menuitem', name='修改后，提醒 AI' if notify else '不提醒 AI', exact=True)
                        await release_on(page, option)
                        await expect(editor).to_be_focused()
                        await editor.fill(f'拖动选择 {notify}')
                        await message.get_by_role('button', name='保存修改', exact=True).click()
                        await expect(editor).to_have_count(0)
                        await expect(page.locator('.edit-save-question:popover-open')).to_have_count(0)
                        assert (mid in store.session(sid)['snapshot'].get(NOTIFICATIONS, [])) is notify
                    # Outside release cancels; release over the trigger retains the menu.
                    await hold(page, button)
                    await page.mouse.move(8, 8)
                    await page.mouse.up()
                    await expect(editor).to_have_count(0)
                    await expect(page.locator('.edit-choice-menu:popover-open')).to_have_count(0)
                    await hold(page, button)
                    await page.mouse.up()
                    await expect(page.locator('.edit-choice-menu:popover-open')).to_be_visible()
                    await page.get_by_role('menuitem', name='不提醒 AI', exact=True).click()
                    await expect(editor).to_be_focused()
                    # Unchanged save neither asks nor changes revision.
                    revision = store.session(sid)['revision']
                    await message.get_by_role('button', name='保存修改', exact=True).click()
                    await expect(editor).to_have_count(0)
                    assert store.session(sid)['revision'] == revision
                    await button.press('Shift+F10')
                    await expect(page.get_by_role('menuitem', name='修改后，提醒 AI', exact=True)).to_be_focused()
                    await page.keyboard.press('ArrowDown')
                    await page.keyboard.press('Enter')
                    await expect(editor).to_be_focused()
                    await editor.press('Escape')

                    # A failed save keeps the draft/choice and blocks duplicate requests.
                    await button.click()
                    await editor.fill('网络失败仍保留原文草稿')
                    edit_url = f'**/api/sessions/{sid}/messages/{mid}'
                    requests = []
                    async def fail(route):
                        requests.append(route.request.post_data_json)
                        await asyncio.sleep(.15)
                        await route.fulfill(status=500, content_type='application/json', body='{"detail":"fixture save failed"}')
                    await page.route(edit_url, fail)
                    await message.get_by_role('button', name='保存修改', exact=True).click()
                    await page.get_by_role('button', name='提醒 AI', exact=True).click()
                    await expect(message.get_by_role('button', name='保存中…', exact=True)).to_be_disabled()
                    await message.get_by_role('button', name='保存中…', exact=True).dispatch_event('click')
                    await expect(page.get_by_text('fixture save failed', exact=True)).to_be_visible()
                    assert len(requests) == 1 and requests[0]['notify_ai'] is True
                    await expect(editor).to_have_value('网络失败仍保留原文草稿')
                    await page.unroute(edit_url, fail)
                    await message.get_by_role('button', name='保存修改', exact=True).click()
                    await expect(editor).to_have_count(0)
                    assert mid in store.session(sid)['snapshot'][NOTIFICATIONS]

                    for width, height in ((2560,1440), (1920,1080), (360,792), (960,540)):
                        await page.set_viewport_size({"width":width,"height":height})
                        await page.reload()
                        await expect(button).to_be_visible()
                        await button.click()
                        await editor.fill(original)
                        await expect(editor).to_be_focused()
                        await page.screenshot(path=str(output / f'{mode}-editor-{width}.png'))
                        assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                        assert await editor.evaluate('e => e.scrollHeight <= e.clientHeight + 1')
                        await message.get_by_role('button', name='保存修改', exact=True).click()
                        popup = page.locator('.edit-save-question:popover-open')
                        await expect(popup).to_be_visible()
                        box = await popup.bounding_box()
                        assert box['x'] >= 7 and box['x'] + box['width'] <= width - 7
                        assert box['y'] >= 7 and box['y'] + box['height'] <= height - 7
                        await page.screenshot(path=str(output / f'{mode}-question-{width}.png'))
                        await popup.get_by_role('button',name='不提醒 AI',exact=True).press('Escape')
                        await editor.press('Escape')
                        report.append({"mode":mode,"viewport":[width,height],"overflow":False})
                    # Actual touch hold/release on the trigger, then tap an option.
                    await page.set_viewport_size({"width":360,"height":792})
                    await page.reload()
                    await expect(button).to_be_visible()
                    await button.scroll_into_view_if_needed()
                    box = await button.bounding_box()
                    cdp = await context.new_cdp_session(page)
                    point = {"x":box['x']+box['width']/2,"y":box['y']+box['height']/2}
                    assert await page.evaluate('p => !!document.elementFromPoint(p.x,p.y)?.closest(".message-edit-trigger")', point)
                    await cdp.send('Input.dispatchTouchEvent', {"type":"touchStart","touchPoints":[point]})
                    await expect(page.locator('.edit-choice-menu:popover-open')).to_be_visible()
                    await cdp.send('Input.dispatchTouchEvent', {"type":"touchEnd","touchPoints":[]})
                    await page.get_by_role('menuitem',name='不提醒 AI',exact=True).tap()
                    await expect(editor).to_be_focused()
                    await editor.press('Escape')
                    await cdp.detach()

                    # Font preferences update the live editor without losing text.
                    await page.set_viewport_size({"width":1920,"height":1080})
                    await page.reload()
                    await button.click()
                    await editor.fill(original)
                    previous_height = await editor.evaluate('e => e.clientHeight')
                    await page.get_by_role('button', name='Aa 阅读排版', exact=True).click()
                    await page.get_by_role('spinbutton', name='正文字号', exact=True).fill('24')
                    await page.get_by_role('spinbutton', name='行距', exact=True).fill('2.4')
                    await page.get_by_role('spinbutton', name='行距', exact=True).press('Tab')
                    await page.get_by_role('dialog').get_by_role('button', name='关闭', exact=True).click()
                    await page.wait_for_function('document.querySelector(".inline-message-editor textarea").clientHeight > 240')
                    assert await editor.evaluate('e => e.clientHeight') > previous_height
                    assert await editor.evaluate('e => e.scrollHeight <= e.clientHeight + 1')
                    await expect(editor).to_have_value(original)
                    await editor.press('Escape')

                    # The history virtualizer must measure a growing edit and keep it mounted.
                    large_sid, large_messages, _ = seed(store, card['id'], mode, 120)
                    await page.evaluate('sid=>localStorage.setItem("tavern-session",sid)', large_sid)
                    await page.reload()
                    await expect(page.locator('.story-history')).to_have_attribute('data-virtual','true')
                    last = page.locator(f'[data-message-id="{large_messages[-1]["id"]}"]')
                    await last.get_by_role('button', name='编辑', exact=True).click()
                    large_editor = last.get_by_role('textbox', name='编辑消息原文')
                    old_height = await page.locator('.story-history').evaluate('e=>e.getBoundingClientRect().height')
                    await large_editor.fill('完整长段落\n' * 200)
                    await page.wait_for_function('document.querySelector(".inline-message-editor textarea").clientHeight > 10000')
                    await page.wait_for_function('before=>document.querySelector(".story-history").getBoundingClientRect().height>before+5000', arg=old_height)
                    await expect(large_editor).to_have_value('完整长段落\n' * 200)
                    await large_editor.press('Escape')
                    # Reset only the test browser preferences for the next mode.
                    await page.evaluate('localStorage.removeItem("tavern-layout")')
                    print(f'{mode}: autosizing, mouse/touch hold, save questions, retry, keyboard and responsive layouts passed', flush=True)
                assert not errors, errors
                await browser.close()
        finally:
            server.should_exit = True
            thread.join(timeout=10)
        (output / 'inline-report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')


if __name__ == '__main__':
    asyncio.run(main())
