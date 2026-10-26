"""One pinned desktop dictionary, published only after verification; never installs Python code."""

import asyncio
import hashlib
import os
import time
import zipfile
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import TYPE_CHECKING, Callable

import httpx
from filelock import FileLock, Timeout
from starlette.requests import ClientDisconnect

from tavern.android.platform import is_android

if TYPE_CHECKING:
    from tavern.search.retrieval_config import Network

VERSION = "20260723"
FILENAME = f"sudachidict_core-{VERSION}-py3-none-any.whl"
URL = (
    "https://files.pythonhosted.org/packages/46/fe/"
    "68a146fced55319af40d25a4fe19b94c3a988406ce4674a8b2f0237fbc9f/" + FILENAME
)
WHEEL_SHA256 = "b3869ce6b12b4bfa09575dc19030703bb669ab41bac12a74cafcbb28c6be2498"
WHEEL_BYTES = 72275897
DIC_SHA256 = "53fa281d11eef3769712fe1c3c892117338f9892bee6daf4dad51daa5281bb6f"
DIC_BYTES = 217466039
DIC_MEMBER = "sudachidict_core/resources/system.dic"
ACTIVE = {"checking", "downloading", "importing", "verifying"}
CHUNK = 1024 * 1024
_current: ContextVar[tuple["JapaneseDictionary", asyncio.AbstractEventLoop, bool] | None] = ContextVar(
    "japanese_dictionary", default=None
)


class DictionaryUnavailable(ValueError):
    """Resource failure must not become a partial BM25 result or a different tokenizer."""


def dictionary_path() -> str:
    if is_android():
        return "core"  # APK extraction and its bundled dictionary stay unchanged.
    binding = _current.get()
    if binding is None:
        raise DictionaryUnavailable("日文词典尚未准备；请在设置 → 通用 → 本地资源准备或导入词典")
    manager, loop, automatic = binding
    if manager.is_ready():
        return str(manager.path)
    if automatic and not manager.stopping and manager.state not in {"error", "cancelled"}:
        # to_thread copies ContextVars. Schedule on its owning loop, never block a
        # tokenizer/index lock on a network operation or start a second downloader.
        loop.call_soon_threadsafe(manager.start)
    raise DictionaryUnavailable(manager.unavailable_message())


@contextmanager
def dictionary_read_only():
    binding = _current.get()
    token = _current.set((*binding[:2], False) if binding else None)
    try:
        yield
    finally:
        _current.reset(token)


