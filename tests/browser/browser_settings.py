"""Settings linkage, explanatory text and tooltips against isolated local data."""

import asyncio
import socket
import tempfile
from pathlib import Path
from threading import Thread

import uvicorn
from playwright.async_api import async_playwright, expect

from tavern.application.api import create_app


async def main():
    screenshots = Path(".cache/settings-ui")
    screenshots.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as data:
        app = create_app(data)
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
                    await page.get_by_role('button', name='⚙ 设置', exact=True).click()
                    panel = page.get_by_role('dialog', name='设置', exact=True)
                    await expect(panel.get_by_text('严格要求中途 system 注入')).to_have_count(0)
                    await expect(panel.get_by_label('模型上下文窗口（token）', exact=True)).to_have_value('32768')
                    await expect(panel.get_by_label('上下文预算', exact=True)).to_have_count(0)
                    output = panel.get_by_label('单次输出上限（token）', exact=True)
                    await expect(output).not_to_be_visible()
                    await panel.locator('.output-settings > summary').click()
                    await expect(output).to_have_value('2048')
                    await page.screenshot(path=str(screenshots / 'model-output-desktop.png'))
                    await panel.locator('.output-settings > summary').click()

                    await panel.get_by_role('button', name='系统提示词', exact=True).click()
                    fields = panel.locator('.expandable-textarea > textarea')
                    assert await fields.count() == 3
                    assert await fields.evaluate_all('nodes=>nodes.every(e=>e.getBoundingClientRect().height===144)')
                    for label in ['应用系统提示词', '摘要系统提示词', '记忆系统提示词']:
                        await panel.get_by_role('button', name=f'放大编辑：{label}', exact=True).click()
                        editor = page.get_by_role('dialog', name=f'编辑：{label}', exact=True)
                        await expect(editor).to_be_visible()
                        await editor.get_by_role('button', name='取消', exact=True).click()
                    await panel.get_by_role('button', name='放大编辑：摘要系统提示词', exact=True).click()
                    editor = page.get_by_role('dialog', name='编辑：摘要系统提示词', exact=True)
                    await editor.get_by_label('编辑内容：摘要系统提示词', exact=True).fill('自定义摘要提示词')
                    await editor.get_by_role('button', name='应用修改', exact=True).click()
                    await panel.get_by_label('记忆系统提示词', exact=True).fill('自定义记忆提示词')
                    await page.screenshot(path=str(screenshots / 'prompts-desktop.png'))
                    await page.set_viewport_size({"width": 390, "height": 844})
                    assert await fields.evaluate_all('nodes=>nodes.every(e=>e.getBoundingClientRect().height===144)')
                    await panel.get_by_role('button', name='放大编辑：记忆系统提示词', exact=True).click()
                    await page.screenshot(path=str(screenshots / 'prompt-expanded-mobile.png'))
                    await page.get_by_role('dialog', name='编辑：记忆系统提示词', exact=True).get_by_role('button', name='取消', exact=True).click()
                    await page.set_viewport_size({"width": 1440, "height": 1000})
                    await panel.get_by_role('button', name='保存设置', exact=True).click()
                    await expect(panel).to_have_count(0)
                    await page.get_by_role('button', name='⚙ 设置', exact=True).click()
                    await panel.get_by_role('button', name='系统提示词', exact=True).click()
                    await expect(panel.get_by_label('摘要系统提示词', exact=True)).to_have_value('自定义摘要提示词')
                    await expect(panel.get_by_label('记忆系统提示词', exact=True)).to_have_value('自定义记忆提示词')
                    await panel.get_by_role('button', name='世界书注入', exact=True).click()
                    await expect(panel.get_by_label('新对话的注入方案', exact=False)).to_have_value('conversation')
                    count = panel.get_by_label('单次新增条目上限', exact=True)
                    chars = panel.get_by_label('单次新增字符预算', exact=True)
                    await expect(count).to_have_value('5')
                    await expect(chars).to_have_value('3000')
                    await count.fill('0')
                    await expect(panel.get_by_role('button', name='保存设置', exact=True)).to_be_disabled()
                    await count.fill('2')
                    await chars.fill('1000')
                    await panel.get_by_role('button', name='保存设置', exact=True).click()
                    await expect(panel).to_have_count(0)
                    await page.get_by_role('button', name='⚙ 设置', exact=True).click()
                    await panel.get_by_role('button', name='世界书注入', exact=True).click()
                    await expect(count).to_have_value('2')
                    await expect(chars).to_have_value('1000')

                    await expect(panel.get_by_text('重复命中不会再次注入或移动。', exact=False)).to_be_visible()
                    await page.screenshot(path=str(screenshots / 'lore-desktop.png'))
                    await panel.get_by_role('button', name='历史窗口与后台维护', exact=True).click()
                    cap = panel.get_by_label('历史消息 cap', exact=False)
                    summary = panel.get_by_label('摘要周期（轮）', exact=False)
                    memory = panel.get_by_label('记忆周期（轮）', exact=False)
                    save = panel.get_by_role('button', name='保存设置', exact=True)
                    await expect(save).to_be_enabled()
                    await expect(summary).to_have_attribute('max', '5')
                    await cap.fill('18')
                    await expect(summary).to_have_attribute('max', '2')
                    await expect(summary).to_have_value('5')
                    await expect(save).to_be_disabled()
                    await expect(panel.get_by_role('alert')).to_contain_text('不能超过 2 轮')
                    await summary.fill('2')
                    await memory.fill('2')
                    await expect(save).to_be_enabled()
                    help = panel.locator('.field-help').nth(2)
                    await help.focus()
                    await expect(help).to_be_focused()
                    assert 'cap 减 6' in await help.get_attribute('data-tooltip')
                    await page.wait_for_function("e => getComputedStyle(e, '::after').opacity === '1'", arg=await help.element_handle())
                    await page.screenshot(path=str(screenshots / 'maintenance-desktop.png'))
                    await panel.get_by_label('窗口模式', exact=False).select_option('off')
                    await expect(cap).to_be_disabled()
                    await expect(summary).to_have_attribute('max', '100')
                    await summary.fill('100')
                    await expect(save).to_be_enabled()
                    await panel.get_by_label('窗口模式', exact=False).select_option('messages')
                    await expect(save).to_be_disabled()
                    await summary.fill('2')
                    await save.click()
                    await expect(panel).to_have_count(0)
                    await page.get_by_role('button', name='⚙ 设置', exact=True).click()
                    await panel.get_by_role('button', name='历史窗口与后台维护', exact=True).click()
                    await expect(cap).to_have_value('18')
                    await expect(summary).to_have_value('2')
                    await page.set_viewport_size({"width": 390, "height": 844})
                    await page.screenshot(path=str(screenshots / 'maintenance-mobile.png'))
                    assert await panel.evaluate('e => e.scrollWidth <= e.clientWidth + 1')
                    assert not errors, errors
                    print('Settings linkage, persistence, tooltips and mobile layout passed.')
                finally:
                    await browser.close()
        finally:
            server.should_exit = True
            await asyncio.to_thread(thread.join, 10)


if __name__ == '__main__':
    asyncio.run(main())
