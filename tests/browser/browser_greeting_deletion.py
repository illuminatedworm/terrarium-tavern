"""Exercise opening deletion and preservation using a disposable local database."""

import asyncio
import copy
import json
import os
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

import httpx
from playwright.async_api import async_playwright


async def check(base):
    document = json.loads(Path('samples/雾港来信.json').read_text(encoding='utf-8'))
    data = document['data']
    data['name'] = '开场删除检查'
    greetings = [f'第 {i} 个开场：{{{{user}}}}\r\n独立内容 {i}。' for i in range(1, 8)]
    data['first_mes'], data['alternate_greetings'] = greetings[0], greetings[1:]
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(viewport={'width': 1440, 'height': 1000})
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        await page.goto(base)
        await page.get_by_role('tab', name='角色卡', exact=True).click()
        await page.locator('input[accept=".json,.png,.charx"]').set_input_files({
            'name': 'openings.json', 'mimeType': 'application/json',
            'buffer': json.dumps(document, ensure_ascii=False).encode(),
        })
        await page.get_by_role('button', name='保存角色', exact=True).wait_for()
        cards = await (await page.request.get(f'{base}/api/cards')).json()
        cid = cards[0]['id']
        original = cards[0]['document']
        session_response = await page.request.post(f'{base}/api/sessions', data={
            'card_id': cid, 'greeting': 6, 'persona': '测试旅人',
        })
        assert session_response.ok, await session_response.text()
        session = await session_response.json()
        await page.get_by_role('button', name='开场与提示词', exact=True).click()

        async def delete(index):
            if index > 1:
                details = page.locator('.editor-fields > details').nth(index - 2)
                if not await details.evaluate('e=>e.open'):
                    await details.locator('summary').click()
            await page.get_by_role('button', name=f'删除开场 {index}', exact=True).click()

        async def values():
            return await page.locator('.editor-fields .expandable-textarea > textarea').evaluate_all(
                'nodes=>nodes.slice(0,-3).map(e=>e.value)'
            )

        # Undo reinserts the removed entry without overwriting later edits elsewhere.
        await delete(6)
        assert await values() == [g.replace('\r\n', '\n') for g in greetings[:5] + greetings[6:]]
        seventh = page.get_by_label('开场 6', exact=True)
        last = page.locator('.editor-fields > details').last
        if not await last.evaluate('e=>e.open'):
            await last.locator('summary').click()
        await seventh.fill('保留第七个的新编辑')
        await page.get_by_role('button', name='撤销删除开场白', exact=True).click()
        assert (await values())[-1] == '保留第七个的新编辑'
        if not await last.evaluate('e=>e.open'):
            await last.locator('summary').click()
        await page.get_by_label('开场 7', exact=True).fill(greetings[6])
        await delete(1)
        assert await page.get_by_label('开场白', exact=True).input_value() == greetings[1].replace('\r\n', '\n')
        await page.get_by_role('button', name='撤销删除开场白', exact=True).click()
        await delete(6)
        unsaved = await (await page.request.get(f'{base}/api/cards/{cid}')).json()
        assert unsaved['document'] == original
        await page.get_by_role('button', name='保存角色', exact=True).click()
        await page.get_by_text('角色卡已保存。', exact=True).wait_for()
        saved = await (await page.request.get(f'{base}/api/cards/{cid}')).json()
        expected = copy.deepcopy(original)
        del expected['data']['alternate_greetings'][4]
        # Browsers normalize textarea line endings only for fields explicitly edited above.
        expected['data']['alternate_greetings'][-1] = greetings[6].replace('\r\n', '\n')
        assert saved['document'] == expected
        existing = await (await page.request.get(f"{base}/api/sessions/{session['id']}")).json()
        assert existing['messages'] == session['messages']
        await page.reload()
        await page.get_by_role('tab', name='角色卡', exact=True).click()
        await page.locator('.card-side-item').filter(has_text=data['name']).first.click()
        await page.get_by_role('button', name='开场与提示词', exact=True).click()
        assert len(await values()) == 6
        # Deleting the final opening leaves a valid empty default; undo restores every item.
        before_clear = await values()
        for _ in range(6):
            await delete(1)
        assert await values() == ['']
        for _ in range(6):
            await page.get_by_role('button', name='撤销删除开场白', exact=True).click()
        assert await values() == before_clear
        await page.set_viewport_size({'width': 390, 'height': 844})
        await page.get_by_role('button', name='折叠左侧栏', exact=True).click()
        assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        await page.screenshot(path='.cache/greeting-deletion-mobile.png', full_page=True)
        await page.set_viewport_size({'width': 1440, 'height': 1000})
        await page.get_by_role('button', name='展开左侧栏', exact=True).click()
        await page.screenshot(path='.cache/greeting-deletion.png', full_page=True)
        await page.get_by_role('tab', name='会话', exact=True).click()
        await page.get_by_role('button', name='＋ 开始新故事', exact=True).click()
        await page.get_by_role('button', name=f"选择角色卡 {data['name']}", exact=True).click()
        await page.get_by_role('tab', name='开场 6', exact=True).click()
        assert '独立内容 7' in await page.get_by_role('tabpanel').inner_text()
        await page.get_by_role('button', name='进入故事 →', exact=True).click()
        await page.locator('.story').wait_for()
        assert not errors, errors
        await browser.close()
        print('Middle/default/final deletion, undo, exact preservation, persistence and story entry passed.')


async def main():
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        port = listener.getsockname()[1]
    base = f'http://127.0.0.1:{port}'
    with tempfile.TemporaryDirectory(prefix='greeting-deletion-', dir='.cache') as directory:
        with Path('.cache/greeting-deletion-server.log').open('w', encoding='utf-8') as log:
            process = subprocess.Popen(
                [sys.executable, '-m', 'tavern', '--port', str(port), '--data', directory],
                stdout=log, stderr=log,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
            )
            try:
                async with httpx.AsyncClient(trust_env=False) as client:
                    for _ in range(100):
                        try:
                            (await client.get(f'{base}/api/health')).raise_for_status()
                            break
                        except httpx.HTTPError:
                            await asyncio.sleep(0.1)
                    else:
                        raise RuntimeError('测试服务未启动')
                await check(base)
            finally:
                async with httpx.AsyncClient(trust_env=False) as client:
                    try:
                        await client.post(f'{base}/api/shutdown', json={'confirm': True})
                    except httpx.HTTPError:
                        pass
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    process.wait(timeout=5)


if __name__ == '__main__':
    asyncio.run(main())
