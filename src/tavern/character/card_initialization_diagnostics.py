"""Locate ambiguous initialization sources without migrating or deleting card data."""

import re

from tavern.character.cards import lore_entries, normalize

INIT = re.compile(r"<initvar\b", re.I)
INIT_END = re.compile(r"</initvar\s*>", re.I)
UPDATE = re.compile(r"<(?:UpdateVariable|JSONPatch)\b|_\.(?:set|add|insert|delete|move)\s*\(", re.I)


def initialization_warnings(document):
    data = normalize(document)
    warnings = []
    for index, entry in enumerate(lore_entries(document)):
        if "[initvar]" in str(entry.get("comment", "")).lower() and entry.get("terrarium", {}).get("audience") == "tracker_mvu":
            warnings.append(f"世界书第 {index + 1} 条：[InitVar] 被标记为 tracker_mvu，内置初始化会忽略它；核对初始化数据与更新规则的归属，不要删除原数据")
    for index, text in enumerate([data.get("first_mes", ""), *data.get("alternate_greetings", [])]):
        location = "first_mes" if index == 0 else f"alternate_greetings[{index - 1}]"
        updates = re.sub(r"<initvar\b[^>]*>[\s\S]*?</initvar\s*>", "", text, flags=re.I)
        updates = re.sub(r"<UpdateVariable\s*>\s*</UpdateVariable\s*>", "", updates, flags=re.I)
        if len(INIT.findall(text)) != len(INIT_END.findall(text)):
            warnings.append(location + "：initvar 标签未成对；先修复边界并验证初值，不要删除特殊数据")
        if UPDATE.search(updates):
            warnings.append(location + "：发现开场增量指令；内置初始化读取内联 initvar 全量数据，但不会自动执行原文 UpdateVariable/JSONPatch 增量。保留原文和隐藏正则，核对脚本并运行 test_draft，不能把隐藏成功当成初始化成功")
    return warnings
