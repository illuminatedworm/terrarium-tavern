"""Browser realm boundary for isolated card scripts.

ScriptRuntime owns script orchestration; a backend only supplies isolated pages.
Desktop uses Playwright Chromium. A backend must keep these guarantees:

- Each context is an isolated group with no service workers and no network.
  Every page request, including synchronous XHR, goes to its ``Resource``;
  ``None`` fails the request, a result is served uncached and readable by its pages.
- ``open_page`` exposes ``__tavernHost(method, args)`` before parsing the document,
  runs inline scripts in order and returns after the load event.
- Pages never receive host capabilities or model keys beyond that host function.
"""

from collections.abc import Awaitable, Callable
from typing import Any, Protocol

Host = Callable[[str, Any], Awaitable[Any]]
# (url, method) -> (body, mime type), or None to fail the request.
Resource = Callable[[str, str], tuple[str | bytes, str] | None]


def default_backend(*, tools: bool = False) -> "RealmBackend":
    """Keep platform selection at the browser boundary, before importing a driver.

    ``tools`` marks Card Author validation and dependency bundling. Desktop backends
    each own a browser; Android shares one process per kind, so tools get their own.
    """
    import os
    import sys

    if sys.platform == "android" or os.environ.get("TAVERN_PLATFORM") == "android":
        from tavern.scripting.android_realm import AndroidBackend

        return AndroidBackend(tools=tools)
    from tavern.scripting.playwright_realm import PlaywrightBackend

    return PlaywrightBackend()


class RealmPage(Protocol):
    async def evaluate(self, script: str, arg: Any = None) -> Any:
        """Evaluate an expression, or call function source with ``arg``; await its JSON result.

        Script exceptions propagate as the backend's own error type.
        """

    async def add_script(self, source: str) -> None:
        """Run a classic script in the page's global scope."""

    async def texts(self, selector: str) -> list[str]:
        """Return the textContent of every element matching a CSS selector."""

    async def close(self) -> None: ...


class RealmContext(Protocol):
    async def open_page(
        self, html: str, host: Host, on_error: Callable[[str], None] | None = None
    ) -> RealmPage:
        """Load a full document; ``on_error`` receives uncaught page errors."""

    async def close(self) -> None:
        """Close the context and every page it opened."""


class RealmBackend(Protocol):
    async def new_context(self, resource: Resource) -> RealmContext: ...

    async def close(self) -> None:
        """Release the browser; later contexts start it again."""
