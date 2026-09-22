"""Portable card workflow metadata and deterministic validation (no script execution)."""

import hashlib
import json
import re

from tavern.character.cards import lore_entries, normalize, parse_card

AUDIENCES = {"writer", "tracker_mvu", "runtime_init"}


def audience(entry):
    extensions = entry.get("extensions", {})
    if not isinstance(extensions, dict):
        raise ValueError("世界书扩展必须为对象")
    metadata = extensions.get("terrarium", entry.get("terrarium", {}))
    if not isinstance(metadata, dict):
        raise ValueError("世界书 terrarium 扩展必须为对象")
    return metadata.get("audience", "writer")


def workflow(document):
    metadata = normalize(document).get("extensions", {}).get("terrarium", {})
    if not isinstance(metadata, dict):
        raise ValueError("角色卡 terrarium 扩展必须为对象")
    return metadata.get("workflow")


def greeting_hash(text):
    return hashlib.sha256(text.encode()).hexdigest()


def validate_document(document):
    parse_card(json.dumps(document, ensure_ascii=False).encode(), "card.json")
    for entry in lore_entries(document):
        if audience(entry) not in AUDIENCES:
            raise ValueError(f"条目 {entry['id']} 的用途无效")
    metadata = workflow(document)
    if metadata is None:
        return
    if not isinstance(metadata, dict) or metadata.get("version") != 1:
        raise ValueError("不支持的角色卡工作流版本")
    if not isinstance(metadata.get("unresolved", []), list):
        raise ValueError("工作流 unresolved 必须为待处理问题数组")
    data = normalize(document)
    greetings = [data.get("first_mes", ""), *data.get("alternate_greetings", [])]
    records = metadata.get("greetings")
    if not isinstance(records, list) or len(records) != len(greetings):
        raise ValueError("开场白与独立初始化记录必须一一对应")
    ids = set()
    for text, record in zip(greetings, records, strict=True):
        if not isinstance(record, dict) or not isinstance(record.get("id"), str) or not record["id"]:
            raise ValueError("开场初始化缺少稳定 ID")
        if record["id"] in ids:
            raise ValueError("开场初始化 ID 重复")
        ids.add(record["id"])
        if record.get("greeting_hash") != greeting_hash(text):
            raise ValueError("开场白与初始化记录不匹配，请通过开场编辑工具同步正文与初始化")
        if not isinstance(record.get("initialization", ""), str):
            raise ValueError("开场初始化必须为文本")
        if re.search(r"<script\b|\beval\s*\(|\bfetch\s*\(", record.get("initialization", ""), re.I):
            raise ValueError("开场初始化只允许 InitVar 数据与 MVU 数据指令")


def validation_report(document):
    errors, warnings = [], []
    try:
        validate_document(document)
    except (ValueError, TypeError, KeyError) as error:
        errors.append(str(error))
    marker = re.compile(r"<(?:initvar|UpdateVariable|JSONPatch)\b|_\.(?:set|add|insert|delete|move)\s*\("
                        r"|StatusPlaceHolder|getMvuData|getAllVariables|stat_data", re.I)
    # Opening MVU and its card-owned filtering/display rules are outside the
    # default adaptation scope; only Writer worldbook leakage is diagnosed.
    for entry in lore_entries(document):
        if audience(entry) == "writer" and marker.search(entry.get("content", "")):
            warnings.append(f"Writer 条目 {entry['id']} 可能包含变量规则，请确认或拆分")
    meta = workflow(document) or {}
    warnings.extend(str(item) for item in meta.get("unresolved", []))
    return {"valid": not errors, "errors": errors, "warnings": warnings,
            "adapted": not errors and not warnings}
