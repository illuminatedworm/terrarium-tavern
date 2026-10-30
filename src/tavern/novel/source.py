"""Immutable file copies with a sparse UTF-8 index; no whole-book database payload."""

import codecs
import json
import re
from pathlib import Path

MAX_BYTES = 64 * 1024 * 1024
BLOCK_CHARS = 4096
FILES = ("original", "text.utf8", "index.json")
ENCODINGS = {"utf-8", "utf-16", "gb18030", "big5"}


def directory(root: Path, source_id: str) -> Path:
    if not re.fullmatch(r"[a-f0-9]{32}", source_id):
        raise ValueError("原著文件标识无效")
    return root / "blobs" / "novels" / source_id


def import_file(root, source_id, stream, filename, encoding="utf-8"):
    if Path(filename).suffix.lower() not in {".txt", ".md", ".markdown"}:
        raise ValueError("原著仅支持 txt、md、markdown 纯文本文件")
    if encoding not in ENCODINGS:
        raise ValueError("请选择 UTF-8、UTF-16、GB18030 或 Big5 编码")
    first = stream.read(4)
    actual = "utf-16" if first.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)) else encoding
    if actual == "utf-8":
        actual = "utf-8-sig"
    decoder = codecs.getincrementaldecoder(actual)(errors="strict")
    folder = directory(root, source_id)
    folder.mkdir(parents=True, exist_ok=False)
    total_bytes, characters, pending = 0, 0, ""
    offsets = []
    with (folder / "original").open("wb") as original, (folder / "text.utf8").open("wb") as text:
        chunk = first
        while True:
            total_bytes += len(chunk)
            if total_bytes > MAX_BYTES:
                raise ValueError("原著文件不能超过 64 MiB")
            original.write(chunk)
            try:
                decoded = decoder.decode(chunk, final=not chunk)
            except UnicodeError as error:
                raise ValueError("文件解码失败，请选择正确编码后重新导入；不会替换乱码") from error
            if "\x00" in decoded:
                raise ValueError("文件含 NUL 字符，不是支持的纯文本")
            pending += decoded
            while len(pending) >= BLOCK_CHARS or not chunk and pending:
                block, pending = pending[:BLOCK_CHARS], pending[BLOCK_CHARS:]
                offsets.append(text.tell())
                text.write(block.encode("utf-8"))
                characters += len(block)
            if not chunk:
                break
            chunk = stream.read(65536)
    if not characters:
        raise ValueError("原著文件不能为空")
    (folder / "index.json").write_text(json.dumps(offsets), encoding="utf-8")
    return {"name": Path(filename).name, "encoding": actual, "bytes": total_bytes, "characters": characters}


class NovelText:
    def __init__(self, root, source):
        self.source = source
        self.folder = directory(root, source["id"])
        if not all((self.folder / name).is_file() for name in FILES):
            raise ValueError("原著参考文件缺失，请重新导入；不会改用同名文件")
        self.offsets = json.loads((self.folder / "index.json").read_text(encoding="utf-8"))

    def open_at(self, start):
        if not 0 <= start <= self.source["characters"]:
            raise ValueError("字符位置超出原著范围")
        handle = (self.folder / "text.utf8").open("r", encoding="utf-8", newline="")
        block = min(start // BLOCK_CHARS, len(self.offsets) - 1)
        handle.seek(self.offsets[block])
        handle.read(start - block * BLOCK_CHARS)
        return handle

    def read(self, start, length):
        with self.open_at(start) as handle:
            text = handle.read(length)
        end = start + len(text)
        return {
            "source": "N1",
            "name": self.source["name"],
            "kind": "excerpt",
            "start": start,
            "end": end,
            "total_characters": self.source["characters"],
            "text": text,
            "has_more": end < self.source["characters"],
            "next_start": end,
        }

    def search(self, query, start, limit, end=None):
        end = self.source["characters"] if end is None else end
        if not start <= end <= self.source["characters"]:
            raise ValueError("搜索范围无效：需要 start ≤ end ≤ 全文字符数")
        pattern = re.compile(re.escape(query), re.IGNORECASE)
        matches, total, position, carry = [], 0, start, ""
        with self.open_at(start) as handle:
            # The whole range is scanned so the total count tells the model how frequent the
            # term is; excerpts are only built for the returned page.
            while chunk := handle.read(min(65536, end - position)):
                block = carry + chunk
                base = position - len(carry)
                # Keep only enough overlap to find a literal spanning a block boundary.
                for match in pattern.finditer(block):
                    a, b = base + match.start(), base + match.end()
                    if b <= position:
                        continue
                    total += 1
                    if len(matches) < limit:
                        excerpt = self.read(max(start, a - 100), len(query) + 200)
                        matches.append(
                            {
                                "start": a,
                                "end": b,
                                "excerpt_start": excerpt["start"],
                                "excerpt": excerpt["text"],
                            }
                        )
                position += len(chunk)
                carry = block[-(len(query) - 1) :] if len(query) > 1 else ""
        return {
            "source": "N1",
            "kind": "search_excerpts",
            "matches": matches,
            "total_matches": total,
            "has_more": total > len(matches),
            **({"next_start": matches[-1]["end"]} if total > len(matches) else {}),
            "scanned_start": start,
            "scanned_end": end,
            "notice": "仅进行字面关键词搜索，片段只用于定位；零命中不能证明相关情节不存在。",
        }

    def outline(self, start, limit):
        headings, position = [], start
        title = re.compile(r"^(?:#{1,6}\s+|第[^\r\n]{1,30}[章回卷节部篇]|(?:序章|序言|楔子|尾声|后记))")
        with self.open_at(start) as handle:
            beginning = start == 0 or self.read(start - 1, 1)["text"] in "\r\n"
            while line := handle.readline(4096):
                if beginning and len(line.strip()) <= 200 and title.match(line.strip()):
                    if len(headings) == limit:
                        return {
                            "source": "N1",
                            "kind": "inferred_outline",
                            "headings": headings,
                            "has_more": True,
                            "next_start": position,
                        }
                    headings.append({"start": position, "title": line.strip()})
                beginning = line.endswith(("\r", "\n"))
                position += len(line)
        return {
            "source": "N1",
            "kind": "inferred_outline",
            "headings": headings,
            "has_more": False,
            "notice": "目录按标题格式识别，可能不完整；无目录仍可搜索及按字符位置阅读。",
        }
