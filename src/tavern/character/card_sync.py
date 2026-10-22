"""Selective sync from a shelf card into a story's conversation-level card.

Only content a running story still reads is offered: the character prompt
fields, worldbook entries, regex scripts and Tavern Helper scripts. Openings,
creator notes, tags and worldbook-level settings matter only when a story
starts or for display, so they are never synced.

Each difference is compared three ways against the card copied at story
creation (``base``): a shelf-only change is offered by default, while a
change made inside the story (by Tracker, scripts or manual edits) is shown
but left unselected so syncing never silently reverts story progress.

Entries mounted from the worldbook library are not shelf content: callers pass
their IDs as ``exclude`` so they are never offered and survive a full overwrite.
"""

import copy
from uuid import uuid4

from tavern.character.cards import lore_entries, lore_title, normalize

FIELDS = (
    "name",
    "nickname",
    "description",
    "personality",
    "scenario",
    "system_prompt",
    "post_history_instructions",
    "mes_example",
)
_MISSING = object()


def _unique(identities):
    # Duplicate IDs in malformed cards still need one distinct sync key each.
    seen, result = {}, []
    for identity in identities:
        seen[identity] = seen.get(identity, 0) + 1
        result.append(identity if seen[identity] == 1 else f"{identity}~{seen[identity]}")
    return result


def _lore_rows(data, exclude=frozenset()):
    """{identity: (container key, raw entry)} in source order, without ``exclude``."""
    book = data.get("character_book") or {}
    entries = book.get("entries", [])
    keys = list(entries) if isinstance(entries, dict) else list(range(len(entries)))
    ids = _unique(entry["id"] for entry in lore_entries({"character_book": book}))
    return {
        identity: (key, entries[key])
        for identity, key in zip(ids, keys, strict=True)
        if identity not in exclude
    }


def _regex_list(data):
    scripts = (data.get("extensions") or {}).get("regex_scripts", [])
    if not isinstance(scripts, list):
        raise ValueError("extensions.regex_scripts 必须是数组")
    return scripts


def _regex_rows(data):
    scripts = _regex_list(data)
    ids = _unique(
        str(script["id"]) if isinstance(script, dict) and script.get("id") not in (None, "") else f"#{i + 1}"
        for i, script in enumerate(scripts)
    )
    return {
        identity: (index, script) for index, (identity, script) in enumerate(zip(ids, scripts, strict=True))
    }


def _helper_pairs(helper):
    return [part for part in helper if isinstance(part, list) and len(part) == 2]


def _helper_scripts(data):
    extensions = data.get("extensions") or {}
    helper = extensions.get("tavern_helper", {})
    helper = dict(_helper_pairs(helper)) if isinstance(helper, list) else helper
    value = {}
    if isinstance(helper, dict) and "scripts" in helper:
        value["tavern_helper.scripts"] = helper["scripts"]
    if "TavernHelper_scripts" in extensions:
        value["TavernHelper_scripts"] = extensions["TavernHelper_scripts"]
    return value or _MISSING


def _common_order(rows, others):
    return [key for key in rows if all(key in other for other in others)]


def _shown(value):
    return None if value is _MISSING else value


def _item(key, group, label, base, current, source):
    return {
        "key": key,
        "group": group,
        "label": label,
        "change": "add" if current is _MISSING else "remove" if source is _MISSING else "update",
        # "card": only the shelf card changed; "story": only the story changed,
        # so syncing reverts it; "both": both sides diverged from the base.
        "origin": "card" if base == current else "story" if base == source else "both",
        "before": _shown(current),
        "after": _shown(source),
    }


def _lore_label(entry, identity):
    return lore_title(entry) or f"条目 {identity}"


def _regex_label(script, identity):
    if not isinstance(script, dict):
        return f"正则脚本 {identity}"
    return str(script.get("scriptName") or script.get("name") or f"正则脚本 {identity}")


