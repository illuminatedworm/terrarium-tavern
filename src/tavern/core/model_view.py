"""Small, reversible views for tools that edit structured source documents.

Persistent identities and content checksums are bookkeeping, not reasoning input.
Tools return only short local handles; a run-local map restores identifiers before
applying edits. Hash fields are omitted and preserved by Runtime, including when
an agent replaces a parent object. Never store this view as the real document.
Free-form source code is kept intact except for reversible opaque-token handles.
"""
import copy
import re
import uuid

import jsonpatch
import jsonpointer

OPAQUE = re.compile(r"(?<![\w])(?:[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}|[0-9a-fA-F]{24,})(?![\w])")
HANDLE = re.compile(r"@D[1-9]\d*\b")
IDENTITY_FIELDS = {"id", "uid", "source", "message_id", "source_message_id", "anchor_id", "split_from"}
STATE_BOOKKEEPING = {"initialized_lorebooks", "initialized_openings", "schema", "display_data", "delta_data"}


def state_for_model(state):
    """Keep actual story values; initialization ledgers and MVU internals stay local.

    Filtering is structural, not a global rule deleting every user variable named
    'id' or 'hash'. Both flat state and stat_data are supported. Runtime receives
    the complete original state; this projection must never be committed back.
    """
    def values(value):
        if isinstance(value, dict):
            return {k: values(v) for k, v in value.items() if k not in {"$internal", "$meta", "$arrayMeta"}}
        if isinstance(value, list):
            return [values(v) for v in value if v != "$__META_EXTENSIBLE__$"]
        return copy.deepcopy(value)
    return values({k: v for k, v in state.items() if k not in STATE_BOOKKEEPING})


def hidden_field(key):
    return key in {"fingerprint", "checksum", "sha256", "source_hash"} or key.endswith("_hash")


