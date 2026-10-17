"""Compare independent wire parameters without reconstructing a provider prompt."""
import json
import re
from collections import deque
from itertools import takewhile

import httpx

WHITESPACE = re.compile(r"\s*")
DECODER = json.JSONDecoder()


def wire_parameters(body: bytes) -> dict[str, bytes]:
    """Locate JSON values, retaining their exact UTF-8 spelling and key order.

    Parsing only locates boundaries: re-serializing would hide whitespace,
    escaping and object-order changes introduced by an SDK.
    """
    source = body.decode("utf-8")
    if not source.startswith("{"):
        raise ValueError("请求正文不是 JSON 对象")
    result = {}
    offset = WHITESPACE.match(source, 1).end()
    while source[offset] != "}":
        key, offset = DECODER.raw_decode(source, offset)
        offset = WHITESPACE.match(source, offset).end()
        if source[offset] != ":" or key in result:
            raise ValueError("请求参数边界无效或包含重复名称")
        start = WHITESPACE.match(source, offset + 1).end()
        _, end = DECODER.raw_decode(source, start)
        result[key] = source[start:end].encode("utf-8")
        offset = WHITESPACE.match(source, end).end()
        if source[offset] == "}":
            break
        if source[offset] != ",":
            raise ValueError("请求参数边界无效")
        offset = WHITESPACE.match(source, offset + 1).end()
    return result


def compare_parameters(previous: bytes, current: bytes) -> list[dict]:
    before, after = wire_parameters(previous), wire_parameters(current)
    result = []
    for name in dict.fromkeys([*before, *after]):
        left, right = before.get(name), after.get(name)
        entry = {"parameter": name, "previous_bytes": len(left) if left is not None else None,
                 "current_bytes": len(right) if right is not None else None}
        if left is None or right is None:
            entry["status"] = "added" if left is None else "removed"
        else:
            prefix = sum(1 for _ in takewhile(lambda pair: pair[0] == pair[1], zip(left, right)))
            entry.update(status="identical" if left == right else "changed", common_prefix_bytes=prefix,
                         first_difference_byte=None if left == right else prefix)
            # Appending messages changes the old array's closing bracket. Show
            # that exact boundary separately instead of calling it instability.
            if (left != right and left.startswith(b"[") and prefix == len(left) - 1
                    and right[prefix:prefix + 1] == b","):
                entry["status"] = "array_extended"
        result.append(entry)
    return result


class PrefixDebug:
    def __init__(self):
        self.requests: deque[bytes] = deque(maxlen=2)
        self.sequence = 0

    def clear(self):
        self.requests.clear()
        self.sequence = 0

    def record(self, request: httpx.Request) -> dict:
        self.requests.append(request.content)
        self.sequence += 1
        if len(self.requests) < 2:
            return {"message": "Full Agent 前缀测试：已暂存首个真实请求，等待下一个请求。", "parameters": []}
        parameters = compare_parameters(*self.requests)
        lines = [f"Full Agent 前缀测试：请求 #{self.sequence - 1} → #{self.sequence}"]
        descriptions = {"added": "新增参数，无法比较", "removed": "参数已移除，无法比较",
                        "identical": "一致", "array_extended": "不一致", "changed": "不一致"}
        for item in parameters:
            detail = descriptions[item["status"]]
            if item["parameter"] in {"messages", "input", "contents"}:
                if item["status"] in {"identical", "array_extended"}:
                    detail = "公共前缀与首个差异未错位"
                elif item["status"] == "changed":
                    detail = "公共前缀或首个差异发生错位"
            lines.append(f"{item['parameter']}：{detail}")
        return {"message": "\n".join(lines), "parameters": parameters}