def sync_items(base, current, source, exclude=frozenset()):
    """Differences between the story card and the shelf card, grouped for selection."""
    base, current, source = normalize(base), normalize(current), normalize(source)
    items = []
    for field in FIELDS:
        b, c, s = (data.get(field, _MISSING) for data in (base, current, source))
        if c != s:
            items.append(_item(f"field:{field}", "character", field, b, c, s))

    for group, rows, label in (
        ("lore", lambda data: _lore_rows(data, exclude), _lore_label),
        ("regex", _regex_rows, _regex_label),
    ):
        b_rows, c_rows, s_rows = rows(base), rows(current), rows(source)
        for identity in [*c_rows, *(key for key in s_rows if key not in c_rows)]:
            c = c_rows[identity][1] if identity in c_rows else _MISSING
            s = s_rows[identity][1] if identity in s_rows else _MISSING
            if c != s:
                b = b_rows[identity][1] if identity in b_rows else _MISSING
                shown = s if s is not _MISSING else c
                items.append(_item(f"{group}:{identity}", group, label(shown, identity), b, c, s))
        if group == "regex":
            # Regex rules run in sequence, so a reordering alone changes output.
            c_order, s_order = _common_order(c_rows, [s_rows]), _common_order(s_rows, [c_rows])
            if c_order != s_order:
                b_order = [key for key in b_rows if key in c_order]
                item = _item("regex-order", "regex", "执行顺序", b_order, c_order, s_order)
                # Preview names instead of internal script IDs.
                item["before"] = [_regex_label(c_rows[key][1], key) for key in c_order]
                item["after"] = [_regex_label(s_rows[key][1], key) for key in s_order]
                items.append(item)

    b, c, s = (_helper_scripts(data) for data in (base, current, source))
    if c != s:
        items.append(_item("scripts", "scripts", "Tavern Helper 角色脚本", b, c, s))
    return items


def _set_helper_scripts(data, value):
    extensions = data.setdefault("extensions", {})
    value = {} if value is _MISSING else value
    if "TavernHelper_scripts" in value:
        extensions["TavernHelper_scripts"] = copy.deepcopy(value["TavernHelper_scripts"])
    else:
        extensions.pop("TavernHelper_scripts", None)
    helper = extensions.get("tavern_helper")
    scripts = value.get("tavern_helper.scripts", _MISSING)
    if isinstance(helper, list):
        # Keep the list-of-pairs form some Tavern Helper versions export.
        position = next(
            (i for i, part in enumerate(helper) if _helper_pairs([part]) and part[0] == "scripts"),
            None,
        )
        if scripts is _MISSING:
            if position is not None:
                del helper[position]
        elif position is None:
            helper.append(["scripts", copy.deepcopy(scripts)])
        else:
            helper[position] = ["scripts", copy.deepcopy(scripts)]
    elif scripts is not _MISSING:
        extensions.setdefault("tavern_helper", {})["scripts"] = copy.deepcopy(scripts)
    elif isinstance(helper, dict):
        helper.pop("scripts", None)


