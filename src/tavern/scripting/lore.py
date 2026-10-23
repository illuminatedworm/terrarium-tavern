"""Merge script worldbook deltas without persisting normalized runtime copies."""

import copy
from uuid import uuid4

from tavern.character.cards import is_initvar, lore_entries


def sync_runtime_lore(mirror, document, previous_document):
    """Apply only authored changes, retaining prior script-side changes.

    Runtime views add derived fields to entries. Comparing only old/new card
    entries keeps those views intact unless the author actually edited them.
    """
    old = {e["id"]: e for e in lore_entries(previous_document)}
    new = {e["id"]: e for e in lore_entries(document)}
    current = {e["id"]: e for e in mirror.get("lore", [])}
    for key in old.keys() - new.keys():
        current.pop(key, None)
    for key, value in new.items():
        if key not in old or old[key] != value:
            previous = old.get(key, {})
            merged = copy.deepcopy(current.get(key, previous))
            for field in previous.keys() - value.keys():
                merged.pop(field, None)
            merged.update({field: copy.deepcopy(v) for field, v in value.items() if previous.get(field) != v})
            current[key] = merged
    mirror["lore"] = list(current.values())


def merge_script_lore(document, initial_lore, processed, *, include_existing=False):
    """Fold changed runtime entries into original list/dict containers losslessly."""
    if not processed.get("loreModified"):
        return document
    result = copy.deepcopy(document)
    data = result["data"] if result.get("spec") else result
    book = data.setdefault("character_book", {})
    existing = book.get("entries", [])
    initial = {e["id"]: e for e in initial_lore}
    final = {e["id"]: e for e in processed["lore"]}
    normalized = lore_entries(document)
    keys = list(existing) if isinstance(existing, dict) else list(range(len(existing)))
    replacements = {}
    deleted = set()
    for key, entry in zip(keys, normalized):
        eid = entry["id"]
        if eid not in final and (include_existing or eid in initial):
            deleted.add(key)
        elif eid in final and final[eid] != (entry if include_existing else initial.get(eid, entry)):
            # Copy only actual callback changes rather than replacing every
            # original entry with a lossy normalized runtime representation.
            old = entry if include_existing else initial.get(eid, entry)
            before_callbacks = initial.get(eid, {})
            value = copy.deepcopy(existing[key])
            for field in old.keys() - final[eid].keys():
                value.pop(field, None)
                value.get("extensions", {}).pop(field, None)
            previous_extension = old.get("extensions", {})
            final_extension = final[eid].get("extensions", {})
            if final_extension != previous_extension and "extensions" in final[eid]:
                extension = value.setdefault("extensions", {})
                for field in previous_extension.keys() - final_extension.keys():
                    extension.pop(field, None)
                extension.update(
                    {
                        field: copy.deepcopy(replacement)
                        for field, replacement in final_extension.items()
                        if field not in previous_extension or previous_extension[field] != replacement
                    }
                )
            for field, replacement in final[eid].items():
                # Merge extensions first: copying their older effective values
                # afterward would overwrite a callback's normalized-field edit.
                if field == "extensions" or field in old and old[field] == replacement:
                    continue
                # Runtime expands initialization text. Preserve its authored form unless a callback
                # actually changed that field after the authoring sync.
                init_only = is_initvar(entry)
                if (
                    init_only
                    and field in ("content", "comment")
                    and before_callbacks.get(field) == replacement
                ):
                    continue
                # Effective extension values override top-level fields when
                # the card is loaded again. Update that same authored source
                # unless the callback explicitly changed the extension itself.
                extension = value.get("extensions", {})
                if field in extension and field in final_extension:
                    if final_extension[field] == previous_extension.get(field):
                        extension[field] = copy.deepcopy(replacement)
                else:
                    value[field] = copy.deepcopy(replacement)
            replacements[key] = value
    known = {entry["id"] for entry in normalized} if include_existing else initial.keys()
    added = [eid for eid in final if eid not in known]
    if isinstance(existing, dict):
        entries = {k: replacements.get(k, copy.deepcopy(v)) for k, v in existing.items() if k not in deleted}
        for eid in added:
            key = eid
            while key in entries:
                key = str(uuid4())
            entries[key] = copy.deepcopy(final[eid])
    else:
        entries = [replacements.get(i, copy.deepcopy(v)) for i, v in enumerate(existing) if i not in deleted]
        entries.extend(copy.deepcopy(final[eid]) for eid in added)
    book["entries"] = entries
    return result
