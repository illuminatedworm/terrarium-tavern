"""The stored purpose of a worldbook entry and the conversion of older entries."""

NARRATIVE = "narrative"
MVU_RULES = "mvu_rules"
PURPOSES = (NARRATIVE, MVU_RULES)
_OLD_FIELD = "audience"
_OLD_PURPOSES = {"writer": NARRATIVE, "tracker_mvu": MVU_RULES}


def lore_rows(book):
    if book is None:
        return
    if not isinstance(book, dict):
        raise ValueError("世界书必须为对象")
    entries = book.get("entries", [])
    if not isinstance(entries, (dict, list)):
        raise ValueError("世界书 entries 必须为对象或数组")
    for slot, entry in entries.items() if isinstance(entries, dict) else enumerate(entries):
        if not isinstance(entry, dict) or not isinstance(entry.get("extensions", {}), dict):
            raise ValueError("世界书条目及其扩展必须为对象")
        yield slot, entry


def lore_purpose(entry):
    return (entry.get("extensions", {}).get("terrarium") or {}).get("purpose", NARRATIVE)


def _metadata(slot, entry):
    label = entry.get("comment") or entry.get("name") or entry.get("id", slot)
    label = f"世界书条目 {label}"
    top = entry.get("terrarium")
    nested = entry.get("extensions", {}).get("terrarium")
    if (effective := nested if nested is not None else top) is not None and not isinstance(effective, dict):
        raise ValueError(f"{label} 的 terrarium 扩展必须为对象")
    return label, (top if isinstance(top, dict) else None), nested


def validate_lore_purposes(book):
    for slot, entry in lore_rows(book):
        label, top, nested = _metadata(slot, entry)
        if (top and ("purpose" in top or _OLD_FIELD in top)) or (nested and _OLD_FIELD in nested):
            raise ValueError(f"{label} 的用途须写在 extensions.terrarium.purpose，请重新导入旧格式文件")
        if lore_purpose(entry) not in PURPOSES:
            raise ValueError(f"{label} 的用途无效，应为 {NARRATIVE} 或 {MVU_RULES}")


def _old_purpose(value, label):
    if not isinstance(value, str) or value not in _OLD_PURPOSES:
        raise ValueError(f"{label} 的旧用途值无法识别")
    return _OLD_PURPOSES[value]


def upgrade_lore_purposes(book):
    """Convert declared purposes in place, preserving entry bodies and other fields."""
    changed = False
    for slot, entry in lore_rows(book):
        label, top, nested = _metadata(slot, entry)
        metadata = [value for value in (top, nested) if value is not None]
        has_old = any(_OLD_FIELD in value for value in metadata)
        top_purpose = top is not None and "purpose" in top
        if not has_old and not top_purpose:
            continue

        declared = [value["purpose"] for value in metadata if "purpose" in value]
        if declared:
            purpose = declared[0]
            if purpose not in PURPOSES:
                raise ValueError(f"{label} 的用途无效，应为 {NARRATIVE} 或 {MVU_RULES}")
            old = [_old_purpose(value[_OLD_FIELD], label) for value in metadata if _OLD_FIELD in value]
            if any(value != purpose for value in (*declared, *old)):
                raise ValueError(f"{label} 的用途字段冲突，请保留一个生效用途")
        else:
            # Older entries gave the entire non-null extension precedence.
            # A shadowed top-level tag did not affect the entry's behavior.
            effective = nested if nested is not None else top
            purpose = _old_purpose(effective[_OLD_FIELD], label) if _OLD_FIELD in effective else None

        if top is not None and (_OLD_FIELD in top or top_purpose):
            top.pop(_OLD_FIELD, None)
            top.pop("purpose", None)
            if not top:
                del entry["terrarium"]
        if nested is not None:
            nested.pop(_OLD_FIELD, None)
        if purpose is not None:
            extensions = entry.setdefault("extensions", {})
            if nested is None:
                nested = extensions["terrarium"] = {}
            nested["purpose"] = purpose
        changed = True
    return changed