def _apply_lore(data, source, chosen, every, exclude):
    s_book = source.get("character_book") or {}
    s_entries = s_book.get("entries", [])
    if not isinstance(data.get("character_book"), dict):
        settings = {key: copy.deepcopy(value) for key, value in s_book.items() if key != "entries"}
        data["character_book"] = {**settings, "entries": {} if isinstance(s_entries, dict) else []}
    book = data["character_book"]
    if every:
        # Selecting every entry is a full overwrite, including source order.
        # Mounted library entries are not card content and follow the shelf's.
        mounted = [(identity, raw) for identity, (_, raw) in _lore_rows(data).items() if identity in exclude]
        if source.get("character_book"):
            book["entries"] = copy.deepcopy(s_entries)
        else:
            book["entries"] = {} if isinstance(book.get("entries"), dict) else []
        for identity, raw in mounted:
            if isinstance(book["entries"], list):
                book["entries"].append(raw)
            else:
                book["entries"][identity] = raw
        return
    c_rows, s_rows = _lore_rows(data), _lore_rows(source)
    entries = book.setdefault("entries", [])
    removed = []
    for identity in chosen:
        if identity not in s_rows:
            removed.append(c_rows[identity][0])
            continue
        source_key, raw = s_rows[identity]
        entry = copy.deepcopy(raw)
        if isinstance(entries, list) and isinstance(s_entries, dict):
            entry["uid"] = source_key  # keyed identities stay stable in an array
        if identity in c_rows:
            entries[c_rows[identity][0]] = entry
        elif isinstance(entries, list):
            entries.append(entry)
        else:
            key = identity
            while key in entries:
                key = f"{identity}-{uuid4().hex[:8]}"
            if key != identity and "id" not in entry:
                entry["id"] = identity
            entries[key] = entry
    for key in sorted(removed, reverse=True) if isinstance(entries, list) else removed:
        del entries[key]


def _apply_regex(data, source, chosen, every, order):
    extensions = data.setdefault("extensions", {})
    if every:
        if "regex_scripts" in (source.get("extensions") or {}):
            extensions["regex_scripts"] = copy.deepcopy(_regex_list(source))
        else:
            extensions.pop("regex_scripts", None)
        return
    c_rows, s_rows = _regex_rows(data), _regex_rows(source)
    rows = [[identity, script] for identity, (_, script) in c_rows.items()]
    for identity in chosen:
        if identity not in s_rows:
            rows = [row for row in rows if row[0] != identity]
        elif identity in c_rows:
            next(row for row in rows if row[0] == identity)[1] = copy.deepcopy(s_rows[identity][1])
        else:
            rows.append([identity, copy.deepcopy(s_rows[identity][1])])
    if order:
        # Shelf scripts follow shelf order; story-only scripts stay right after
        # the shelf script that preceded them.
        rank = {identity: index for index, identity in enumerate(s_rows)}
        last, ordered = -1, []
        for position, (identity, script) in enumerate(rows):
            if identity in rank:
                last = rank[identity]
            ordered.append(((last, identity not in rank, position), script))
        rows = [[None, script] for _, script in sorted(ordered, key=lambda row: row[0])]
    extensions["regex_scripts"] = [script for _, script in rows]


def apply_sync(base, current, source, keys, exclude=frozenset()):
    """Return ``current`` with the selected sync items taken from ``source``.

    Keys must come from :func:`sync_items` for the same three documents; a
    stale or unknown key fails so the user re-reviews the current differences.
    """
    items = {item["key"]: item for item in sync_items(base, current, source, exclude)}
    keys = set(keys)
    if not keys:
        raise ValueError("请选择要同步的内容")
    if unknown := keys - items.keys():
        raise ValueError(f"同步项已变化，请重新打开同步：{', '.join(sorted(unknown))}")
    document = copy.deepcopy(current)
    data = document["data"] if document.get("spec") else document
    source = normalize(source)
    for key in keys:
        if key.startswith("field:"):
            field = key.removeprefix("field:")
            if field in source:
                data[field] = copy.deepcopy(source[field])
            else:
                data.pop(field, None)
    if "scripts" in keys:
        _set_helper_scripts(data, _helper_scripts(source))
    for group in ("lore", "regex"):
        group_keys = {key for key, item in items.items() if item["group"] == group}
        selected = keys & group_keys
        if not selected:
            continue
        # Item order puts story entries first, then shelf-only additions in shelf order.
        chosen = [key.split(":", 1)[1] for key in items if key in selected and key != "regex-order"]
        every = selected == group_keys
        if group == "lore":
            _apply_lore(data, source, chosen, every, exclude)
        else:
            _apply_regex(data, source, chosen, every, "regex-order" in keys)
    return document