class ModelView:
    def __init__(self, value=None):
        self.forward, self.reverse = {}, {}
        self.reserved = set(HANDLE.findall(str(value)))
        self.seed(value)

    def bind(self, value):
        if value not in self.forward:
            number = len(self.reverse) + 1
            while f"@D{number}" in self.reverse or f"@D{number}" in self.reserved:
                number += 1
            handle = f"@D{number}"
            self.forward[value] = handle
            self.reverse[handle] = value
        return self.forward[value]

    def seed(self, value):
        if isinstance(value, dict):
            for key, item in value.items():
                if hidden_field(str(key)):
                    continue  # Never allocate handles for omitted bookkeeping.
                if isinstance(item, str) and key in IDENTITY_FIELDS and len(item) > 12:
                    self.bind(item)
                if len(str(key)) > 24:
                    self.bind(str(key))
                self.seed(item)
        elif isinstance(value, list):
            for item in value:
                self.seed(item)
        elif isinstance(value, str):
            for match in OPAQUE.finditer(value):
                self.bind(match[0])

    def text(self, text):
        # Register before replacement, including UUIDs in code and error text.
        # Restore is exact and run-local, not fuzzy matching or substring guessing.
        self.seed(text)
        if not self.forward:
            return text
        pattern = "|".join(re.escape(raw) for raw in sorted(self.forward, key=len, reverse=True))
        return re.sub(pattern, lambda m: self.forward[m[0]], text)

    def expose(self, value):
        self.seed(value)
        if isinstance(value, dict):
            return {self.text(str(k)): self.expose(v) for k, v in value.items() if not hidden_field(str(k))}
        if isinstance(value, list):
            return [self.expose(v) for v in value]
        if isinstance(value, str):
            return self.text(value)
        return value

    def restore(self, value):
        if isinstance(value, str):
            def replace(match):
                if match[0] in self.reserved:
                    return match[0]
                if match[0] not in self.reverse:
                    raise ValueError("未知资料短编号；请使用本次工具返回的编号")
                return self.reverse[match[0]]
            return HANDLE.sub(replace, value)
        if isinstance(value, dict):
            return {self.restore(k): self.restore(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.restore(v) for v in value]
        return value

    def messages(self, messages):
        """Project business text only; native call IDs remain transport-owned."""
        rows = copy.deepcopy(messages)
        for row in rows:
            if isinstance(row.get("content"), str):
                row["content"] = self.text(row["content"])
            for call in row.get("tool_calls", []):
                call["function"]["arguments"] = self.text(call["function"]["arguments"])
        return rows

    def pointer(self, path, *, restore=False):
        # Decode JSON Pointer escapes before mapping keys, and re-escape after
        # restoring them. A key containing '/' or '~' remains exactly one key.
        convert = self.restore if restore else self.text
        return "".join("/" + convert(part).replace("~", "~0").replace("/", "~1")
                       for part in jsonpointer.JsonPointer(path).parts)

    def operations(self, operations):
        return [{k: self.pointer(v, restore=True) if k in ("path", "from") else self.restore(v)
                 for k, v in raw.items()} for raw in operations]

    def patch(self, document, operations):
        """Apply to the original, preserving fields the model was never shown.

        JSON Pointers may contain dictionary keys exposed as handles; translate
        paths and values together. Hidden hash fields are always Runtime-owned.
        A failed patch leaves the caller's original document and hashes untouched.
        """
        result = copy.deepcopy(document)
        for operation in self.operations(operations):
            path = operation.get("path", "")
            if any(hidden_field(part) for part in jsonpointer.JsonPointer(path).parts):
                raise ValueError("指纹和哈希由程序维护，不接受模型修改；开场请使用 set_opening")
            if "from" in operation and any(hidden_field(part) for part in jsonpointer.JsonPointer(operation["from"]).parts):
                raise ValueError("模型不能读取或复制内部哈希")
            op = copy.deepcopy(operation)
            if op.get("op") in ("add", "replace") and "value" in op:
                old = jsonpointer.resolve_pointer(result, path, default=None)
                parent = jsonpointer.resolve_pointer(result, path.rpartition("/")[0], default=None)
                if op["op"] == "add" and path and isinstance(parent, list):
                    old = None  # Array insertion must not inherit the next item's ID/hash.
                op["value"] = preserve_hidden(op["value"], old)
            result = jsonpatch.apply_patch(result, [op])
        # New lore IDs are assigned by Runtime. Existing implicit-index entries
        # are left alone unless they were actually changed or newly introduced.
        old_data = document.get("data", document) if document.get("spec") else document
        old_entries = old_data.get("character_book", {}).get("entries", [])
        old_entries = list(old_entries.values()) if isinstance(old_entries, dict) else old_entries
        new_data = result.get("data", result) if result.get("spec") else result
        new_entries = new_data.get("character_book", {}).get("entries", [])
        new_entries = list(new_entries.values()) if isinstance(new_entries, dict) else new_entries
        for entry in new_entries:
            if "id" not in entry and not any(entry == old for old in old_entries):
                entry["id"] = uuid.uuid4().hex
        return result


def preserve_hidden(new, old):
    """Only match existing identities; never copy a replaced item's metadata."""
    if isinstance(new, dict):
        if any(hidden_field(str(k)) for k in new):
            raise ValueError("指纹和哈希由程序维护，请勿在模型编辑内容中提供")
        old = old if isinstance(old, dict) else {}
        if new.get("id") is not None and old.get("id") != new["id"]:
            old = {}
        result = {k: preserve_hidden(v, old.get(k)) for k, v in new.items()}
        if "id" in old and "id" not in new:
            result["id"] = copy.deepcopy(old["id"])
        result.update({k: copy.deepcopy(v) for k, v in old.items() if hidden_field(str(k))})
        return result
    if isinstance(new, list):
        old = old if isinstance(old, list) else []
        by_id = {v["id"]: v for v in old if isinstance(v, dict) and isinstance(v.get("id"), (str, int))}
        def previous(value, index):
            if isinstance(value, dict) and "id" in value:
                return by_id.get(value["id"])
            candidate = old[index] if index < len(old) else None
            return None if isinstance(candidate, dict) and "id" in candidate else candidate
        return [preserve_hidden(v, previous(v, i)) for i, v in enumerate(new)]
    return new
