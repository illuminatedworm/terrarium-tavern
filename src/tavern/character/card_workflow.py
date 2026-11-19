"""Portable card workflow metadata and deterministic validation (no script execution)."""

import json
import re

from tavern.character.card_initialization_diagnostics import initialization_warnings
from tavern.character.card_regex_diagnostics import regex_warnings
from tavern.character.card_status_diagnostics import status_display_warnings
from tavern.character.card_template_diagnostics import template_warnings
from tavern.character.cards import card_view, is_initvar, lore_entries, parse_card
from tavern.character.lore_purpose import NARRATIVE, lore_purpose, validate_lore_purposes


def validate_document(document):
    parse_card(json.dumps(document, ensure_ascii=False).encode())
    lore_entries(document)  # Validate entry IDs and retrieval fields as read by the runtime.
    validate_lore_purposes(card_view(document).get("character_book"))


def validation_report(document):
    errors, warnings = [], []
    try:
        validate_document(document)
    except (ValueError, TypeError, KeyError) as error:
        errors.append(str(error))
    if errors:
        return {"valid": False, "errors": errors, "warnings": [], "adapted": False}
    entries = lore_entries(document)
    if len({e["id"] for e in entries}) != len(entries):
        return {
            "valid": False,
            "errors": ["世界书存在重复的条目身份，请核对冲突条目"],
            "warnings": [],
            "adapted": False,
        }
    marker = re.compile(
        r"<(?:initvar|UpdateVariable|JSONPatch)\b|_\.(?:set|add|insert|delete|move)\s*\("
        r"|StatusPlaceHolder|getMvuData|getAllVariables|stat_data",
        re.I,
    )
    # Existing inline MVU initialization stays outside ordinary adaptation;
    # model-written status displays have separate, advisory diagnostics.
    for entry in entries:
        if (
            lore_purpose(entry) == NARRATIVE
            and not is_initvar(entry)
            and marker.search(entry.get("content", ""))
        ):
            warnings.append(f"叙事条目 {entry['id']} 可能包含变量规则，请确认或拆分")
    warnings.extend(status_display_warnings(document))
    warnings.extend(regex_warnings(document))
    warnings.extend(initialization_warnings(document))
    warnings.extend(template_warnings(document))
    return {
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "adapted": not errors and not warnings,
    }
