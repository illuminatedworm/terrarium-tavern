"""Lossless card storage; exports canonicalize supported worldbook flag names."""

import base64
import copy
import io
import json
import re
import struct
import zipfile
import zlib
from pathlib import PurePosixPath
from uuid import uuid4

MAX_BYTES = 32 * 1024 * 1024
PNG = b"\x89PNG\r\n\x1a\n"
_LORE_FLAG_ALIASES = {
    "matchWholeWords": "match_whole_words",
    "excludeRecursion": "exclude_recursion",
    "preventRecursion": "prevent_recursion",
    "caseSensitive": "case_sensitive",
}


def png_chunks(blob):
    pos = 8
    while pos + 12 <= len(blob):
        size = struct.unpack(">I", blob[pos : pos + 4])[0]
        end = pos + size + 12
        if end > len(blob):
            raise ValueError("PNG 数据不完整")
        kind, data = blob[pos + 4 : pos + 8], blob[pos + 8 : end - 4]
        if zlib.crc32(kind + data) & 0xFFFFFFFF != struct.unpack(">I", blob[end - 4 : end])[0]:
            raise ValueError("PNG 校验失败")
        yield kind, data, blob[pos:end]
        pos = end
        if kind == b"IEND":
            return
    raise ValueError("PNG 缺少结束标记")


def parse_card(blob: bytes, filename: str):
    if len(blob) > MAX_BYTES:
        raise ValueError("角色卡超过 32 MB")
    resources = {}
    container = "json"
    if blob.startswith(PNG):
        container = "png"
        texts = {}
        for kind, data, _ in png_chunks(blob):
            if kind == b"tEXt" and b"\0" in data:
                key, value = data.split(b"\0", 1)
                if key in (b"ccv3", b"chara"):
                    texts[key] = value
                elif key.startswith(b"chara-ext-asset_:"):
                    resources[key.decode().split(":", 1)[1]] = base64.b64decode(value, validate=True)
        encoded = texts.get(b"ccv3", texts.get(b"chara"))
        if encoded is None:
            raise ValueError("图片中未找到 chara / ccv3 数据")
        document = json.loads(base64.b64decode(encoded, validate=True))
    elif zipfile.is_zipfile(io.BytesIO(blob)):
        container = "charx"
        with zipfile.ZipFile(io.BytesIO(blob)) as archive:
            infos = archive.infolist()
            if len(infos) > 2048 or sum(x.file_size for x in infos) > MAX_BYTES:
                raise ValueError("CHARX 解压大小或文件数量超限")
            for item in infos:
                path = PurePosixPath(item.filename)
                if path.is_absolute() or ".." in path.parts or "\\" in item.filename or ":" in item.filename:
                    raise ValueError("CHARX 含非法资源路径")
                if item.filename in resources:
                    raise ValueError("CHARX 含重复路径")
                if not item.is_dir():
                    resources[item.filename] = archive.read(item)
            if "card.json" not in resources:
                raise ValueError("CHARX 缺少 card.json")
            document = json.loads(resources.pop("card.json"))
    else:
        document = json.loads(blob.decode("utf-8-sig"))
    view = normalize(document)
    if not isinstance(view.get("name"), str) or not view["name"].strip():
        raise ValueError("角色卡缺少 name")
    for field in (
        "description",
        "personality",
        "scenario",
        "first_mes",
        "mes_example",
        "system_prompt",
        "post_history_instructions",
    ):
        if field in view and not isinstance(view[field], str):
            raise ValueError(f"角色卡 {field} 必须为文本")
    if "extensions" in view and not isinstance(view["extensions"], dict):
        raise ValueError("角色卡 extensions 必须为对象")
    project = view.get("extensions", {}).get("terrarium", {})
    if isinstance(project, dict) and "workflow" in project:
        raise ValueError("角色卡含已移除的独立开场初始化扩展；请核对世界书 [InitVar] 或开场 <initvar> 中的初值，"
                         "保留需要的数据后删除 extensions.terrarium.workflow")
    if view.get("character_book") is not None:
        book = view["character_book"]
        if not isinstance(book, dict) or not isinstance(book.get("entries", []), (dict, list)):
            raise ValueError("角色世界书格式无效")
    if "alternate_greetings" in view and (
        not isinstance(view["alternate_greetings"], list)
        or not all(isinstance(x, str) for x in view["alternate_greetings"])
    ):
        raise ValueError("候选开场白必须为文本数组")
    return document, container, resources, diagnose(document)


def normalize(document):
    if not isinstance(document, dict):
        raise ValueError("角色卡必须为 JSON 对象")
    data = document.get("data", document) if document.get("spec") else document
    if not isinstance(data, dict):
        raise ValueError("角色卡 data 必须为对象")
    return copy.deepcopy(data)


def _lore_flag_values(fields):
    # Keep existing camelCase precedence, including explicit false/null values.
    return {
        field: fields[field] if field in fields else fields[alias]
        for field, alias in _LORE_FLAG_ALIASES.items()
        if field in fields or alias in fields
    }


def lore_entries(document, audience=None):
    book = normalize(document).get("character_book", {}) or {}
    entries = book.get("entries", [])
    if isinstance(entries, dict):
        entries = [dict(v, uid=k) for k, v in entries.items()]
    result = []
    for index, original in enumerate(entries):
        if not isinstance(original, dict) or not isinstance(original.get("extensions", {}), dict):
            raise ValueError("世界书条目及其扩展必须为对象")
        e = {**original, **original.get("extensions", {})}
        # ST exports these flags as snake_case extensions. Resolve only the view;
        # the editor and exporter use the same precedence without rewriting storage.
        e.update(_lore_flag_values(e))
        e["id"] = str(e.get("id", e.get("uid", index)))
        e["keys"] = [str(key).strip() for key in e.get("keys", e.get("key", []))]
        e["secondary_keys"] = [str(key).strip() for key in e.get("secondary_keys", e.get("keysecondary", []))]
        e["enabled"] = e.get("enabled", not e.get("disable", False))
        if audience is None or e.get("terrarium", {}).get("audience", "writer") == audience:
            result.append(e)
    return result


