"""Standalone library worldbooks as Card Author drafts.

A library book is a subset of a card: ``{"name", "character_book"}`` is exactly the
document ``validate_book`` checks. A draft item carries that document plus a
``worldbook`` key (the library ID), so every document tool works unchanged. The
library ID stays in the item and never enters model input; models address the draft
by its ``C`` number like any card.
"""

from tavern.character.card_workflow import audience, validation_report
from tavern.character.cards import is_initvar, lore_entries, lore_title
from tavern.character.worldbooks import validate_book

KEYS = {"name", "character_book"}
NAME_LIMIT = 200


def is_worldbook(item):
    return "worldbook" in item


def library_document(row):
    return {"name": row["name"], "character_book": row["book"]}


def reject_worldbook(item, action):
    if is_worldbook(item):
        raise ValueError(f"世界书草稿没有{action}；它只有名称和世界书条目")


def validate_worldbook_document(document):
    """Full check of a worldbook draft, including every card-level document check."""
    if set(document) != KEYS:
        raise ValueError("世界书草稿只能包含 name 和 character_book；其他内容请写入角色卡草稿")
    name = document["name"]
    if not isinstance(name, str) or not name.strip() or len(name) > NAME_LIMIT:
        raise ValueError(f"世界书名称必须是 1–{NAME_LIMIT} 个字符的非空文本")
    validate_book(name, document["character_book"])


def worldbook_report(document):
    validation = validation_report(document)
    completeness = []
    if validation["valid"]:
        entries = lore_entries(document)
        if not entries:
            completeness.append("世界书还没有条目")
        for index, entry in enumerate(entries, 1):
            title = lore_title(entry) or f"第 {index} 条"
            if not str(entry.get("content", "")).strip():
                completeness.append(f"条目「{title}」内容为空")
            # Only enabled Writer entries rely on keys: Tracker and InitVar entries take
            # other paths, and Full Agent can still find keyless entries by searching.
            if (
                entry["enabled"]
                and audience(entry) == "writer"
                and not is_initvar(entry)
                and not entry["keys"]
                and not entry.get("constant")
            ):
                completeness.append(
                    f"条目「{title}」没有主触发词且不是常驻条目：Half Agent 不会按关键词召回，Full Agent 只能靠主动检索找到"
                )
    return {
        "valid": validation["valid"],
        "errors": validation["errors"],
        "warnings": validation["warnings"],
        "completeness": completeness,
        "runtime_tested": False,
    }
