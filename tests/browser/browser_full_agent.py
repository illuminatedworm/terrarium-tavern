"""Full Agent mode selection, real card IO, notes, rollover, and responsive UI."""
import asyncio
import socket
import tempfile
from pathlib import Path
from threading import Thread

import uvicorn
from playwright.async_api import async_playwright, expect

from tavern.application.api import create_app
from tests.backend.test_full_agent import Model


async def main():
    output = Path(".cache/full-agent-ui")
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as data:
        app = create_app(data)
        store = app.state.store
        card = store.import_card(Path("samples/雾港来信.json").read_bytes(), "card.json")
        settings = store.settings()
        settings.full_agent_config.auto_retrieval = False
        store.save_settings(settings)
        app.state.service.full.provider_factory = lambda: Model(
            ("read_history", {"ref": "H2"}),
            ("read_history", {"reason": "核对编辑后的H2内容"}),
            ("write_note", {"key": "约定/钟楼", "content": "玩家答应明天见面", "kind": "fact", "sources": ["H2"]}),
            ("new_context", {"content": "约定明天钟楼见面，继续当前对话。", "keep_recent": 0}),
            "她点头应下，钟声从远处传来。",
        )
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
            assert server.started
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch()
                page = await browser.new_page(viewport={"width": 1440, "height": 900})
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                await page.goto(address)
                await page.get_by_role("tab", name="会话", exact=True).click()
                await page.get_by_role("button", name="＋ 开始新故事", exact=True).click()
                await page.get_by_role("button", name=f"选择角色卡 {card['name']}").click()
                await page.get_by_label("运行模式", exact=True).select_option("full_agent")
                await page.get_by_role("button", name="进入故事 →", exact=True).click()
                await page.locator(".story").wait_for()
                await expect(page.locator(".story-heading")).to_contain_text("Full Agent")
                await expect(page.locator('.cache-breakpoint-fallback')).to_contain_text('计划断点')
                follow = page.locator('.cache-breakpoint-fallback .cache-breakpoint-dot')
                await follow.click()
                await expect(follow).to_have_attribute('aria-label', '开启缓存断点自动跟进')
                await follow.click()
                await expect(follow).to_have_attribute('aria-label', '关闭缓存断点自动跟进')
                await page.get_by_placeholder("说些什么，或描述你的行动…").fill("我答应明天在钟楼见面。")
                await page.get_by_role("button", name="发送", exact=True).click()
                await page.locator(".message.assistant").nth(1).wait_for(timeout=30000)
                await page.get_by_role("button", name="发送", exact=True).wait_for()
                await expect(page.locator(".message.assistant").nth(1)).to_contain_text("她点头应下")
                assert "工作过程" not in await page.locator(".message.assistant").nth(1).inner_text()
                activity = page.locator(".agent-message-activity")
                await expect(activity.locator(".activity-name")).to_have_text([
                    "read_history", "read_history", "write_note", "new_context"])
                assert await activity.locator("details[open]").count() == 0
                assert await page.evaluate("""() => {
                    const activity = document.querySelector('.agent-message-activity');
                    const user = document.querySelector('.message.user');
                    const reply = document.querySelectorAll('.message.assistant')[1];
                    return !!(user.compareDocumentPosition(activity) & Node.DOCUMENT_POSITION_FOLLOWING)
                        && !!(activity.compareDocumentPosition(reply) & Node.DOCUMENT_POSITION_FOLLOWING);
                }""")
                read_call = activity.locator('.agent-activity').filter(
                    has=page.locator('.activity-name', has_text='read_history')).first
                await read_call.locator('summary').click()
                await expect(read_call.get_by_role('region', name='调用参数')).to_contain_text('H2')
                await expect(read_call.get_by_role('region', name='工具返回')).to_contain_text('我答应明天在钟楼见面')
                await read_call.locator('summary').press('Enter')
                await expect(read_call).not_to_have_attribute('open', '')
                # Error state must share the disclosure layout, including at
                # high DPI and equivalent 200% browser zoom, without the global
                # error banner's horizontal flex layout squeezing its header.
                saved_browser_state = await page.context.storage_state()
                for width, height, scale, label in [
                    (2560, 1440, 1, '2560'), (1920, 1080, 1, '1920'),
                    (360, 792, 3, 'mobile'), (960, 540, 2, 'zoom200'),
                ]:
                    context = await browser.new_context(viewport={'width': width, 'height': height},
                        device_scale_factor=scale, storage_state=saved_browser_state)
                    try:
                        detail_page = await context.new_page()
                        await detail_page.goto(address)
                        calls = detail_page.locator('.agent-activity').filter(
                            has=detail_page.locator('.activity-name', has_text='read_history'))
                        for call in [calls.first, calls.last]:
                            await call.locator('summary').click()
                            await expect(call.get_by_role('region', name='调用参数')).to_contain_text('H2')
                        await expect(calls.last.get_by_role('region', name='工具返回')).to_contain_text('Field required')
                        layout = await calls.evaluate_all("""elements => elements.map(el => {
                            const properties = ['display', 'padding', 'margin', 'borderWidth', 'borderRadius',
                                'font', 'gap', 'whiteSpace', 'overflowWrap', 'maxHeight'];
                            const style = node => Object.fromEntries(properties.map(k => [k, getComputedStyle(node)[k]]));
                            const header = el.querySelector('summary'), details = el.querySelector('.activity-details');
                            const a = header.getBoundingClientRect(), b = details.getBoundingClientRect();
                            return {root:style(el), header:style(header), details:style(details),
                                heading:style(el.querySelector('h3')), text:style(el.querySelector('pre')),
                                headerHeight:a.height, width:el.getBoundingClientRect().width,
                                vertical:b.top >= a.bottom - 1};
                        })""")
                        assert len(layout) == 2 and all(item['vertical'] for item in layout), (label, layout)
                        assert layout[0] == layout[1], (label, layout)
                        assert await detail_page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                        await calls.last.scroll_into_view_if_needed()
                        await detail_page.screenshot(path=str(output / f'tool-error-{label}.png'))
                        if label == '1920':
                            await calls.first.screenshot(path=str(output / 'tool-normal-reference.png'))
                            await calls.last.screenshot(path=str(output / 'tool-error-detail.png'))
                    finally:
                        await context.close()
                await page.reload()
                await expect(page.locator('.activity-name')).to_have_text([
                    'read_history', 'read_history', 'write_note', 'new_context'])
                assert await page.locator('.agent-message-activity details[open]').count() == 0
                await page.get_by_role('button', name='Agent 工作记录', exact=True).click()
                await expect(page.locator('.work-toolbar h2')).to_contain_text('Debug')
                assert await page.locator('.work-turn[open]').count() == 0
                await page.locator('.work-turn > summary').click()
                tool_event = page.locator('.work-event').filter(has_text='Full Agent 工具调用').first
                await tool_event.locator(':scope > summary').click()
                await tool_event.get_by_role('button', name='原始 JSON', exact=True).click()
                await expect(tool_event.locator('.work-raw-json')).to_contain_text('read_history')
                await page.get_by_role('button', name='对话', exact=True).click()
                if not await page.get_by_role("button", name="场景", exact=True).is_visible():
                    await page.get_by_role("button", name="展开右侧栏", exact=True).click()
                await page.get_by_role("button", name="工具", exact=True).click()
                tools = page.get_by_role("region", name="Full Agent 工具")
                search_toggle = tools.get_by_label("启用 search_read_lore", exact=True)
                await expect(search_toggle).to_be_checked()
                ref_toggle = tools.get_by_label("search_read_lore 返回编号", exact=True)
                title_toggle = tools.get_by_label("search_read_lore 返回标题", exact=True)
                await expect(ref_toggle).not_to_be_checked()
                await expect(title_toggle).not_to_be_checked()
                await ref_toggle.check()
                await expect(ref_toggle).to_be_checked()
                await expect(ref_toggle).to_be_enabled()
                await title_toggle.check()
                await expect(title_toggle).to_be_checked()
                await expect(title_toggle).to_be_enabled()
                await search_toggle.uncheck()
                await expect(search_toggle).not_to_be_checked()
                await expect(search_toggle).to_be_enabled()
                await tools.locator(".tool-card").filter(has=page.get_by_label("启用 search_read_lore", exact=True)).locator("summary").click()
                await expect(tools).to_contain_text("Uses keyword and BM25 recall.")
                await expect(tools).to_contain_text("Search text or retrieval clues")
                await page.screenshot(path=str(output / "tools-desktop.png"))
                await page.get_by_role("button", name="场景", exact=True).click()
                await page.get_by_role("button", name="工具", exact=True).click()
                await expect(search_toggle).not_to_be_checked()
                await expect(ref_toggle).to_be_checked()
                await expect(title_toggle).to_be_checked()
                await page.get_by_role("button", name="折叠左侧栏", exact=True).click()
                await page.set_viewport_size({"width": 390, "height": 844})
                if not await search_toggle.is_visible():
                    await page.get_by_role("button", name="展开右侧栏", exact=True).click()
                await tools.locator(".tool-card").filter(has=page.get_by_label("启用 search_read_lore", exact=True)).locator("summary").click()
                await page.screenshot(path=str(output / "tools-mobile.png"))
                assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                await page.set_viewport_size({"width": 1440, "height": 900})
                await page.get_by_role("button", name="展开左侧栏", exact=True).click()
                await search_toggle.check()
                await expect(search_toggle).to_be_checked()
                await expect(search_toggle).to_be_enabled()
                await page.get_by_role("button", name="场景", exact=True).click()
                notes = page.get_by_role("region", name="Full Agent 记忆与窗口")
                await expect(notes).to_contain_text("工作窗口 2")
                await expect(notes.get_by_text("约定/钟楼 · 有据事实", exact=True)).to_be_visible()
                await expect(page.get_by_role("button", name="重新生成记忆", exact=True)).to_have_count(0)
                await page.screenshot(path=str(output / "desktop.png"))
                await notes.get_by_label("交接笔记（handoff）", exact=True).fill("人工确认：明天去钟楼。")
                await notes.get_by_role("button", name="保存并换新窗口", exact=True).click()
                await expect(notes).to_contain_text("工作窗口 3")
                await page.get_by_role("button", name="状态与记忆", exact=True).click()
                modal = page.get_by_role("dialog")
                await expect(modal.get_by_label("交接笔记（handoff）", exact=True)).to_have_value("人工确认：明天去钟楼。")
                await page.set_viewport_size({"width": 390, "height": 844})
                await page.screenshot(path=str(output / "mobile.png"))
                assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                await modal.get_by_role("button", name="关闭", exact=True).click()
                await page.set_viewport_size({"width": 1440, "height": 900})
                await page.get_by_role("button", name="⚙ 设置", exact=True).click()
                modal = page.get_by_role("dialog", name="设置")
                await modal.get_by_role("button", name="full agent 工具与记忆", exact=True).click()
                await expect(modal.get_by_label("每回合模型调用上限", exact=True)).to_have_value("24")
                await page.screenshot(path=str(output / "settings.png"))
                await modal.get_by_role('button', name='关闭', exact=True).click()
                app.state.service.full.provider_factory = lambda: Model(
                    ("read_note", {"ref": "约定/钟楼"}), "她再次确认明天见面的约定。")
                await page.get_by_placeholder('说些什么，或描述你的行动…').fill('我们再确认一次明天的约定。')
                await page.get_by_role('button', name='发送', exact=True).click()
                await expect(page.locator('.message.assistant')).to_have_count(3)
                await page.get_by_role('button', name='◫ 上下文', exact=True).click()
                await expect(page.get_by_role('heading', name='上下文预览')).to_be_visible()
                history = page.locator('.context-group.context-history')
                # Runtime updates can separate history groups; preserve their
                # chronological message order when checking native tool pairs.
                entries = history.locator('.context-entry')
                await expect(entries.locator('.badge')).to_have_text(['user', 'assistant', 'tool', 'assistant'])
                await entries.nth(1).locator('summary').click()
                await expect(entries.nth(1).locator('pre')).to_contain_text('read_note')
                await expect(entries.nth(1).locator('pre')).to_contain_text('约定/钟楼')
                await expect(entries.nth(1).locator('pre')).to_contain_text('工作过程不会混入正文')
                await entries.nth(2).locator('summary').click()
                await expect(entries.nth(2).locator('pre')).to_contain_text('玩家答应明天见面')
                assert await entries.locator('.context-entry-position').all_text_contents() != ['位置 '] * 4
                await page.screenshot(path=str(output / 'context-preview.png'))
                await page.get_by_role('dialog').get_by_role('button', name='关闭', exact=True).click()
                await page.get_by_role('button', name='Agent 工作记录', exact=True).click()
                await expect(page.locator('.work-turn-preview')).to_have_text([
                    '我答应明天在钟楼见面。', '我们再确认一次明天的约定。',
                ])
                assert not errors, errors
                await browser.close()
        finally:
            server.should_exit = True
            thread.join(timeout=15)


if __name__ == "__main__":
    asyncio.run(main())
