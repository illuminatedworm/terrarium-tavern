"""Owned Chromium app window; untrusted cards cannot choose their isolation policy."""

import asyncio
import os
import shutil
import socket
import subprocess
import webbrowser
from collections.abc import Awaitable, Callable
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from tavern.scripting.network_guard import NETWORK_GUARD


class DesktopWindow:
    def __init__(self, profile: Path, url: str, *, headless: bool = False):
        self.profile = profile
        self.url = url
        self.headless = headless
        self.closed = asyncio.Event()
        self.playwright = None
        self.context = None
        self.page = None

    async def open(self):
        from playwright.async_api import async_playwright

        self.playwright = await async_playwright().start()
        self.context = await self.playwright.chromium.launch_persistent_context(
            self.profile,
            headless=self.headless,
            no_viewport=True,
            chromium_sandbox=True,
            service_workers="block",
            args=[
                "--app=about:blank",
                "--dns-prefetch-disable",
                "--disk-cache-size=67108864",
                "--media-cache-size=16777216",
            ],
        )
        self.context.on("close", lambda _: self.closed.set())
        # With no listener Playwright automatically dismisses dialogs, including
        # the settings editor's unsaved-changes confirmation. Leave native UI to
        # the user; cards cannot show dialogs through their iframe sandbox.
        self.context.on("dialog", lambda _: None)
        await self.context.add_init_script(NETWORK_GUARD)
        origin = urlsplit(self.url)[:2]

        async def route(request_route):
            request = request_route.request
            target = urlsplit(request.url)
            if target[:2] == origin:
                await request_route.continue_()
                return
            await request_route.abort()
            # A card's subframe navigation is denied without opening another app.
            # Only top-level UI navigation may open the system browser.
            if (
                request.is_navigation_request()
                and request.frame.parent_frame is None
                and target.scheme in {"http", "https"}
            ):
                await asyncio.to_thread(webbrowser.open, request.url)
                if request.frame.page is not self.page:
                    await request.frame.page.close()

        await self.context.route("**/*", route)
        self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
        self.page.on("close", lambda _: self.closed.set())
        # Use Chromium's download UI and normal filenames, not automation files
        # in Playwright's temporary directory, which disappear on context close.
        cdp = await self.context.new_cdp_session(self.page)
        await cdp.send("Browser.setDownloadBehavior", {"behavior": "default"})
        await cdp.detach()
        await self.page.goto(self.url, wait_until="domcontentloaded")

    async def close(self):
        try:
            if self.context is not None:
                await self.context.close()
                self.context = None
        finally:
            if self.playwright is not None:
                await self.playwright.stop()
                self.playwright = None


async def serve_window(server, ready: asyncio.Event, shutdown, profile: Path, url: str):
    window = DesktopWindow(profile, url)
    serving = asyncio.create_task(server.serve())
    starting = asyncio.create_task(ready.wait())
    closing = None
    try:
        finished, _ = await asyncio.wait((serving, starting), return_when=asyncio.FIRST_COMPLETED)
        if serving in finished:
            await serving
            return
        await window.open()
        closing = asyncio.create_task(window.closed.wait())
        await asyncio.wait((serving, closing), return_when=asyncio.FIRST_COMPLETED)
    finally:
        shutdown()
        starting.cancel()
        if closing is not None:
            closing.cancel()
        try:
            await serving
        finally:
            await window.close()
            await asyncio.gather(starting, *([closing] if closing else []), return_exceptions=True)


async def with_dev_frontend(run: Callable[[], Awaitable[None]]):
    """Own Vite and its esbuild children together with the application window."""
    root = Path(__file__).resolve().parents[2]
    node = shutil.which("node")
    vite = root / "node_modules/vite/bin/vite.js"
    if not node or not vite.is_file():
        raise RuntimeError("请先运行 setup，准备 Node 和前端依赖")
    for port in (8000, 5173):
        with socket.socket() as probe:
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                raise RuntimeError(f"端口 {port} 已被使用，请先停止旧服务")
    process = await asyncio.create_subprocess_exec(
        node,
        str(vite),
        "--host",
        "127.0.0.1",
        "--strictPort",
        cwd=root,
        **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}),
    )
    exited = asyncio.create_task(process.wait())
    application = None
    try:
        async with httpx.AsyncClient(trust_env=False, timeout=1) as client:
            async with asyncio.timeout(20):
                while True:
                    if exited.done():
                        raise RuntimeError(f"Vite 启动失败（退出码 {exited.result()}）")
                    try:
                        response = await client.get("http://127.0.0.1:5173/")
                        response.raise_for_status()
                        break
                    except httpx.ConnectError:
                        # The owned child is starting, but has not bound its port yet.
                        await asyncio.sleep(0.1)
        application = asyncio.create_task(run())
        finished, _ = await asyncio.wait((application, exited), return_when=asyncio.FIRST_COMPLETED)
        if exited in finished and not application.done():
            application.cancel()
            raise RuntimeError(f"Vite 意外退出（退出码 {exited.result()}）")
        await application
    finally:
        if application is not None and not application.done():
            application.cancel()
            await asyncio.gather(application, return_exceptions=True)
        if process.returncode is None:
            if os.name == "nt":
                # Terminating only node can strand its esbuild subprocess on Windows.
                cleanup = await asyncio.create_subprocess_exec(
                    "taskkill",
                    "/PID",
                    str(process.pid),
                    "/T",
                    "/F",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                await cleanup.wait()
            else:
                process.terminate()
        await exited
