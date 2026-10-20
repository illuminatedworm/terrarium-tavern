"""Locate ST-Prompt-Template syntax, which Runtime sends as plain text instead of executing."""

import re

from tavern.character.cards import lore_entries, normalize

TAG = re.compile(r"<%|<#escape-ejs>", re.I)
TITLE = re.compile(r"^\s*(?:\[(?:GENERATE|RENDER)[:\]]|@INJECT\b|\[InitialVariables]|\[Preprocessing])", re.I)
# Decorators only count on the leading lines; "@@@" is the plugin's own escape.
DECORATOR = re.compile(r"^@@(?!@)\w")
FIELDS = (
    "description",
    "personality",
    "scenario",
    "system_prompt",
    "post_history_instructions",
    "mes_example",
)


def template_warnings(document):
    data = normalize(document)
    texts = [(key, data.get(key, "")) for key in FIELDS]
    openings = [data.get("first_mes", ""), *data.get("alternate_greetings", [])]
    texts.extend(
        ("first_mes" if i == 0 else f"alternate_greetings[{i - 1}]", text) for i, text in enumerate(openings)
    )
    locations = [key for key, text in texts if TAG.search(text)]
    # Disabled entries are included: a plugin compatibility setting makes special
    # entries effective only while disabled. Ordinals keep entry IDs out of model text.
    for i, entry in enumerate(lore_entries(document)):
        content = entry.get("content", "")
        if (
            TAG.search(content)
            or TITLE.match(str(entry.get("comment") or ""))
            or DECORATOR.match(content.lstrip())
        ):
            locations.append(f"世界书第 {i + 1} 条")
    rules = data.get("extensions", {}).get("regex_scripts", [])
    for i, rule in enumerate(rules if isinstance(rules, list) else []):
        if isinstance(rule, dict) and TAG.search(str(rule.get("replaceString") or "")):
            locations.append(f"正则第 {i + 1} 条")
    if not locations:
        return []
    return [
        "、".join(locations) + "：含 ST-Prompt-Template 语法（EJS、条目标题前缀或 @@ 装饰器），本项目不执行，"
        "会作为普通文本发送或显示；请按 references/prompt-template.md 改写，并报告降级与未转换项"
    ]
