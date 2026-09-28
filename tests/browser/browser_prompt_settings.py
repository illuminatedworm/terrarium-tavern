"""Prompt save/restore/leave guards are covered by the settings acceptance suite."""
import asyncio

from tests.browser.browser_settings_redesign import main

if __name__ == '__main__':
    asyncio.run(main())
