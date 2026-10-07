"""Browser regressions for dense history navigation, using actual Vue components."""
import asyncio
import json
import socket
import subprocess
import sys
from pathlib import Path

from playwright.async_api import async_playwright, expect


async def main():
    output = Path('.agents/.cache/rail-regression')
    output.mkdir(parents=True, exist_ok=True)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    process = subprocess.Popen(
        ['node', 'node_modules/vite/bin/vite.js', '--host', '127.0.0.1', '--port', str(port), '--strictPort'],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0,
    )
    try:
        for _ in range(100):
            try:
                _, writer = await asyncio.open_connection('127.0.0.1', port)
                writer.close()
                await writer.wait_closed()
                break
            except OSError:
                await asyncio.sleep(.1)
        else:
            raise AssertionError('Fixture server did not start')
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            errors = []
            results = []
            for width, height, scale, label in [
                (2560, 1440, 1, 'desktop2560'), (1920, 1080, 1, 'desktop1920'),
                (360, 792, 3, 'mobile'), (960, 540, 2, 'zoom200'),
            ]:
                context = await browser.new_context(viewport={'width': width, 'height': height},
                                                    device_scale_factor=scale, has_touch=label == 'mobile')
                page = await context.new_page()
                page.on('pageerror', lambda error: errors.append(str(error)))
                await page.goto(f'http://127.0.0.1:{port}/tests/browser/fixtures/conversation-rail.html')
                rail = page.get_by_role('slider', name='对话历史导航')
                panel = page.get_by_role('region', name='附近对话轮次')
                counter = page.locator('.rail-preview-heading small')
                for count in (200, 1000, 5000):
                    await page.evaluate('(n) => railFixture.seed(n)', count)
                    await expect(rail).to_have_attribute('aria-valuemax', str(count))
                    await rail.focus()
                    await rail.press('Home')
                    await expect(rail).to_have_attribute('aria-valuenow', '1')
                    await rail.press('End')
                    await expect(rail).to_have_attribute('aria-valuenow', str(count))
                    await rail.press('ArrowUp')
                    await expect(rail).to_have_attribute('aria-valuenow', str(count - 1))
                    # Reading changes outside the rail must discard the old keyboard/hover position.
                    await page.evaluate('railFixture.jump(100)')
                    await expect(rail).to_have_attribute('aria-valuenow', '101')
                    await rail.press('ArrowDown')
                    await expect(rail).to_have_attribute('aria-valuenow', '102')
                    box = await rail.bounding_box()
                    x, y = box['x'] + box['width'] / 2, box['y'] + box['height'] / 2
                    await page.mouse.move(x, y)
                    await expect(panel).to_be_visible()
                    # Reading buttons in the same viewport cannot cover the interactive preview.
                    assert await page.locator('.rail-preview-heading button').evaluate('''node => {
                        const r=node.getBoundingClientRect();
                        return node.contains(document.elementFromPoint(r.x+r.width/2,r.y+r.height/2));
                    }''')
                    selected = int((await counter.inner_text()).split('/')[0])
                    calls_before = await page.evaluate('railFixture.calls.length')
                    await page.mouse.wheel(0, 100)
                    await expect(counter).to_have_text(f'{selected + 1} / {count}')
                    assert await page.evaluate('railFixture.calls.length') == calls_before
                    await page.get_by_role('button', name=f'跳转到第 {selected + 1} 个对话位置', exact=True).click()
                    await expect(rail).to_have_attribute('aria-valuenow', str(selected + 1))
                    await page.get_by_role('button', name='预览下一轮', exact=True).click()
                    await expect(counter).to_have_text(f'{selected + 2} / {count}')
                    await page.get_by_role('button', name=f'跳转到第 {selected + 2} 个对话位置', exact=True).click()
                    await expect(rail).to_have_attribute('aria-valuenow', str(selected + 2))
                    # Same-round pointer movement emits only one jump for the entire gesture.
                    await page.mouse.move(x, y)
                    await page.evaluate('railFixture.calls.length = 0')
                    await page.mouse.down()
                    for n in range(12):
                        await page.mouse.move(x + n % 2, y)
                    await page.mouse.up()
                    assert await page.evaluate('railFixture.calls.length') == 1
                    # Release commits the final target, including a move past the track endpoint.
                    await page.mouse.down()
                    await page.mouse.move(x, box['y'] + box['height'] + 8, steps=8)
                    await page.mouse.up()
                    await expect(rail).to_have_attribute('aria-valuenow', str(count))
                    assert await page.evaluate('railFixture.calls.at(-1)') == f'u{count - 1}'
                    # Three indicators occupy separate horizontal lanes, even at adjacent rounds.
                    rects = await page.locator('.rail-tick.current,.rail-tick.cached,.rail-tick.hovered').evaluate_all(
                        '(nodes) => nodes.map(n => {const r=n.getBoundingClientRect();return {left:r.left,right:r.right}})')
                    assert len(rects) == 3
                    assert all(a['right'] <= b['left'] or b['right'] <= a['left']
                               for i, a in enumerate(rects) for b in rects[i + 1:])
                    bounds = await panel.bounding_box()
                    assert bounds['x'] >= 0 and bounds['y'] >= 0
                    assert bounds['x'] + bounds['width'] <= width + 1
                    assert bounds['y'] + bounds['height'] <= height - 180 + 1
                    assert await page.locator('.rail-nearby .selected').evaluate('''node => {
                        const row=node.getBoundingClientRect(), box=node.parentElement.getBoundingClientRect();
                        return row.top >= box.top-1 && row.bottom <= box.bottom+1;
                    }''')
                    assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth && document.documentElement.scrollHeight <= innerHeight')
                    assert await page.locator('.history-row').count() < 80
                    await page.screenshot(path=str(output / f'{label}-{count}.png'))
                    await rail.press('Escape')
                    await expect(panel).to_be_hidden()
                    results.append({'viewport': label, 'rounds': count, 'status': 'passed'})
                # Empty-to-populated mount still measures the actual track height.
                await page.evaluate('railFixture.seed(0)')
                await expect(rail).to_have_count(0)
                await page.evaluate('railFixture.seed(5000)')
                await expect(rail).to_be_visible()
                await expect(rail).to_have_css('height', f'{height - 180 - 64}px')
                if label == 'mobile':
                    box = await rail.bounding_box()
                    await page.touchscreen.tap(box['x'] + box['width'] / 2, box['y'] + box['height'] / 2)
                    await expect(panel).to_be_visible()
                    selected = int((await counter.inner_text()).split('/')[0])
                    await page.get_by_role('button', name='预览下一轮', exact=True).tap()
                    await page.get_by_role('button', name=f'跳转到第 {selected + 1} 个对话位置', exact=True).tap()
                    await expect(rail).to_have_attribute('aria-valuenow', str(selected + 1))
                if label == 'desktop1920':
                    # Many moves in one frame are coalesced; pointer-up wins before that frame.
                    box = await rail.bounding_box()
                    x, y = box['x'] + box['width'] / 2, box['y'] + box['height'] / 2
                    await page.mouse.move(x, y)
                    await page.mouse.down()
                    await page.evaluate('railFixture.calls.length = 0')
                    await rail.evaluate('''node => {
                        const box=node.getBoundingClientRect();
                        for(let i=0;i<30;i++) node.dispatchEvent(new PointerEvent('pointermove',
                            {pointerId:1,clientX:box.x+12,clientY:box.y+20+i}));
                        node.dispatchEvent(new PointerEvent('pointerup',
                            {pointerId:1,clientX:box.x+12,clientY:box.bottom}));
                    }''')
                    await page.mouse.up()
                    await expect(rail).to_have_attribute('aria-valuenow', '5000')
                    assert await page.evaluate('railFixture.calls') == ['u4999']
                    await page.mouse.move(x, y)
                    await page.mouse.down()
                    await page.evaluate('railFixture.calls.length = 0')
                    await rail.evaluate('''node => {
                        const box=node.getBoundingClientRect();
                        node.dispatchEvent(new PointerEvent('pointermove', {pointerId:1,clientY:box.top}));
                        node.dispatchEvent(new PointerEvent('pointercancel', {pointerId:1}));
                    }''')
                    await page.mouse.up()
                    await page.evaluate('new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))')
                    assert await page.evaluate('railFixture.calls.length') == 0
                    # A pointer preview must not determine the next keyboard step.
                    await page.evaluate('railFixture.jump(100)')
                    await expect(rail).to_have_attribute('aria-valuenow', '101')
                    await page.mouse.move(x, y + 10)
                    await rail.press('ArrowDown')
                    await expect(rail).to_have_attribute('aria-valuenow', '102')
                    await rail.press('Escape')
                    await page.locator('.fixture-scroller').focus()
                    await page.mouse.move(x, y + 20)
                    await expect(panel).to_be_visible()
                    await page.mouse.move(10, height - 10)
                    await expect(panel).to_be_hidden()
                # Short histories and the single opening message retain direct positioning.
                await page.evaluate('railFixture.seed(1)')
                await rail.focus()
                await rail.press('End')
                await expect(rail).to_have_attribute('aria-valuenow', '1')
                await expect(page.get_by_role('button', name='预览下一轮', exact=True)).to_be_disabled()
                await context.close()
            assert not errors, errors
            await browser.close()
            (output / 'results.json').write_text(json.dumps(results, indent=2), encoding='utf-8')
            print(json.dumps(results))
    finally:
        process.terminate()
        process.wait(timeout=10)


if __name__ == '__main__':
    asyncio.run(main())
