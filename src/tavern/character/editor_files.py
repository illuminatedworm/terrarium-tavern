"""Bounded reads of external authoring references, separate from internal card reads."""
import asyncio
import codecs
import io
import json
import os
import re
import stat
import subprocess
import sys
import zipfile
from contextlib import contextmanager
from pathlib import Path, PureWindowsPath
from typing import BinaryIO, Iterator

MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_TEXT_CHARS = 20_000
MAX_ZIP_ENTRIES = 4096
MAX_EXPANDED_BYTES = 128 * 1024 * 1024
MAX_DOCUMENT_CHARS = 8_000_000
CONVERSION_TIMEOUT = 30
DOCUMENT_LIMITATIONS = (
    "这是转换后 Markdown 的字符分页，可能跨越段落或表格；用 next_offset 继续。"
    "连续分页期间请勿修改源文件。仅提取文字和表格，不执行宏、公式重算或图片 OCR；"
    "XLSX 使用已保存的公式缓存值，无缓存的公式可能没有结果。"
)
_BINARY_CONTROLS = re.compile(r"[\x00-\x08\x0b\x0e-\x1f\x7f]")


def local_path(value: str) -> Path:
    """Disallow URL/UNC/device namespaces, including resolved symlink targets."""
    if not value or "\0" in value or value.startswith(("\\\\", "//", "\\??\\")) or "://" in value:
        raise ValueError("path 必须是绝对本地普通文件路径，不支持 URL、网络共享或设备路径。")
    path = Path(value)
    if not path.is_absolute():
        raise ValueError("path 必须是后端电脑上的绝对文件路径。")
    if os.name == "nt" and (PureWindowsPath(value).is_reserved() or ":" in str(path)[2:]):
        raise ValueError("不支持设备路径或文件的备用数据流。")
    resolved = path.resolve(strict=True)
    if str(resolved).startswith(("\\\\", "//")):
        raise ValueError("不支持网络共享或设备路径。")
    _check_stat(resolved.stat())
    return resolved


def _check_stat(info: os.stat_result) -> None:
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("只能读取普通文件，不支持目录或设备。")
    if info.st_size > MAX_FILE_BYTES:
        raise ValueError("源文件超过 32 MiB，请先拆分文件。")


@contextmanager
def open_local(value: str) -> Iterator[tuple[Path, BinaryIO]]:
    try:
        path = local_path(value)
        with path.open("rb") as stream:
            _check_stat(os.fstat(stream.fileno()))
            yield path, stream
    except FileNotFoundError:
        raise ValueError("文件不存在，请核对后端电脑上的绝对路径。") from None
    except PermissionError:
        raise ValueError("没有权限读取该文件。") from None
    except OSError:
        raise ValueError("无法读取该本地文件，请检查路径、文件占用和访问权限。") from None


class _BoundedReader(io.RawIOBase):
    """Enforce the byte ceiling even if a file grows after fstat()."""

    def __init__(self, source: BinaryIO):
        self.source = source
        self.consumed = 0

    def readable(self) -> bool:
        return True

    def readinto(self, buffer) -> int:
        chunk = self.source.read(min(len(buffer), MAX_FILE_BYTES - self.consumed + 1))
        self.consumed += len(chunk)
        if self.consumed > MAX_FILE_BYTES:
            raise ValueError("源文件超过 32 MiB，请先拆分文件。")
        buffer[:len(chunk)] = chunk
        return len(chunk)


def _encoding(header: bytes, requested: str | None) -> str:
    if requested:
        try:
            info = codecs.lookup(requested)
            if not info._is_text_encoding:
                raise LookupError
            return info.name
        except LookupError:
            raise ValueError("encoding 不是支持的文本编码；可使用 utf-8、utf-16 或 gb18030。") from None
    for bom, encoding in ((codecs.BOM_UTF32_LE, "utf-32"), (codecs.BOM_UTF32_BE, "utf-32"),
                          (codecs.BOM_UTF8, "utf-8-sig"), (codecs.BOM_UTF16_LE, "utf-16"),
                          (codecs.BOM_UTF16_BE, "utf-16")):
        if header.startswith(bom):
            return encoding
    return "utf-8"


