import asyncio
import json
import platform
import socket
import tempfile
import time
from pathlib import Path
from threading import Thread

import uvicorn
from playwright.async_api import async_playwright, expect

from tavern.application.api import create_app
from tavern.core.models import SessionCreate
from tavern.full_agent.state import new_state
from tavern.memories.memory import fingerprint
from tavern.storage.store import dump, uid


def seed(store, card, mode, count, interactive=False):
    session = store.new_session(SessionCreate(card_id=card, agent_mode=mode))
    parent = session["head"]
    messages = list(session["messages"])
    inserts = []
    snapshot = {"state": {}, "scopes": {}, "lore": {}, "content": None}
    frame_id = None
    for index in range(count):
        for role in ("user", "assistant"):
            message_id = uid()
            text = f"{role} 第 {index + 1} 轮。" + ("这是导航验收中的可变长度正文。" * (index % 8 + 1))
            if interactive and index == count // 2 and role == "assistant":
                text = '<div><input aria-label="交互草稿" value="初始值"><script>window.boots = (window.boots || 0) + 1;</script></div>'
                frame_id = message_id
            messages.append({"id": message_id, "parent": parent, "role": role, "raw": text, "model": text})
            inserts.append((message_id, session["id"], parent, role, text, text, text, dump(snapshot), index + 1))
            parent = message_id
    covered = max(0, len(messages) - 20)
    snapshot["context_window"] = {"covered_count": covered, "fingerprint": fingerprint(messages[:covered])}
    if mode == 'full_agent':
        snapshot["full_agent"] = new_state()
        snapshot["full_agent"]["window"]["start"] = covered
    with store.connection() as database:
        database.executemany("INSERT INTO messages(id,session,parent,role,raw,model,display,snapshot,created) VALUES (?,?,?,?,?,?,?,?,?)", inserts)
        database.execute("UPDATE sessions SET head=?,title=? WHERE id=?", (parent, f"导航 {mode} {count}", session["id"]))
        database.execute("UPDATE messages SET snapshot=? WHERE id=?", (dump(snapshot), parent))
    return session["id"], messages, frame_id


