"""Independent editor UI acceptance with an isolated store and local tool model."""

import asyncio
import json
import socket
import tempfile
from pathlib import Path
from threading import Thread

import uvicorn
from kohakuterrarium.testing.llm import ScriptedLLM, ScriptEntry
from playwright.async_api import async_playwright, expect

from tavern.application.api import create_app
from tavern.core.models import SessionCreate
from tests.backend.test_card_editor import ToolLLM, fixture


async def main():
    screenshots = Path('.cache/card-editor-ui')
    screenshots.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as root:
        app = create_app(root)
        store = app.state.store
        document = fixture()
        document['first_mes'] += '<initvar>hp: 8</initvar><div class="stat_data">旧变量面板</div>'
        card = store.import_card(json.dumps(document).encode(), 'card.json')
        original = store.new_session(SessionCreate(card_id=card['id']))
        app.state.editor.provider_factory = lambda: ToolLLM([
            ('read_skill', {}),
            ('read_field', {'path': '/description'}),
            ('search_entries', {'query': '变量'}),
            ('set_opening', {'index': 0, 'narrative': '开场一', 'initialization': '<initvar>hp: 8</initvar>'}),
            ('set_opening', {'index': 1, 'narrative': '开场二', 'initialization': '<initvar>hp: 9</initvar>'}),
            ('validate_draft', {}),
        ])
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
                    page = await browser.new_page(viewport={'width': 1440, 'height': 900})
                    errors = []
                    page.on('pageerror', lambda e: errors.append(str(e)))
                    await page.goto(address)
                    await page.get_by_role('tab', name='角色卡', exact=True).click()
                    card_item = page.locator('.card-side-item').first
                    buttons = page.locator('.card-row-actions').first
                    await expect(buttons).to_be_hidden()
                    content_width = "element => element.clientWidth - parseFloat(getComputedStyle(element).paddingLeft) - parseFloat(getComputedStyle(element).paddingRight)"
                    idle_content_width = await card_item.evaluate(content_width)
                    copy_box = await buttons.locator('.card-copy-action').bounding_box()
                    delete_box = await buttons.locator('.card-delete-action').bounding_box()
                    assert abs(copy_box['y'] - delete_box['y']) < 1
                    assert copy_box['height'] == delete_box['height'] and copy_box['width'] == delete_box['width']
                    await card_item.click()
                    await expect(buttons).to_be_visible()
                    focused_content_width = await card_item.evaluate(content_width)
                    assert idle_content_width > focused_content_width + 60
                    await page.get_by_role('button', name='世界书 3', exact=True).click()
                    await expect(buttons).to_be_hidden()
                    blurred_content_width = await card_item.evaluate(content_width)
                    assert blurred_content_width == idle_content_width
                    blurred_background_alpha = await card_item.evaluate(
                        "element => Number(getComputedStyle(element).backgroundColor.split(',').at(-1).replace(')', ''))"
                    )
                    assert blurred_background_alpha == 0
                    await page.get_by_role('searchbox', name='搜索条目').fill('秘密 规则')
                    await expect(page.locator('.lore-editor')).to_have_count(1)
                    await page.get_by_role('button', name='清空筛选').click()
                    await page.get_by_label('条目用途', exact=True).select_option('runtime_init')
                    await expect(page.locator('.lore-editor')).to_have_count(1)
                    await page.get_by_role('button', name='清空筛选').click()
                    original_title = await page.locator('.lore-editor').first.inner_text()
                    await page.locator('.lore-editor').first.click(position={'x': 8, 'y': 80})
                    lore_dialog = page.get_by_role('dialog', name='编辑世界书条目', exact=True)
                    await lore_dialog.get_by_label('条目名称').fill('不应保存的名称')
                    await page.locator('.lore-modal-backdrop').click(position={'x': 4, 'y': 4})
                    await expect(lore_dialog).to_be_visible()
                    await lore_dialog.get_by_role('button', name='取消', exact=True).click()
                    assert await page.locator('.lore-editor').first.inner_text() == original_title
                    await page.get_by_role('button', name='＋ 添加条目', exact=True).click()
                    add_dialog = page.get_by_role('dialog', name='添加世界书条目', exact=True)
                    await add_dialog.get_by_label('条目名称').fill('可删除的新条目')
                    await add_dialog.get_by_role('button', name='确认', exact=True).click()
                    await expect(page.locator('.lore-editor')).to_have_count(4)
                    await page.locator('.lore-editor').filter(has_text='可删除的新条目').click()
                    lore_dialog = page.get_by_role('dialog', name='编辑世界书条目', exact=True)
                    await lore_dialog.get_by_role('button', name='删除条目', exact=True).click()
                    await lore_dialog.get_by_role('button', name='确认删除', exact=True).click()
                    await expect(page.locator('.lore-editor')).to_have_count(3)
                    await page.locator('.editor-tabs').get_by_role('button', name='Agent 编辑', exact=True).click()
                    await expect(page.locator('.editor-page')).to_have_class('editor-page agent-mode')
                    await expect(page.locator('.json-data-tree details[open]')).to_have_count(0)
                    assert await page.locator('.json-data-tree > details').count() > 0
                    await page.screenshot(animations='disabled', path=str(screenshots / 'agent-workspace-empty.png'))
                    await page.get_by_role('button', name='适配本项目工作流', exact=True).click()
                    await expect(page.locator('.agent-conversation')).to_contain_text('草稿修改完成', timeout=20000)
                    await expect(page.locator('.agent-tool-step')).to_have_count(6, timeout=20000)
                    await expect(page.locator('.agent-tool-step[data-status="completed"]')).to_have_count(6, timeout=20000)
                    await page.locator('.agent-tool-step').filter(has_text='读取字段').locator('summary').click()
                    await expect(page.locator('.tool-detail').filter(has_text='/description')).to_be_visible()
                    await page.get_by_role('tab', name='诊断', exact=True).click()
                    await expect(page.locator('#editor-panel-diagnostics')).to_contain_text('静态适配检查通过')
                    assert store.card(card['id'])['document']['first_mes'] == document['first_mes']
                    selector = page.get_by_role('combobox', name='切换编辑对话', exact=True)
                    original_context = await selector.input_value()
                    await page.get_by_role('textbox', name='编辑要求', exact=True).fill('旧对话未发送的输入')
                    await page.get_by_role('button', name='沿用草稿开新对话', exact=True).click()
                    await expect(selector).not_to_have_value(original_context)
                    carried_context = await selector.input_value()
                    await expect(page.locator('.agent-run')).to_have_count(0)
                    await expect(page.get_by_role('textbox', name='编辑要求', exact=True)).to_have_value('')
                    carried = app.state.editor.edits.get(carried_context)
                    assert carried['document']['first_mes'] == '开场一'
                    assert carried['runs'] == []
                    await page.get_by_role('textbox', name='编辑要求', exact=True).fill('沿用草稿对话的输入')
                    await page.get_by_role('button', name='＋ 新建对话', exact=True).click()
                    await expect(selector).not_to_have_value(carried_context)
                    fresh_context = await selector.input_value()
                    fresh = app.state.editor.edits.get(fresh_context)
                    assert fresh['document'] == document and fresh['runs'] == []
                    await expect(page.get_by_role('textbox', name='编辑要求', exact=True)).to_have_value('')
                    await selector.select_option(original_context)
                    await expect(page.locator('.agent-run')).to_have_count(1)
                    await expect(page.get_by_role('textbox', name='编辑要求', exact=True)).to_have_value('旧对话未发送的输入')
                    await selector.select_option(carried_context)
                    await expect(page.locator('.agent-run')).to_have_count(0)
                    await expect(page.get_by_role('textbox', name='编辑要求', exact=True)).to_have_value('沿用草稿对话的输入')
                    await selector.select_option(original_context)
                    await expect(page.locator('.agent-run')).to_have_count(1)

                    await page.get_by_role('tab', name='修改', exact=False).click()
                    changes_disclosure = page.locator('.agent-changes-disclosure')
                    await expect(changes_disclosure.locator('> summary')).to_be_visible()
                    assert await changes_disclosure.get_attribute('open') is None
                    await expect(changes_disclosure.locator('.agent-diff').first).to_be_hidden()
                    save_box = await page.get_by_role('button', name='删除当前对话', exact=True).bounding_box()
                    changes_box = await changes_disclosure.bounding_box()
                    assert save_box['y'] + save_box['height'] <= changes_box['y']
                    await changes_disclosure.locator('> summary').click()
                    await expect(changes_disclosure.locator('.agent-diff').first).to_be_visible()
                    await changes_disclosure.locator('> summary').click()
                    app.state.editor.provider_factory = lambda: ScriptedLLM([
                        ScriptEntry(response='OLD_RUNNING_OUTPUT_旧会话完成', chunk_size=1, delay_per_chunk=.08)])
                    await page.get_by_role('textbox', name='编辑要求', exact=True).fill('旧会话继续运行')
                    await page.get_by_role('button', name='发送编辑要求', exact=False).click()
                    await expect(page.get_by_role('button', name='沿用草稿开新对话', exact=True)).to_be_enabled()
                    running = app.state.editor.edits.get(original_context)['runs'][-1]
                    await selector.select_option(carried_context)
                    await expect(page.locator('.agent-run')).to_have_count(0)
                    for _ in range(200):
                        if app.state.editor.edits.run(running['id'])['status'] not in ('queued', 'running'):
                            break
                        await asyncio.sleep(.05)
                    assert app.state.editor.edits.run(running['id'])['status'] == 'completed'
                    await expect(selector).to_have_value(carried_context)
                    await expect(page.locator('.agent-run')).to_have_count(0)
                    await expect(page.get_by_role('textbox', name='编辑要求', exact=True)).to_have_value('沿用草稿对话的输入')
                    await selector.select_option(original_context)
                    await expect(page.locator('.agent-run')).to_have_count(2)
                    await expect(page.locator('.agent-conversation')).to_contain_text('OLD_RUNNING_OUTPUT')
                    await page.get_by_role('button', name='折叠左侧栏', exact=True).click()
                    right = page.get_by_role('button', name='折叠右侧栏', exact=True)
                    if await right.is_visible():
                        await right.click()
                    for width, height in [(1440, 900), (1280, 720), (390, 844), (720, 450)]:
                        await page.set_viewport_size({'width': width, 'height': height})
                        await expect(page.get_by_role('textbox', name='编辑要求', exact=True)).to_be_visible()
                        composer = await page.locator('.agent-composer').bounding_box()
                        assert composer['y'] + composer['height'] <= height + 1
                        assert await page.locator('.agent-editor').evaluate('e => e.scrollHeight <= e.clientHeight + 1')
                        assert await page.evaluate('''() => document.documentElement.scrollWidth <= innerWidth &&
                            document.documentElement.scrollHeight <= innerHeight''')
                        await page.screenshot(animations='disabled', path=str(screenshots / f'editor-{width}x{height}.png'))
                        await page.get_by_role('button', name='删除编辑对话', exact=True).click()
                        delete_dialog = page.get_by_role('dialog', name='删除编辑对话', exact=True)
                        await expect(delete_dialog).to_contain_text('未保存修改')
                        await expect(delete_dialog.get_by_role('button', name='取消', exact=True)).to_be_focused()
                        assert await page.locator('.shell').evaluate('e => e.inert')
                        box = await delete_dialog.bounding_box()
                        assert box['height'] <= height and box['width'] <= width
                        await page.screenshot(animations='disabled', path=str(screenshots / f'delete-dialog-{width}x{height}.png'))
                        await page.keyboard.press('Escape')
                        await expect(delete_dialog).to_have_count(0)
                        assert not await page.locator('.shell').evaluate('e => e.inert')

                        toggle = page.get_by_role('button', name='角色资料', exact=True)
                        if await toggle.is_visible():
                            await toggle.click()
                        await page.get_by_role('tab', name='角色数据', exact=True).click()
                        await expect(page.locator('.json-data-tree details[open]')).to_have_count(0)
                        await page.screenshot(animations='disabled', path=str(screenshots / f'agent-data-{width}x{height}.png'))
                        await page.locator('.json-data-tree > details').first.locator('summary').click()
                        await expect(page.locator('.json-data-tree details[open]')).to_have_count(1)
                        await page.get_by_role('button', name='全部折叠', exact=True).click()
                        await expect(page.locator('.json-data-tree details[open]')).to_have_count(0)
                        await page.get_by_role('tab', name='修改', exact=False).click()
                        await expect(page.get_by_role('button', name='覆盖保存原卡', exact=True)).to_be_visible()
                        if await toggle.is_visible():
                            await page.get_by_role('button', name='收起角色资料', exact=True).click()
                    await page.set_viewport_size({'width': 1440, 'height': 900})
                    await page.get_by_role('tab', name='修改', exact=False).click()
                    await page.get_by_role('textbox', name='编辑要求', exact=True).fill('下一步待发送的要求')
                    await page.get_by_role('button', name='覆盖保存原卡', exact=True).click()
                    await expect(page.get_by_role('textbox', name='编辑要求', exact=True)).to_have_value('下一步待发送的要求')
                    await expect(page.locator('.agent-editor')).to_contain_text('暂无修改')
                    assert store.card(card['id'])['document']['first_mes'] == '开场一'
                    assert store.session_card(original['id'])['document']['first_mes'] == document['first_mes']
                    await page.reload()
                    await page.set_viewport_size({'width': 1440, 'height': 900})
                    await page.get_by_role('button', name='展开左侧栏', exact=True).click()
                    await page.get_by_role('tab', name='角色卡', exact=True).click()
                    await page.locator('.card-side-item').first.click()
                    await page.locator('.editor-tabs').get_by_role('button', name='Agent 编辑', exact=True).click()
                    await expect(page.locator('.agent-conversation')).to_contain_text('草稿修改完成')
                    await expect(page.locator('.agent-tool-step')).to_have_count(6, timeout=20000)
                    await page.get_by_role('tab', name='修改', exact=False).click()
                    await page.get_by_role('button', name='另存副本', exact=True).click()
                    await expect(page.locator('.agent-workspace-title h1')).to_contain_text('副本')
                    assert len(store.cards()) == 2
                    copied = next(c for c in store.cards() if c['id'] != card['id'])
                    store.delete_card(copied['id'])
                    await page.reload()
                    await page.get_by_role('tab', name='角色卡', exact=True).click()
                    await page.get_by_role('button', name=f"继续编辑 {copied['name']}", exact=True).click()
                    await expect(page.locator('.agent-editor')).to_contain_text('原卡已删除')
                    await page.get_by_role('tab', name='修改', exact=False).click()
                    await page.get_by_role('button', name='另存副本', exact=True).click()
                    await expect(page.locator('.agent-workspace-title h1')).to_contain_text(copied['name'] + '（副本）')
                    assert len(store.cards()) == 2
                    # A dirty context must survive cancelled deletion and failed save;
                    # saving a copy before deletion preserves its only modified artifact.
                    carried = app.state.editor.edits.get(carried_context)
                    carried['document']['description'] = '必须保留的未保存修改'
                    app.state.editor.edits.update(carried_context, carried['document'], carried['revision'])
                    await page.locator(f'.card-side-row[data-card-id="{card["id"]}"] .card-side-item').click()
                    await page.locator('.editor-tabs').get_by_role('button', name='Agent 编辑', exact=True).click()
                    selector = page.get_by_role('combobox', name='切换编辑对话', exact=True)
                    await selector.select_option(carried_context)
                    await page.get_by_role('button', name='删除编辑对话', exact=True).click()
                    dialog = page.get_by_role('dialog', name='删除编辑对话', exact=True)
                    await expect(dialog).to_contain_text('未保存修改')
                    before_card = store.card(card['id'])
                    store.edit_card(card['id'], before_card['document'], before_card['revision'])
                    await dialog.get_by_role('button', name='覆盖保存后删除', exact=True).click()
                    await expect(dialog.get_by_role('alert')).to_contain_text('原卡已改变')
                    assert app.state.editor.edits.get(carried_context)['document']['description'] == '必须保留的未保存修改'
                    await dialog.get_by_role('button', name='另存副本后删除', exact=True).click()
                    await expect(dialog).to_have_count(0)
                    await expect(page.locator('.editor-header h1')).to_contain_text('副本')
                    assert not any(context['id'] == carried_context for context in app.state.editor.edits.list(card['id']))
                    assert any(c['document']['description'] == '必须保留的未保存修改' for c in store.cards())
                    assert store.card(card['id'])['document'] == before_card['document']
                    # Deleting an unchanged context removes its unsent input, not the library card.
                    await page.locator(f'.card-side-row[data-card-id="{card["id"]}"] .card-side-item').click()
                    await page.locator('.editor-tabs').get_by_role('button', name='Agent 编辑', exact=True).click()
                    await expect(page.get_by_role('combobox', name='切换编辑对话', exact=True)).to_have_value(fresh_context)
                    await page.get_by_role('textbox', name='编辑要求', exact=True).fill('待删除的输入')
                    await page.get_by_role('button', name='删除编辑对话', exact=True).click()
                    dialog = page.get_by_role('dialog', name='删除编辑对话', exact=True)
                    await expect(dialog).to_contain_text('没有未保存的角色卡修改')
                    await expect(dialog).to_contain_text('未发送的输入也会删除')
                    await dialog.get_by_role('button', name='删除对话', exact=True).click()
                    await expect(dialog).to_have_count(0)
                    await expect(page.locator('.editor-header h1')).to_be_visible()
                    assert app.state.editor.edits.list(card['id']) == []
                    assert store.card(card['id'])['document'] == before_card['document']
                    assert await page.evaluate('(id) => sessionStorage.getItem(`tavern-editor-input-${id}`)', fresh_context) is None
                    assert not errors, errors
                    print('Card search, adaptation, review, overwrite, copy, restore and four viewports passed.')
                finally:
                    await browser.close()
        finally:
            server.should_exit = True
            await asyncio.to_thread(thread.join, 10)


if __name__ == '__main__':
    asyncio.run(main())
