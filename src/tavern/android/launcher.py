"""Run the local API inside HostService's dedicated Python thread."""

import asyncio
import errno
import os
import socket
from pathlib import Path

import uvicorn

_loop = None
_server = None
_app = None


def _shutdown(server, app):
    # SSE requests otherwise keep uvicorn waiting before lifespan can close these services.
    for service in (app.state.service, app.state.editor, app.state.advisor):
        for task in service.tasks.values():
            if not task.done():
                task.cancel()
    server.should_exit = True


def request_shutdown():
    loop, server, app = _loop, _server, _app
    if loop is not None:
        try:
            loop.call_soon_threadsafe(_shutdown, server, app)
        except RuntimeError:
            # Service destruction can race asyncio.run closing its loop after a normal exit.
            if not loop.is_closed():
                raise


def main():
    from java import jclass

    from tavern.application.api import create_app

    if not os.environ.get("TAVERN_STARTUP_TOKEN"):
        raise RuntimeError("Android 本地服务缺少启动凭据")
    root = Path(os.environ["TAVERN_DATA"])
    root.mkdir(parents=True, exist_ok=True)
    native = jclass("com.terrarium.tavern.AndroidPlatform")

    class AndroidServer(uvicorn.Server):
        async def startup(self, sockets=None):
            global _loop
            _loop = asyncio.get_running_loop()
            await super().startup(sockets)
            if self.started:
                native.backendReady(sockets[0].getsockname()[1])

    # Pick a random available port only on the first launch. WebView localStorage is
    # origin-scoped, so changing it later would hide the user's reading/sidebar preferences.
    port = native.getServerPort()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        # The previous run's closed connections hold this fixed port in TIME_WAIT for a while.
        # Like uvicorn's own sockets, allow that; Linux still rejects another live listener.
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind(("127.0.0.1", port))
        except OSError as error:
            if port and error.errno == errno.EADDRINUSE:
                raise RuntimeError(
                    f"本地端口 {port} 已被占用，请关闭冲突应用或重启设备后重试；原端口及界面偏好已保留"
                ) from error
            raise
        if not port:
            native.rememberServerPort(listener.getsockname()[1])
        app = create_app(root)
        global _app, _server, _loop
        _app = app
        server = _server = AndroidServer(
            uvicorn.Config(
                app,
                host="127.0.0.1",
                port=listener.getsockname()[1],
                loop="asyncio",
                http="h11",
                ws="none",
                access_log=False,
                timeout_graceful_shutdown=5,
            )
        )
        app.state.shutdown_callback = lambda: _shutdown(server, app)
        try:
            # Retain the bound socket until uvicorn owns it; there is no free-port race.
            server.run(sockets=[listener])
        finally:
            _loop = _server = _app = None
