"""Message-local story boundaries; offsets use JavaScript's UTF-16 code units.

The separator is ordinary whitespace in raw/display text. Only the reader draws
the delivery ornament, so copying or sending the text never leaks UI markers.
"""

from collections.abc import Sequence

SEPARATOR = "\n\n"
MAX_SEGMENTS = 3


def text_length(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def split_segments(text: str, breaks: Sequence[int]) -> list[str]:
    if not breaks:
        return [text]
    encoded = text.encode("utf-16-le")
    result, start = [], 0
    for end in breaks:
        result.append(encoded[start * 2 : end * 2].decode("utf-16-le"))
        start = end + len(SEPARATOR)
    result.append(encoded[start * 2 :].decode("utf-16-le"))
    return result


def join_segments(parts: Sequence[str]) -> tuple[str, list[int]]:
    breaks, end = [], 0
    for part in parts[:-1]:
        end += text_length(part)
        breaks.append(end)
        end += len(SEPARATOR)
    return SEPARATOR.join(parts), breaks


def validate_segments(value: object, raw: str, display: str) -> dict[str, list[int]]:
    """Validate imported boundaries against both authoritative text fields."""
    if not isinstance(value, dict) or set(value) != {"raw", "display"}:
        raise ValueError("正文分段信息须包含 raw 与 display 边界")
    if any(not isinstance(value[key], list) for key in ("raw", "display")):
        raise ValueError("正文分段边界必须是列表")
    if len(value["raw"]) != len(value["display"]) or len(value["raw"]) >= MAX_SEGMENTS:
        raise ValueError("正文分段数量无效；每条回复最多三段")
    if not value["raw"]:
        return value
    for key, text in (("raw", raw), ("display", display)):
        encoded, start = text.encode("utf-16-le"), 0
        for end in value[key]:
            if type(end) is not int or not start <= end <= len(encoded) // 2 - len(SEPARATOR):
                raise ValueError("正文分段边界超出范围或顺序无效")
            if encoded[end * 2 : (end + len(SEPARATOR)) * 2] != SEPARATOR.encode("utf-16-le"):
                raise ValueError("正文分段边界与正文中的空行不一致")
            start = end + len(SEPARATOR)
        try:
            parts = split_segments(text, value[key])
        except UnicodeDecodeError:
            raise ValueError("正文分段边界不能拆开 Unicode 字符") from None
        if key == "raw" and value[key] and any(not part.strip() for part in parts):
            raise ValueError("正文分段不能包含空白原文")
    return value