class JapaneseDictionary:
    def __init__(self, root: Path, network: Callable[[], "Network"]):
        self.root = (root / "resources" / "sudachi-core" / VERSION).resolve()
        self.path = self.root / "system.dic"
        self.network = network
        self.state = "unchecked" if self.path.is_file() else "missing"
        self.error = ""
        self.completed_bytes = 0
        self.total_bytes = 0
        self.signature = None
        self.task: asyncio.Task | None = None
        self.observers: set[asyncio.Queue] = set()
        self.last_notice = 0.0
        self.stopping = False

    @contextmanager
    def bind(self, *, automatic=True):
        token = _current.set((self, asyncio.get_running_loop(), automatic))
        try:
            yield
        finally:
            _current.reset(token)

    def is_ready(self):
        if self.signature is None or not self.path.is_file():
            return False
        stat = self.path.stat()
        return self.signature == (stat.st_size, stat.st_mtime_ns)

    def unavailable_message(self):
        if self.state in {"error", "cancelled"}:
            return "日文词典准备失败或已取消；请在设置 → 通用 → 本地资源查看原因并重试，再重试本次操作"
        if self.state not in ACTIVE:
            return "日文词典尚未准备；请在设置 → 通用 → 本地资源准备或导入词典，再重试本次操作"
        return "日文词典正在准备；请在设置 → 通用 → 本地资源查看进度，完成后重试本次操作"

    def status(self):
        if self.state == "ready" and not self.is_ready():
            self.signature = None
            self.state = "unchecked" if self.path.is_file() else "missing"
        network = self.network()
        return {
            "platform": "android" if is_android() else "desktop",
            "state": "bundled" if is_android() else self.state,
            "version": VERSION,
            "source": URL,
            "filename": FILENAME,
            "sha256": WHEEL_SHA256,
            "dictionary_sha256": DIC_SHA256,
            "download_bytes": WHEEL_BYTES,
            "dictionary_bytes": DIC_BYTES,
            "path": "APK 内 sudachidict_core/resources/system.dic" if is_android() else str(self.path),
            "completed_bytes": self.completed_bytes,
            "total_bytes": self.total_bytes,
            "error": self.error,
            "proxy_enabled": bool(network.hf_proxy and network.proxy),
        }

    def emit(self, *, force=False):
        if self.stopping:
            return
        now = time.monotonic()
        if not force and now - self.last_notice < 0.2:
            return
        self.last_notice = now
        value = self.status()
        for queue in self.observers:
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(value)

    def phase(self, state, total=0):
        self.state, self.completed_bytes, self.total_bytes = state, 0, total
        self.emit(force=True)

    async def events(self):
        queue = asyncio.Queue(maxsize=1)
        self.observers.add(queue)
        try:
            yield self.status()
            while True:
                value = await queue.get()
                if value is None:
                    break
                yield value
        finally:
            self.observers.remove(queue)

    async def _copy(self, chunks, target, expected_bytes, expected_hash):
        digest = hashlib.sha256()
        count = 0
        with target.open("wb") as output:
            async for chunk in chunks:
                count += len(chunk)
                if count > expected_bytes:
                    raise ValueError("词典文件大小超出固定版本限制")
                output.write(chunk)
                digest.update(chunk)
                self.completed_bytes = count
                self.emit()
            output.flush()
            os.fsync(output.fileno())
        if count != expected_bytes or digest.hexdigest() != expected_hash:
            raise ValueError("词典文件大小或 SHA-256 不匹配；请使用指定版本的完整文件")

    async def _chunks(self, stream):
        while chunk := stream.read(CHUNK):
            yield chunk
            # Bounded disk/decompression work also gives cancellation a boundary.
            await asyncio.sleep(0)

    async def _verify_existing(self):
        self.phase("checking", DIC_BYTES)
        digest = hashlib.sha256()
        with self.path.open("rb") as stream:
            if self.path.stat().st_size != DIC_BYTES:
                raise ValueError("已有日文词典大小不匹配，请重新下载或导入")
            async for chunk in self._chunks(stream):
                digest.update(chunk)
                self.completed_bytes += len(chunk)
                self.emit()
        if digest.hexdigest() != DIC_SHA256:
            raise ValueError("已有日文词典 SHA-256 不匹配，请重新下载或导入")

    async def _extract(self, wheel, pending):
        self.phase("verifying", DIC_BYTES)
        with zipfile.ZipFile(wheel) as archive:
            info = archive.getinfo(DIC_MEMBER)
            if info.file_size != DIC_BYTES:
                raise ValueError("词典归档内的文件大小不匹配")
            # Only a named data member is read; no wheel code is installed or executed.
            with archive.open(info) as stream:
                await self._copy(self._chunks(stream), pending, DIC_BYTES, DIC_SHA256)

    async def _download(self, wheel, pending):
        self.phase("downloading", WHEEL_BYTES)
        network = self.network()
        proxy = network.proxy if network.hf_proxy and network.proxy else None
        async with httpx.AsyncClient(proxy=proxy, trust_env=False, timeout=30) as client:
            async with client.stream("GET", URL) as response:
                response.raise_for_status()
                await self._copy(response.aiter_bytes(CHUNK), wheel, WHEEL_BYTES, WHEEL_SHA256)
        await self._extract(wheel, pending)

    def start(self, *, retry=False, stream=None, size=None, kind="wheel"):
        if self.stopping:
            raise DictionaryUnavailable("服务正在退出，请重新启动后准备日文词典")
        if is_android():
            raise ValueError("Android 使用 APK 内置日文词典")
        if self.task is not None and not self.task.done():
            if stream is not None:
                raise ValueError("词典正在准备，请先等待或取消当前任务")
            return self.task
        if self.is_ready():
            if stream is not None:
                raise ValueError("日文词典已就绪；当前文件未导入，无需替换固定版本")
            return self.task
        if self.state in {"error", "cancelled"} and not retry:
            return self.task
        replace_bad = self.state == "error"
        self.error = ""
        self.phase("importing" if stream is not None else "checking")

        async def run():
            lock = FileLock(str(self.root / ".prepare.lock"))
            wheel = self.root / "download.whl.part"
            pending = self.root / "system.dic.part"
            acquired = False
            try:
                self.root.mkdir(parents=True, exist_ok=True)
                try:
                    lock.acquire(timeout=0)
                    acquired = True
                except Timeout:
                    raise ValueError("另一进程正在准备此词典，请在其完成后重试") from None
                if stream is not None:
                    expected_bytes, expected_hash = (
                        (WHEEL_BYTES, WHEEL_SHA256) if kind == "wheel" else (DIC_BYTES, DIC_SHA256)
                    )
                    if size is not None and size != expected_bytes:
                        raise ValueError("导入文件大小与固定词典版本不符")
                    self.phase("importing", expected_bytes)
                    await self._copy(
                        stream, wheel if kind == "wheel" else pending, expected_bytes, expected_hash
                    )
                    if kind == "wheel":
                        await self._extract(wheel, pending)
                    pending.replace(self.path)
                elif self.path.is_file() and not replace_bad:
                    await self._verify_existing()
                else:
                    await self._download(wheel, pending)
                    pending.replace(self.path)
                stat = self.path.stat()
                self.signature = (stat.st_size, stat.st_mtime_ns)
                self.phase("ready")
            except asyncio.CancelledError:
                self.phase("cancelled")
            except ClientDisconnect:
                self.error = "词典导入连接已断开，尚未完成的文件未生效；请重新导入"
                self.phase("cancelled")
            except Exception as error:
                # This is the resource job boundary: retain the actual failure for
                # the UI, while tokenizer/model-facing errors contain no paths/hashes.
                self.error = str(error).strip() or type(error).__name__
                self.phase("error")
            finally:
                if acquired:
                    try:
                        # These two names belong exclusively to this job. Interrupted
                        # leftovers are never accepted and are overwritten on retry.
                        wheel.unlink(missing_ok=True)
                        pending.unlink(missing_ok=True)
                    finally:
                        lock.release()

        self.task = asyncio.create_task(run())
        return self.task

    async def ensure(self):
        if is_android() or self.is_ready():
            return
        task = self.start()
        if task is not None:
            # A cancelled story must not cancel a download shared with other stories.
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                if asyncio.current_task().cancelling():
                    raise
                raise DictionaryUnavailable(self.unavailable_message()) from None
        if not self.is_ready():
            raise DictionaryUnavailable(self.unavailable_message())

    async def run(self, function, *args, **kwargs):
        """Retry only the pure lexical operation, after its first actual Japanese use."""
        with self.bind():
            try:
                return await asyncio.to_thread(function, *args, **kwargs)
            except DictionaryUnavailable:
                await self.ensure()
                return await asyncio.to_thread(function, *args, **kwargs)

    async def cancel(self):
        if self.task is not None and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            if self.state in ACTIVE:
                self.phase("cancelled")

    async def close(self):
        self.stop()
        await self.cancel()

    def stop(self):
        # Uvicorn drains SSE before lifespan cleanup, so the host calls this
        # together with cancel_producers rather than waiting for lifespan exit.
        self.stopping = True
        for queue in self.observers:
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(None)
        if self.task is not None and not self.task.done():
            self.task.cancel()
