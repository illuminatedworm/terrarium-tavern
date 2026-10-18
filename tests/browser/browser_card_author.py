"""Compatibility entry point for the standalone Card Author browser suite."""
import asyncio

from tests.browser.browser_card_authoring import main

if __name__ == "__main__":
    asyncio.run(main())
