"""User-level actions shared by the model and reasoning menu acceptance tests."""
import json

from playwright.async_api import expect


async def menu_popup(button):
    if await button.get_attribute('aria-expanded') != 'true':
        await button.click()
    popup = button.locator('..').locator('[popover]')
    await expect(popup).to_be_visible()
    return popup


async def menu_option(button, value):
    popup = await menu_popup(button)
    return popup.locator(f'[role="option"][data-value={json.dumps(value)}]')


async def choose_menu(button, value=None, *, label=None):
    popup = await menu_popup(button)
    option = (popup.get_by_role('option', name=label, exact=True) if label is not None
              else popup.locator(f'[role="option"][data-value={json.dumps(value)}]'))
    await option.click()
    await expect(popup).to_be_hidden()


async def choose_model(button, value):
    popup = await menu_popup(button)
    search = popup.get_by_role('combobox', name='搜索或输入模型 ID', exact=True)
    await expect(search).to_have_value('')
    previous = await button.get_attribute('data-value')
    await search.fill(value)
    await expect(button).to_have_attribute('data-value', previous)
    await popup.locator(f'[role="option"][data-value={json.dumps(value)}]').click()
    await expect(popup).to_be_hidden()
    await expect(button).to_have_attribute('data-value', value)