def variable_rule_entries(document):
    # InitVar may carry legacy [value, description] rules. Its disabled switch
    # must not hide those rules once the author explicitly selects Tracker.
    return [entry for entry in lore_entries(document, "tracker_mvu")
            if entry["enabled"] or "[initvar]" in entry.get("comment", "").lower()]


def append_lorebook(current, imported):
    """Append losslessly, preserving the destination's settings and entry container."""
    if not isinstance(imported, dict) or not isinstance(imported.get("entries"), (dict, list)):
        raise ValueError("世界书 entries 无效")
    incoming = imported["entries"]
    if any(not isinstance(entry, dict) for entry in (incoming.values() if isinstance(incoming, dict) else incoming)):
        raise ValueError("世界书条目及其扩展必须为对象")
    book = copy.deepcopy(current if current is not None else {
        **imported, "entries": {} if isinstance(incoming, dict) else [],
    })
    entries = book.setdefault("entries", [])
    used = {entry["id"] for entry in lore_entries({"character_book": book})}
    rows = incoming.items() if isinstance(incoming, dict) else enumerate(incoming)
    identities = lore_entries({"character_book": imported})
    for (key, raw), identity in zip(rows, identities, strict=True):
        entry = copy.deepcopy(raw)
        if isinstance(entries, list) and isinstance(incoming, dict):
            entry["uid"] = key
        eid = identity["id"]
        collision = eid in used
        if collision:
            while eid in used:
                eid = str(uuid4())
        # Imported index/key identities must stay stable at their new location.
        if "id" not in entry or collision:
            entry["id"] = eid
        if collision and "id" in entry.get("extensions", {}):
            entry["extensions"]["id"] = eid
        used.add(eid)
        if isinstance(entries, list):
            entries.append(entry)
        else:
            target = key if isinstance(incoming, dict) else eid
            while target in entries:
                target = str(uuid4())
            entries[target] = entry
    return book


def card_scripts(document):
    ext = normalize(document).get("extensions", {}) or {}
    helper = ext.get("tavern_helper", {})
    if isinstance(helper, list):
        helper = dict(helper)
    scripts = helper.get("scripts", ext.get("TavernHelper_scripts", []))

    def flatten(items):
        for item in items:
            if isinstance(item, list) and len(item) == 2:
                item = item[1]
            if not isinstance(item, dict):
                continue
            if "scripts" in item:
                yield from flatten(item["scripts"])
            elif item.get("enabled", True):
                yield item

    return list(flatten(scripts))


def regex_scripts(document):
    return normalize(document).get("extensions", {}).get("regex_scripts", [])


def diagnose(document):
    scripts = card_scripts(document)
    text = json.dumps(document, ensure_ascii=False)
    unsupported = [
        x
        for x in ("SillyTavern.getContext", "parent.document", "parent.$", "<%", "executeSlashCommands")
        if x in text
    ]
    urls = sorted(set(re.findall(r"https?://[^\s\"'<>\\)]+", text)))
    return {
        "data": "parsed",
        "display": "needs_runtime_test" if regex_scripts(document) or "<html" in text else "text",
        "interaction": "needs_runtime_test" if scripts else "none",
        "variables": "mvu_detected" if re.search(r"mvu|initvar", text, re.I) else "native",
        "unsupported": unsupported,
        "external_dependencies": urls,
        "note": "静态检查不代表完整运行兼容；脚本需启用后在隔离环境中验证。",
    }


def _card_for_export(document):
    result = copy.deepcopy(document)
    data = result.get("data", result) if result.get("spec") else result
    entries = (data.get("character_book") or {}).get("entries", [])
    for entry in entries.values() if isinstance(entries, dict) else entries:
        flags = _lore_flag_values({**entry, **entry.get("extensions", {})})
        for field, value in flags.items():
            alias = _LORE_FLAG_ALIASES[field]
            # Preserve the effective value while removing every alternate location.
            entry.pop(field, None)
            entry.pop(alias, None)
            extensions = entry.setdefault("extensions", {})
            extensions.pop(field, None)
            extensions[alias] = value
    return result


def export_card(document, container, original, resources):
    encoded = json.dumps(_card_for_export(document), ensure_ascii=False, indent=2).encode()
    if container == "json":
        return encoded, "application/json", "json"
    if container == "charx":
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("card.json", encoded)
            for name, content in resources.items():
                z.writestr(name, content)
        return buffer.getvalue(), "application/zip", "charx"
    key = b"ccv3" if document.get("spec") == "chara_card_v3" else b"chara"
    text = key + b"\0" + base64.b64encode(encoded)
    chunk = (
        struct.pack(">I", len(text))
        + b"tEXt"
        + text
        + struct.pack(">I", zlib.crc32(b"tEXt" + text) & 0xFFFFFFFF)
    )
    out = bytearray(PNG)
    for kind, data, raw in png_chunks(original):
        if kind == b"tEXt" and data.split(b"\0", 1)[0] in (b"ccv3", b"chara"):
            continue
        if kind == b"IEND":
            out.extend(chunk)
        out.extend(raw)
    return bytes(out), "image/png", "png"
