"""Check built settings chunks load on demand and the modal remains dismissible."""
import asyncio
import socket
import tempfile
from threading import Thread

import uvicorn
from playwright.async_api import async_playwright, expect

from tavern.application.api import create_app


async def main():
    with tempfile.TemporaryDirectory() as root:
        app = create_app(root)
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
            assert server.started
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch()
                try:
                    page = await browser.new_page()
                    requests = []
                    page.on('request', lambda request: requests.append(request.url))
                    gate = asyncio.Event()

                    async def delayed(route):
                        await gate.wait()
                        await route.continue_()

                    await page.route('**/assets/SettingsPanel-*.js', delayed)
                    await page.goto(address)
                    assert not any('/SettingsPanel-' in url for url in requests)
                    await page.get_by_role('button', name='⚙ 设置', exact=True).click()
                    dialog = page.get_by_role('dialog', name='设置', exact=True)
                    await expect(dialog.get_by_role('status')).to_have_text('正在加载设置…')
                    await dialog.get_by_role('button', name='关闭', exact=True).click()
                    await expect(dialog).to_be_hidden()
                    gate.set()
                    await page.get_by_role('button', name='⚙ 设置', exact=True).click()
                    await expect(dialog.get_by_role('button', name='full agent 模型与生成', exact=True)).to_be_visible()
                    await dialog.get_by_role('button', name='关闭', exact=True).click()
                    await expect(dialog).to_be_hidden()
                    await page.close()

                    page = await browser.new_page()
                    failed = False

                    async def fail_once(route):
                        nonlocal failed
                        if not failed:
                            failed = True
                            await route.abort()
                        else:
                            await route.continue_()

                    await page.route('**/assets/SettingsPanel-*.js', fail_once)
                    await page.goto(address)
                    await page.get_by_role('button', name='⚙ 设置', exact=True).click()
                    dialog = page.get_by_role('dialog', name='设置', exact=True)
                    await expect(dialog.get_by_role('alert')).to_contain_text('设置页面加载失败')
                    await dialog.get_by_role('button', name='关闭', exact=True).click()
                    await expect(dialog).to_be_hidden()
                    # Browsers cache a failed module import until the document is reloaded.
                    await page.reload()
                    await page.get_by_role('button', name='⚙ 设置', exact=True).click()
                    await expect(dialog.get_by_role('button', name='full agent 模型与生成', exact=True)).to_be_visible()
                    print('Settings chunks: deferred load, close while loading, failure feedback and reload recovery passed.')
                finally:
                    await browser.close()
        finally:
            server.should_exit = True
            thread.join(timeout=10)


if __name__ == '__main__':
    asyncio.run(main())
