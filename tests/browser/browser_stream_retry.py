"""Verify manual scrolling during real streaming and recovery after MVU failure."""

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
from tavern.storage.store import dump
from tests.support.tracker_model import PatchModel


async def main():
    screenshots = Path(".cache/stream-retry-ui")
    screenshots.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as root:
        app = create_app(root)
        store = app.state.store
        card = store.import_card(json.dumps({"name": "流式与重试检查",
                                             "first_mes": "旧剧情。\n\n" * 200}).encode(), "card.json")
        session = store.new_session(SessionCreate(card_id=card["id"]))
        snapshot = session["snapshot"] | {"state": {
            "stat_data": {"hp": 5}, "schema": {"type": "object", "extensible": False,
                                                "properties": {"hp": {"type": "number"}}}}}
        with store.connection() as db:
            db.execute("UPDATE messages SET snapshot=? WHERE id=?", (dump(snapshot), session["head"]))
        responses = iter([
            ScriptEntry(response="新的剧情缓缓展开。\n\n" * 200, chunk_size=50, delay_per_chunk=.08),
            '错误草稿',
            "重试成功。",
        ])
        providers = []

        def make_provider():
            provider = ScriptedLLM([next(responses)])
            providers.append(provider)
            return provider

        app.state.service.provider_factory = make_provider
        variable_responses = iter(['[{"op":"add","path":"/unknown","value":1}]',
                                   '[{"op":"replace","path":"/hp","value":6}]'])
        app.state.service.variable_provider_factory = lambda: PatchModel(next(variable_responses))
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
                    send = page.get_by_role('button', name='发送', exact=True)
                    await expect(send).to_be_visible()
                    checkbox = page.get_by_role('checkbox', name='本轮更新变量', exact=True)
                    await expect(checkbox).not_to_be_checked()
                    await page.get_by_role('button', name='折叠左侧栏', exact=True).click()
                    await page.get_by_role('button', name='折叠右侧栏', exact=True).click()
                    for width, height in [(1440, 900), (1280, 720), (390, 844), (720, 450)]:
                        # 720x450 is the CSS viewport of 1440x900 at 200% browser zoom.
                        await page.set_viewport_size({'width': width, 'height': height})
                        await expect(checkbox).to_be_visible()
                        assert await page.evaluate('''() =>
                            document.documentElement.scrollWidth <= innerWidth &&
                            document.documentElement.scrollHeight <= innerHeight''')
                        await page.screenshot(path=str(screenshots / f'variables-{width}x{height}.png'))
                    await page.set_viewport_size({'width': 1440, 'height': 1000})
                    await page.get_by_role('button', name='展开左侧栏', exact=True).click()
                    await page.get_by_role('button', name='展开右侧栏', exact=True).click()
                    composer_input = page.get_by_role('textbox', name='用户输入', exact=True)
                    expand_input = page.get_by_role('button', name='放大编辑：用户输入')
                    await send.focus()
                    await expect(expand_input).to_be_hidden()
                    await composer_input.focus()
                    await expect(expand_input).to_be_visible()
                    await composer_input.fill('暂存输入')
                    await expand_input.click()
                    editor = page.get_by_role('dialog', name='编辑：用户输入')
                    await expect(editor.get_by_role('textbox')).to_have_value('暂存输入')
                    await editor.get_by_role('textbox').fill('取消的改动')
                    await editor.get_by_role('button', name='取消').click()
                    await expect(composer_input).to_have_value('暂存输入')
                    await expect(composer_input).to_be_focused()
                    await expand_input.click()
                    await editor.get_by_role('textbox').fill('应用的改动')
                    await editor.get_by_role('button', name='应用修改').click()
                    await expect(composer_input).to_have_value('应用的改动')
                    await composer_input.fill('第一轮')
                    await send.click()
                    await expect(page.locator('.draft')).to_contain_text('新的剧情', timeout=15000)
                    chat = page.locator('.messages')
                    await chat.hover()
                    await page.mouse.wheel(0, -600)
                    await expect(page.get_by_role('button', name='跳到最新', exact=True)).to_be_visible()
                    await asyncio.sleep(.15)
                    top = await chat.evaluate('e=>e.scrollTop')
                    length = len(await page.locator('.draft').inner_text())
                    await asyncio.sleep(.5)
                    assert len(await page.locator('.draft').inner_text()) > length
                    assert abs(await chat.evaluate('e=>e.scrollTop') - top) < 3
                    await page.get_by_role('button', name='跳到最新', exact=True).click()
                    await asyncio.sleep(.2)
                    assert await chat.evaluate('e=>e.scrollHeight-e.scrollTop-e.clientHeight') < 50
                    await chat.hover()
                    await page.mouse.wheel(0, -600)
                    await asyncio.sleep(.15)
                    top = await chat.evaluate('e=>e.scrollTop')
                    await expect(send).to_be_visible(timeout=15000)
                    assert abs(await chat.evaluate('e=>e.scrollTop') - top) < 3
                    await page.screenshot(path=str(screenshots / 'paused-stream-desktop.png'))
                    await composer_input.fill('第二轮')
                    await page.get_by_role('checkbox', name='本轮更新变量', exact=True).check()
                    await send.click()
                    retry = page.locator('.story').get_by_role('button', name='重新生成', exact=True)
                    await expect(retry).to_be_enabled(timeout=15000)
                    await expect(page.get_by_role('alert')).to_contain_text('未提交')
                    await expect(page.get_by_role('alert')).not_to_contain_text('Page.evaluate')
                    before = store.session(session["id"])
                    assert len(before["messages"]) == 3
                    assert before["snapshot"]["state"]["stat_data"]["hp"] == 5
                    await page.reload()
                    await page.get_by_role('checkbox', name='本轮更新变量', exact=True).check()
                    await expect(retry).to_be_visible()
                    await expect(page.locator('.draft')).to_contain_text('错误草稿')
                    await expect(page.locator('.message.user').last).to_contain_text('第二轮')
                    await expect(page.locator('.message.user').filter(has_text='第二轮')).to_have_count(1)
                    await expect(retry).to_have_count(1)
                    await retry.click()
                    await expect(retry).to_have_count(1)
                    note = '严格遵守变量输出格式，不要使用不存在的变量路径。'
                    await page.get_by_role('textbox', name='重新生成补充要求').fill(note)
                    await page.screenshot(path=str(screenshots / 'failed-retry-desktop.png'))
                    await page.get_by_role('button', name='折叠左侧栏', exact=True).click()
                    await page.get_by_role('button', name='折叠右侧栏', exact=True).click()
                    await page.set_viewport_size({"width": 390, "height": 844})
                    await expect(retry).to_be_visible()
                    assert await page.locator('.regenerate-bar').evaluate('e=>e.scrollWidth<=e.clientWidth+1')
                    await page.screenshot(path=str(screenshots / 'failed-retry-mobile.png'))
                    await retry.click()
                    await expect(page.locator('.message').last).to_contain_text('重试成功', timeout=15000)
                    await expect(send).to_be_visible(timeout=15000)
                    after = store.session(session["id"])
                    assert len(after["messages"]) == 5
                    assert after["messages"][-2]["raw"] == '第二轮'
                    assert after["snapshot"]["state"]["stat_data"]["hp"] == 6
                    retry_turn = store.turn(after['turns'][0]['id'])
                    assert retry_turn['request']['mode'] == 'send'
                    assert retry_turn['request']['text'] == '第二轮'
                    assert retry_turn['request']['regenerate_note'] == note
                    assert any(note in m.get('content', '') for m in providers[-1].last_messages)
                    assert not errors, errors
                    print('Streaming pause/resume, end position, failed retry and reload recovery passed.')
                finally:
                    await browser.close()
        finally:
            server.should_exit = True
            await asyncio.to_thread(thread.join, 10)


if __name__ == "__main__":
    asyncio.run(main())