async def main():
    output = Path('.cache/history-navigation')
    output.mkdir(parents=True, exist_ok=True)
    report = {"platform": platform.platform(), "cases": []}
    with tempfile.TemporaryDirectory() as data:
        app = create_app(data)
        store = app.state.store
        card = store.import_card(Path('samples/雾港来信.json').read_bytes(), 'card.json')
        with store.connection() as database:
            database.execute("UPDATE cards SET permission=1 WHERE id=?", (card['id'],))
        cases = [(mode, size, *seed(store, card['id'], mode, size, size == 5000))
                 for mode in ('half_agent', 'full_agent') for size in (8, 5000)]
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
                report['browser'] = browser.version
                page = await browser.new_page(viewport={"width": 1440, "height": 900})
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                await page.goto(address)
                for mode, size, sid, messages, frame_id in cases:
                    await page.evaluate('(sid) => localStorage.setItem("tavern-session", sid)', sid)
                    started = time.perf_counter()
                    await page.reload()
                    history = page.locator('.story-history')
                    await expect(history).to_have_attribute('data-virtual', 'true' if size > 60 else 'false', timeout=30000)
                    rail = page.get_by_role('slider', name='对话历史导航')
                    await expect(rail).to_be_visible()
                    await expect(page.locator(f'[data-message-id="{messages[-1]["id"]}"]')).to_be_visible(timeout=30000)
                    load_ms = round((time.perf_counter() - started) * 1000)
                    await rail.focus()
                    await rail.press('Home')
                    await expect(page.locator(f'[data-message-id="{messages[0]["id"]}"]')).to_be_visible()
                    if not await page.get_by_role('button', name='缓存', exact=True).is_visible():
                        await page.get_by_role('button', name='展开右侧栏', exact=True).click()
                    await page.get_by_role('button', name='缓存', exact=True).click()
                    panel = page.get_by_role('region', name='缓存断点控制')
                    await panel.get_by_label('每新增几轮跟进').fill('3')
                    await panel.get_by_label('倒数第几个 AI 回复').fill('2')
                    await panel.get_by_role('button', name='保存跟进策略').click()
                    await expect(panel).to_contain_text('再新增 3 轮后跟进')
                    await panel.get_by_role('button', name='定位断点').click()
                    target = page.locator(f'[data-message-id="{messages[-3]["id"]}"]')
                    await expect(target).to_be_visible(timeout=30000)
                    await target.hover()
                    await target.locator(':scope > .cache-breakpoint-dot').click()
                    await expect(panel.get_by_label('缓存跟进方式')).to_have_value('false')
                    await expect(panel).to_contain_text('位置已手动固定')
                    await panel.get_by_label('缓存跟进方式').select_option('true')
                    await panel.get_by_label('每新增几轮跟进').fill('0')
                    await expect(panel.get_by_role('alert')).to_contain_text('整数')
                    await expect(panel.get_by_role('button', name='保存跟进策略')).to_be_disabled()
                    await panel.get_by_label('每新增几轮跟进').fill('3')
                    await panel.get_by_role('button', name='保存跟进策略').click()
                    await expect(panel.get_by_role('button', name='保存跟进策略')).to_be_disabled()
                    await rail.focus()
                    await rail.press('End')
                    await expect(page.locator(f'[data-message-id="{messages[-1]["id"]}"]')).to_be_visible()
                    mounted = await page.locator('.history-row').count()
                    if size == 5000:
                        assert mounted < 80, mounted
                        box = await rail.bounding_box()
                        middle_y = box['y'] + box['height'] * ((size // 2 + 1) / size)
                        await page.mouse.move(box['x'] + box['width'] / 2, middle_y)
                        await expect(page.locator('.rail-preview')).to_contain_text('第 2501 轮')
                        await page.mouse.down()
                        await page.mouse.up()
                        frame = page.frame_locator(f'[data-message-id="{frame_id}"] iframe')
                        await frame.get_by_label('交互草稿').fill('必须保留的草稿')
                        await page.locator(f'[data-message-id="{frame_id}"] iframe').evaluate('(node) => window.testFrame = node')
                        await rail.focus()
                        await rail.press('End')
                        await expect(page.locator(f'[data-message-id="{messages[-1]["id"]}"]')).to_be_visible()
                        await page.mouse.click(box['x'] + box['width'] / 2, middle_y)
                        await expect(frame.get_by_label('交互草稿')).to_have_value('必须保留的草稿')
                        assert await page.locator(f'[data-message-id="{frame_id}"] iframe').evaluate('(node) => node === window.testFrame')
                        assert await frame.get_by_label('交互草稿').evaluate('() => window.boots') == 1
                        started = time.perf_counter()
                        for fraction in (.1, .8, .2, .9, .3, .7):
                            await page.mouse.click(box['x'] + box['width'] / 2, box['y'] + box['height'] * fraction)
                            await page.wait_for_timeout(80)
                            assert await page.locator('.history-row').count() < 80
                        jump_ms = round((time.perf_counter() - started) * 1000)
                        mounted = await page.locator('.history-row').count()
                    else:
                        jump_ms = None
                        assert mounted == len(messages)
                    await page.screenshot(path=str(output / f'{mode}-{size}.png'))
                    await page.get_by_role('button', name='折叠左侧栏', exact=True).click()
                    await page.get_by_role('button', name='折叠右侧栏', exact=True).click()
                    await page.set_viewport_size({"width": 390, "height": 844})
                    assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                    await expect(rail).to_be_visible()
                    await rail.focus()
                    await rail.press('Home')
                    await expect(page.locator(f'[data-message-id="{messages[0]["id"]}"]')).to_be_visible()
                    await page.screenshot(path=str(output / f'{mode}-{size}-mobile.png'))
                    await page.get_by_role('button', name='展开右侧栏', exact=True).click()
                    await expect(panel).to_be_visible()
                    await page.screenshot(path=str(output / f'{mode}-{size}-mobile-cache.png'))
                    await page.get_by_role('button', name='关闭右侧栏', exact=True).first.click()
                    await page.set_viewport_size({"width": 1440, "height": 900})
                    await page.get_by_role('button', name='展开左侧栏', exact=True).click()
                    if size == 8:
                        for control_mode in ('cloud', 'runtime'):
                            await page.get_by_role('button', name='⚙ 设置', exact=True).click()
                            dialog = page.get_by_role('dialog', name='设置', exact=True)
                            await dialog.get_by_role('button', name=mode.replace('_', ' ') + ' 模型与生成', exact=True).click()
                            profile = dialog.get_by_role('region', name='模型连接' if mode == 'full_agent' else '正文生成', exact=True)
                            await profile.locator('details > summary').click()
                            async with page.expect_response(lambda response: response.request.method == 'PATCH' and response.url.endswith('/settings/' + mode)):
                                await profile.get_by_label('缓存控制', exact=True).select_option(control_mode)
                            await expect(page.locator('.settings-save-status')).to_contain_text('已保存')
                            await dialog.get_by_role('button', name='关闭', exact=True).click()
                            if control_mode == 'cloud':
                                await expect(page.locator('.cache-breakpoint-dot')).to_have_count(0)
                                await page.get_by_role('button', name='展开右侧栏', exact=True).click()
                                await page.get_by_role('button', name='缓存', exact=True).click()
                                await expect(panel).to_be_visible()
                                await expect(panel.get_by_label('每新增几轮跟进')).to_have_count(0)
                                await expect(page.get_by_role('button', name='缓存', exact=True)).to_have_attribute('aria-current', 'page')
                            else:
                                await page.get_by_role('button', name='缓存', exact=True).click()
                                await expect(panel.get_by_label('每新增几轮跟进')).to_have_value('3')
                                await expect(panel.get_by_label('倒数第几个 AI 回复')).to_have_value('2')
                        await rail.focus()
                        await rail.press('Home')
                        await rail.press('ArrowDown')
                        editable = page.locator(f'[data-message-id="{messages[2]["id"]}"]')
                        await editable.get_by_role('button', name='编辑', exact=True).click()
                        await editable.locator('textarea').fill('threshold fixture')
                        await editable.locator('textarea').evaluate("node => { node.value = 'A longer paragraph. '.repeat(10001); node.dispatchEvent(new Event('input', {bubbles: true})); }")
                        async with page.expect_response(lambda response: response.request.method == 'PUT' and '/messages/' in response.url, timeout=30000) as saved_edit:
                            await editable.get_by_role('button', name='保存修改', exact=True).click()
                        saved_response = await saved_edit.value
                        assert saved_response.status == 200, (saved_response.status, (await saved_response.text())[:500])
                        await page.wait_for_function('document.querySelector(".story-history")?.dataset.virtual === "true"')
                        await page.wait_for_function('!document.querySelector(".inline-message-editor")')
                        await editable.get_by_role('button', name='编辑', exact=True).click()
                        await editable.locator('textarea').fill(messages[2]['raw'])
                        await editable.get_by_role('button', name='保存修改', exact=True).click()
                        await expect(editable.locator('textarea')).to_have_count(0)
                        await expect(history).to_have_attribute('data-virtual', 'true')
                    report['cases'].append({"mode": mode, "messages": len(messages), "load_ms": load_ms,
                                            "six_jumps_ms_including_waits": jump_ms, "mounted_rows": mounted})
                assert not errors, errors
                (output / 'results.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
                print(json.dumps(report, ensure_ascii=False))
                await browser.close()
        finally:
            server.should_exit = True
            thread.join(timeout=10)


if __name__ == '__main__':
    asyncio.run(main())
