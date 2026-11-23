"""One disposable QuickJS/WASM calculation, independent of every card and turn."""

import asyncio
import json
import os
import subprocess
from contextlib import suppress
from pathlib import Path
from weakref import WeakValueDictionary

from tavern.android.platform import is_android

ENGINE = Path(__file__).parents[1] / "static/calculation/engine.js"
WORKER = Path(__file__).with_name("calculation_worker.mjs")
# HostService can restart its event loop in the same Python interpreter. Only
# active or waiting calls keep their loop's shared renderer queue alive.
_android_locks: WeakValueDictionary[asyncio.AbstractEventLoop, asyncio.Lock] = WeakValueDictionary()


class CalculationError(ValueError):
    """A correctable error in model-supplied code or its output."""


async def _desktop(payload: dict) -> dict:
    from playwright._impl._driver import compute_driver_executable

    node, _ = compute_driver_executable()
    launch = asyncio.create_task(
        asyncio.create_subprocess_exec(
            node,
            str(WORKER),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    )
    try:
        worker = await asyncio.shield(launch)
        output, diagnostic = await worker.communicate(json.dumps(payload).encode())
        if worker.returncode:
            raise RuntimeError("运算进程异常退出：" + diagnostic.decode("utf-8", errors="replace"))
        return json.loads(output)
    finally:
        # Cancellation can arrive before spawn returns its handle.
        worker = await launch
        if worker.returncode is None:
            with suppress(ProcessLookupError):
                worker.kill()
            await worker.communicate()


async def _android(payload: dict) -> dict:
    from tavern.scripting.android_realm import AndroidBackend
    from tavern.scripting.runtime import script_source

    async def no_host(method, args):
        raise ValueError("运算工具没有宿主接口")

    backend = AndroidBackend(purpose="calculation")
    try:
        context = await backend.new_context(lambda url, method: None)
        page = await context.open_page(
            '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; '
            "script-src 'unsafe-inline' 'unsafe-eval'\">",
            no_host,
        )
        await page.add_script(script_source(ENGINE))
        return await page.evaluate("input => tavernCalculate(input)", payload)
    finally:
        await backend.close()


async def calculate(code: str, summary: bool) -> dict:
    if not ENGINE.is_file():
        raise RuntimeError("运算与掷骰运行环境尚未构建，请运行 npm run build:sdk")
    payload = {"code": code, "summary": summary}
    try:
        # The guest has its own 1200 ms instruction deadline. The outer wall
        # deadline also covers host parsing, process startup and native work.
        if is_android():
            lock = _android_locks.setdefault(asyncio.get_running_loop(), asyncio.Lock())
            async with lock:
                async with asyncio.timeout(15):
                    result = await _android(payload)
        else:
            async with asyncio.timeout(15):
                result = await _desktop(payload)
    except TimeoutError:
        raise CalculationError("运算超过 15 秒，已中止本次调用") from None
    if "error" in result:
        raise CalculationError(result["error"])
    return result["data"]
