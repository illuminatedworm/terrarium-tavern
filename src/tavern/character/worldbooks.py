"""Mount library worldbooks into a conversation-level card.

Mounted entries are copied into the card's single ``character_book``, so every
reader (retrieval, both agents, Tracker, Advisor, scripts, MVU, review and
transfer) treats them exactly like card entries. Each copy gets a fresh ID:
entry IDs key usage, cooldown and MVU ledgers, Full Agent rejects duplicates,
and a later shelf sync may add an entry carrying the library's original ID.

Which entries came from which book is recorded in ``snapshot["worldbooks"]``
rather than in the card: the card document is shown to models, and library
IDs must not enter model input. Card-content writes (Tracker, scripts, Full
Agent) replace ``snapshot["content"]`` wholesale, so the record lives beside it.
"""

import copy
from uuid import uuid4

from tavern.character.card_workflow import validate_document
from tavern.character.cards import lore_entries

REGISTRY = "worldbooks"


def validate_book(name, book):
    if not isinstance(book, dict) or not isinstance(book.get("entries"), (dict, list)):
        raise ValueError("世界书 entries 无效")
    # A library book must pass exactly the checks it would pass inside a card.
    validate_document({"name": name, "character_book": book})


def mounted_ids(snapshot):
    return {eid for item in snapshot.get(REGISTRY, []) for eid in item["entries"]}


def _data(document):
    return document.get("data", document) if document.get("spec") else document


def _mount(document, row):
    data = _data(document)
    if not isinstance(data.get("character_book"), dict):
        # No book-level settings are copied: scripts keep seeing the card's own
        # book name ("character"), and book-level settings are not read at runtime.
        data["character_book"] = {"entries": []}
    entries = data["character_book"].setdefault("entries", [])
    source = row["book"]["entries"]
    ids = []
    for raw in source.values() if isinstance(source, dict) else source:
        entry = copy.deepcopy(raw)
        eid = uuid4().hex
        entry["id"] = eid
        # ``extensions.id`` takes precedence over ``id`` when entries are read.
        if "id" in entry.get("extensions", {}):
            entry["extensions"]["id"] = eid
        if isinstance(entries, list):
            entries.append(entry)
        else:
            entries[eid] = entry
        ids.append(eid)
    return {"id": row["id"], "name": row["name"], "revision": row["revision"], "entries": ids}


def _remove(document, ids):
    book = _data(document).get("character_book")
    if not ids or not isinstance(book, dict):
        return
    entries = book.get("entries", [])
    keys = list(entries) if isinstance(entries, dict) else list(range(len(entries)))
    effective = [entry["id"] for entry in lore_entries({"character_book": book})]
    # IDs already deleted by Tracker, scripts or manual edits are simply absent.
    doomed = [key for key, eid in zip(keys, effective, strict=True) if eid in ids]
    for key in reversed(doomed):
        del entries[key]


def apply_mounts(document, registry, wanted, reload, rows):
    """Return ``(document, registry)`` with exactly ``wanted`` mounted, in that order.

    ``rows`` holds the current library books for every newly mounted or reloaded
    ID. Unmounting or reloading drops the conversation's copies, including any
    changes made to them inside the story.
    """
    current = {item["id"]: item for item in registry}
    if len(set(wanted)) != len(wanted):
        raise ValueError("世界书重复挂载")
    if stale := set(reload) - (set(wanted) & current.keys()):
        raise ValueError(f"只能重新载入已挂载的世界书：{len(stale)} 项无效")
    document = copy.deepcopy(document)
    keep = {wid for wid in wanted if wid in current and wid not in reload}
    _remove(document, {eid for item in registry if item["id"] not in keep for eid in item["entries"]})
    return document, [current[wid] if wid in keep else _mount(document, rows[wid]) for wid in wanted]
