"""Portable card workflow metadata and deterministic validation (no script execution)."""

import json
import re

from tavern.character.card_initialization_diagnostics import initialization_warnings
from tavern.character.card_regex_diagnostics import regex_warnings
from tavern.character.card_status_diagnostics import status_display_warnings
from tavern.character.card_template_diagnostics import template_warnings
from tavern.character.cards import lore_entries, parse_card

AUDIENCES = {"writer", "tracker_mvu"}


def audience(entry):
    extensions = entry.get("extensions", {})
    if not isinstance(extensions, dict):
        raise ValueError("世界书扩展必须为对象")
    metadata = extensions.get("terrarium", entry.get("terrarium", {}))
    if not isinstance(metadata, dict):
        raise ValueError("世界书 terrarium 扩展必须为对象")
    return metadata.get("audience", "writer")


def validate_document(document):
    parse_card(json.dumps(document, ensure_ascii=False).encode(), "card.json")
    for entry in lore_entries(document):
        if audience(entry) not in AUDIENCES:
            raise ValueError(f"条目 {entry['id']} 的用途无效")


def validation_report(document):
    errors, warnings = [], []
    try:
        validate_document(document)
    except (ValueError, TypeError, KeyError) as error:
        errors.append(str(error))
    if errors:
        return {"valid": False, "errors": errors, "warnings": [], "adapted": False}
    marker = re.compile(
        r"<(?:initvar|UpdateVariable|JSONPatch)\b|_\.(?:set|add|insert|delete|move)\s*\("
        r"|StatusPlaceHolder|getMvuData|getAllVariables|stat_data",
        re.I,
    )
    # Existing inline MVU initialization stays outside ordinary adaptation;
    # model-written status displays have separate, advisory diagnostics.
    for entry in lore_entries(document):
        if (
            audience(entry) == "writer"
            and "[initvar]" not in str(entry.get("comment", "")).lower()
            and marker.search(entry.get("content", ""))
        ):
            warnings.append(f"Writer 条目 {entry['id']} 可能包含变量规则，请确认或拆分")
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
