"""Atomic local credentials. Secrets never enter the story/settings database."""

import ctypes
import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

from filelock import FileLock, Timeout


def _dpapi(data: bytes, *, decrypt=False) -> bytes:
    """Current-user DPAPI, with UI disabled; never fall back to plaintext."""
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    source, target = Blob(len(data), buffer), Blob()
    if decrypt:
        fn = crypt.CryptUnprotectData
        fn.argtypes = [
            ctypes.POINTER(Blob),
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(Blob),
        ]
        args = [ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)]
    else:
        fn = crypt.CryptProtectData
        fn.argtypes = [
            ctypes.POINTER(Blob),
            wintypes.LPCWSTR,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(Blob),
        ]
        args = [ctypes.byref(source), "Terrarium Tavern ChatGPT", None, None, None, 1, ctypes.byref(target)]
    fn.restype = wintypes.BOOL
    if not fn(*args):
        raise ValueError("无法保护或读取 ChatGPT 凭据，请使用原操作系统用户重新登录")
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        kernel.LocalFree(target.data)


class CredentialVault:
    def __init__(self, root: Path):
        self.directory = root / ".chatgpt"
        self.path = self.directory / "accounts.dat"

    def prepare(self):
        if self.directory.is_symlink() or self.path.is_symlink():
            raise ValueError("ChatGPT 凭据目录不能使用符号链接")
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if os.name != "nt":
            self.directory.chmod(0o700)
            if self.path.exists():
                self.path.chmod(0o600)

    @contextmanager
    def locked(self):
        self.prepare()
        try:
            # A fresh lock object avoids same-thread asyncio tasks re-entering it.
            with FileLock(self.directory / "accounts.lock", timeout=5, mode=0o600):
                yield
        except Timeout:
            raise ValueError("ChatGPT 凭据正在更新，请稍后重试") from None

    def load(self):
        if not self.path.exists():
            return {"version": 1, "host_id": None, "accounts": {}, "welcomed": False}
        try:
            raw = self.path.read_bytes()
            if os.name == "nt":
                if not raw.startswith(b"DPAPI\0"):
                    raise ValueError("凭据保护格式不匹配")
                raw = _dpapi(raw[6:], decrypt=True)
            else:
                if not raw.startswith(b"JSON\0"):
                    raise ValueError("凭据保护格式不匹配")
                raw = raw[5:]
            value = json.loads(raw)
            if value.get("version") != 1 or not isinstance(value.get("accounts"), dict):
                raise ValueError("凭据版本不匹配")
            return value
        except (OSError, ValueError, TypeError):
            raise ValueError("无法读取受保护的 ChatGPT 连接，请检查原用户凭据存储；未覆盖原文件") from None

    def save(self, value):
        self.prepare()
        raw = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
        raw = b"DPAPI\0" + _dpapi(raw) if os.name == "nt" else b"JSON\0" + raw
        descriptor, name = tempfile.mkstemp(prefix="accounts-", suffix=".tmp", dir=self.directory)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                if os.name != "nt":
                    os.fchmod(stream.fileno(), 0o600)
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.path)
        finally:
            Path(name).unlink(missing_ok=True)
