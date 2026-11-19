"""Maintain worldbook identity while applying Card Author's structural edits."""

from collections import Counter
from uuid import uuid4

import jsonpointer

from tavern.character.cards import card_root, card_view
from tavern.character.lore_purpose import lore_rows


def entries_of(document):
    return (card_view(document).get("character_book") or {}).get("entries", [])


def rows(entries):
    return entries.items() if isinstance(entries, dict) else enumerate(entries)


def identity(key, entry, keyed):
    ext = entry.get("extensions", {})
    return str(ext.get("id", entry.get("id", ext.get("uid", key if keyed else entry.get("uid", key)))))


def assign_id(entry, value):
    entry["id"] = value
    if "id" in entry.get("extensions", {}):
        entry["extensions"]["id"] = value


def fresh_entry(entry):
    if not isinstance(entry, dict) or not isinstance(entry.get("extensions", {}), dict):
        raise ValueError("新增或复制的世界书条目及其扩展须为对象")
    value = uuid4().hex
    assign_id(entry, value)
    for fields in (entry, entry.get("extensions", {})):
        if "uid" in fields:
            fields["uid"] = value
    return value


def append_path(document, operation):
    """JSON Pointer's '-' is a literal dictionary key. Allocate a real slot for book append."""
    path = operation["path"]
    prefix = card_root(document)
    if path == prefix + "/character_book/entries/-" and operation["op"] in {"add", "copy", "move"}:
        if isinstance(entries_of(document), dict):
            return path[:-1] + uuid4().hex
    return path


class LoreIdentities:
    def __init__(self, document):
        entries = entries_of(document)
        # Retain the objects as well as their ids. In-place JSON Patch preserves
        # object identity across insert/move, even for an entry with no stored ID.
        self.original = dict(rows(entries))
        self.by_object = {
            id(entry): identity(key, entry, isinstance(entries, dict)) for key, entry in self.original.items()
        }
        self.counts = Counter(self.by_object.values())

    def finish(self, document):
        book = card_view(document).get("character_book")
        keyed = isinstance(book, dict) and isinstance(book.get("entries"), dict)
        seen = Counter()
        for key, entry in lore_rows(book):
            current = identity(key, entry, keyed)
            previous = self.by_object.get(id(entry))
            if previous is not None and previous != current and "id" not in entry:
                # Array-index or dictionary-slot identity moved with the object.
                assign_id(entry, previous)
            elif previous is None and "id" not in entry and key not in self.original:
                assign_id(entry, uuid4().hex)
            seen[identity(key, entry, keyed)] += 1
        if any(count > max(1, self.counts[key]) for key, count in seen.items()):
            raise ValueError("修改会产生重复的世界书条目身份，请让程序分配新条目身份")


def entry_destination(document, pointer):
    parts = jsonpointer.JsonPointer(pointer).parts
    root = ["data"] if card_root(document) else []
    return parts[:-1] == [*root, "character_book", "entries"]