def read_text_file(path: str, offset: int = 1, limit: int = 200, encoding: str | None = None) -> dict:
    with open_local(path) as (resolved, source):
        selected_encoding = _encoding(source.read(4), encoding)
        source.seek(0)
        try:
            with io.TextIOWrapper(io.BufferedReader(_BoundedReader(source)),
                                  encoding=selected_encoding, errors="strict", newline="") as text:
                for _ in range(offset - 1):
                    if not text.readline():
                        break
                lines, chars, has_more = [], 0, False
                for index in range(limit):
                    line = text.readline(MAX_TEXT_CHARS + 1)
                    if not line:
                        break
                    if _BINARY_CONTROLS.search(line):
                        raise ValueError("文件包含二进制控制字符，不能作为纯文本读取；DOCX/XLSX 请用 read_document。")
                    if len(line) > MAX_TEXT_CHARS:
                        # Return any preceding complete lines first. The offending line remains the next page.
                        if lines:
                            has_more = True
                            break
                        raise ValueError(f"第 {offset + index} 行超过 20,000 字符，请先拆分该行。")
                    if chars + len(line) > MAX_TEXT_CHARS:
                        has_more = True
                        break
                    lines.append(line)
                    chars += len(line)
                else:
                    has_more = bool(text.read(1))
        except UnicodeError:
            raise ValueError("文本解码失败，请用 encoding 指定正确编码（如 gb18030）；二进制文档请用 read_document。") from None
    return {"path": str(resolved), "encoding": selected_encoding, "text": "".join(lines), "offset": offset,
            "returned_lines": len(lines), "has_more": has_more,
            "next_offset": offset + len(lines) if has_more else None}


def _check_archive(blob: bytes) -> None:
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_ZIP_ENTRIES:
                raise ValueError("Office 压缩包超过 4096 个条目，请先拆分文件。")
            if sum(item.file_size for item in entries) > MAX_EXPANDED_BYTES:
                raise ValueError("Office 解压总量超过 128 MiB，请先拆分文件。")
            if any(item.flag_bits & 1 for item in entries):
                raise ValueError("不支持加密的 Office 文件，请先另存为未加密文件。")
            # Validate CRC/actual decompression before a parser consumes the archive. Never extract to disk.
            expanded = 0
            for item in entries:
                with archive.open(item) as member:
                    while chunk := member.read(64 * 1024):
                        expanded += len(chunk)
                        if expanded > MAX_EXPANDED_BYTES:
                            raise ValueError("Office 解压总量超过 128 MiB，请先拆分文件。")
    except (zipfile.BadZipFile, NotImplementedError, RuntimeError):
        raise ValueError("Office 文件损坏、已加密或使用了不支持的压缩格式。") from None


def convert_document(path: str, offset: int, limit: int) -> dict:
    """Runs only in the disposable worker. Return only the selected page over stdout."""
    with open_local(path) as (resolved, stream):
        extension = resolved.suffix.lower()
        if extension not in {".docx", ".xlsx"}:
            raise ValueError("read_document 仅支持 .docx 和 .xlsx 文件。")
        blob = stream.read(MAX_FILE_BYTES + 1)
        if len(blob) > MAX_FILE_BYTES:
            raise ValueError("源文件超过 32 MiB，请先拆分文件。")
    _check_archive(blob)
    # Call only the selected converter: no format inference model, URL dispatch, plugins or model clients.
    from markitdown import StreamInfo
    from markitdown.converters import DocxConverter, XlsxConverter

    converter = DocxConverter() if extension == ".docx" else XlsxConverter()
    try:
        markdown = converter.convert(io.BytesIO(blob), stream_info=StreamInfo(extension=extension)).markdown
    except Exception:
        raise ValueError("Office 文件转换失败，文件可能损坏、已加密或包含不支持的内容。") from None
    if len(markdown) > MAX_DOCUMENT_CHARS:
        raise ValueError("转换后 Markdown 超过 800 万字符，请先拆分文件。")
    page = markdown[offset:offset + limit]
    has_more = offset + len(page) < len(markdown)
    return {"path": str(resolved), "format": extension[1:], "markdown": page, "offset": offset,
            "returned_chars": len(page), "total_chars": len(markdown), "has_more": has_more,
            "next_offset": offset + len(page) if has_more else None, "limitations": DOCUMENT_LIMITATIONS}


async def read_document(path: str, offset: int = 0, limit: int = 12_000) -> dict:
    # Spawn is shielded so cancellation during process creation cannot orphan a worker.
    launch = asyncio.create_task(asyncio.create_subprocess_exec(
        sys.executable, "-m", "tavern.character.editor_file_worker",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {})))
    process = None
    try:
        process = await asyncio.shield(launch)
        request = json.dumps({"path": path, "offset": offset, "limit": limit}).encode("utf-8")
        try:
            stdout, _ = await asyncio.wait_for(process.communicate(request), timeout=CONVERSION_TIMEOUT)
        except TimeoutError:
            raise ValueError("Office 转换超过 30 秒，已终止；请先拆分文件。") from None
        if process.returncode:
            raise ValueError("Office 转换进程异常退出，请检查文件或拆分后重试。")
        try:
            result = json.loads(stdout)
        except (ValueError, UnicodeError):
            raise ValueError("Office 转换进程未返回有效结果。") from None
        if "error" in result:
            raise ValueError(result["error"])
        return result
    finally:
        if process is None:
            process = await launch
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            # Drain pipes after killing too; wait() alone can hang on a full stdout buffer.
            await process.communicate()
        await process.wait()
