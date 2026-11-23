"""Small, reversible views for tools that edit structured source documents.

Persistent identities and content checksums are bookkeeping, not reasoning input.
Tools return only short local handles; a run-local map restores identifiers before
applying edits. Hash fields are omitted and preserved by Runtime, including when
an agent replaces a parent object. Never store this view as the real document.
Free-form source code is kept intact except for reversible opaque-token handles.
"""

import copy
import re

import jsonpatch
import jsonpointer

from tavern.character.editor_lore import LoreIdentities, append_path, entry_destination, fresh_entry

OPAQUE = re.compile(
    r"(?<![\w])(?:[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}|[0-9a-fA-F]{24,})(?![\w])"
)
HANDLE = re.compile(r"@D[1-9]\d*\b")
IDENTITY_FIELDS = {"id", "uid", "source", "message_id", "source_message_id", "anchor_id", "split_from"}
STATE_BOOKKEEPING = {"initialized_lorebooks", "schema", "display_data", "delta_data"}


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
    def __init__(self, value=None, *, references=None):
        self.reverse = references if references is not None else {}
        self.forward = {raw: ref for ref, raw in self.reverse.items()}
        self._pattern = self._compiled_for = None
        self._history_only = set(self.forward)
        self._authored_pattern = None
        self.reserved = set(HANDLE.findall(str(value))).difference(self.reverse)
        self.seed(value)

    def bind(self, value):
        if value not in self.forward:
            number = len(self.reverse) + 1
            while f"@D{number}" in self.reverse or f"@D{number}" in self.reserved:
                number += 1
            handle = f"@D{number}"
            self.forward[value] = handle
            self.reverse[handle] = value
            self._pattern = self._authored_pattern = None
        elif value in self._history_only:
            self._history_only.remove(value)
            self._authored_pattern = None
        return self.forward[value]

    def seed(self, value, *, authored=False):
        if isinstance(value, dict):
            for key, item in value.items():
                if authored:
                    self.seed(str(key))  # Only intrinsic opaque tokens in a program-defined key.
                else:
                    if hidden_field(str(key)):
                        continue  # Never allocate handles for omitted bookkeeping.
                    if isinstance(item, str) and key in IDENTITY_FIELDS and len(item) > 12:
                        self.bind(item)
                    if len(str(key)) > 24 and key != "post_history_instructions":
                        self.bind(str(key))
                self.seed(item, authored=authored)
        elif isinstance(value, list):
            for item in value:
                self.seed(item, authored=authored)
        elif isinstance(value, str):
            for match in OPAQUE.finditer(value):
                self.bind(match[0])

    def text(self, text):
        # Register before replacement, including UUIDs in code and error text.
        # Restore is exact and run-local, not fuzzy matching or substring guessing.
        self.seed(text)
        if not self.forward:
            return text
        if self._pattern is None or self._compiled_for is not self.forward:
            self._pattern = re.compile(
                "|".join(re.escape(raw) for raw in sorted(self.forward, key=len, reverse=True))
            )
            self._compiled_for = self.forward
        return self._pattern.sub(lambda m: self.forward[m[0]], text)

    def protocol_text(self, text):
        """Bundled instructions own their field/tool names; only intrinsic opaque tokens need handles."""
        return OPAQUE.sub(lambda match: self.bind(match[0]), text)

    def expose(self, value, *, authored=False):
        self.seed(value, authored=authored)
        if authored and self._history_only:
            # A sandbox owns its JSON field names. Older saves may have bound those
            # names as document IDs. Keep their handles for replay, but only redact
            # identities observed in live source data and intrinsically opaque tokens.
            if self._authored_pattern is None:
                known = sorted(self.forward.keys() - self._history_only, key=len, reverse=True)
                self._authored_pattern = re.compile("|".join(map(re.escape, known))) if known else OPAQUE
            pattern = self._authored_pattern

            def text(raw):
                return pattern.sub(lambda match: self.forward[match[0]], raw)
        else:
            text = self.text

        def project(item):
            if isinstance(item, dict):
                return {
                    text(str(k)): project(v) for k, v in item.items() if authored or not hidden_field(str(k))
                }
            if isinstance(item, list):
                return [project(v) for v in item]
            return text(item) if isinstance(item, str) else item

        return project(value)

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
            if row.get("_native_response"):
                continue  # Opaque provider blocks and their canonical text must agree exactly.
            if isinstance(row.get("content"), str):
                row["content"] = self.text(row["content"])
            if isinstance(row.get("runtime_content"), str):
                row["runtime_content"] = self.text(row["runtime_content"])
            if row.get("runtime_parts"):
                row["runtime_parts"] = [self.text(part) for part in row["runtime_parts"]]
            for call in row.get("tool_calls", []):
                call["function"]["arguments"] = self.text(call["function"]["arguments"])
        return rows

    def pointer(self, path, *, restore=False):
        # Decode JSON Pointer escapes before mapping keys, and re-escape after
        # restoring them. A key containing '/' or '~' remains exactly one key.
        convert = self.restore if restore else self.text
        return "".join(
            "/" + convert(part).replace("~", "~0").replace("/", "~1")
            for part in jsonpointer.JsonPointer(path).parts
        )

    def operations(self, operations):
        return [
            {
                k: self.pointer(v, restore=True) if k in ("path", "from") else self.restore(v)
                for k, v in raw.items()
            }
            for raw in operations
        ]

    def patch(self, document, operations, *, check=None, applied=None):
        return self.patch_original(document, self.operations(operations), check=check, applied=applied)

    @staticmethod
    def patch_original(document, operations, *, check=None, applied=None):
        """Apply to the original, preserving fields the model was never shown.

        JSON Pointers may contain dictionary keys exposed as handles; translate
        paths and values together. Hidden hash fields are always Runtime-owned.
        A failed patch leaves the caller's original document and hashes untouched.
        """
        result = copy.deepcopy(document)
        identities = LoreIdentities(result)
        for raw in operations:
            path = append_path(result, raw)
            operation = {**raw, "path": path}
            if any(hidden_field(part) for part in jsonpointer.JsonPointer(path).parts):
                raise ValueError("指纹和哈希由程序维护，不接受模型修改；开场请使用 set_opening")
            if "from" in operation and any(
                hidden_field(part) for part in jsonpointer.JsonPointer(operation["from"]).parts
            ):
                raise ValueError("模型不能读取或复制内部哈希")
            if check is not None:
                check(result, operation)
            op = copy.deepcopy(operation)
            parent = jsonpointer.resolve_pointer(result, path.rpartition("/")[0], default=None)
            new_entry = entry_destination(result, path) and (
                isinstance(parent, list) or jsonpointer.JsonPointer(path).parts[-1] not in (parent or {})
            )
            if op.get("op") in ("add", "replace") and "value" in op:
                old = jsonpointer.resolve_pointer(result, path, default=None)
                if op["op"] == "add" and path and isinstance(parent, list):
                    old = None  # Array insertion must not inherit the next item's ID/hash.
                op["value"] = preserve_hidden(op["value"], old)
                if (
                    new_entry
                    and op["op"] == "add"
                    and isinstance(op["value"], dict)
                    and "id" not in op["value"]
                ):
                    fresh_entry(op["value"])
            if op["op"] == "copy" and entry_destination(result, path):
                value = copy.deepcopy(jsonpointer.resolve_pointer(result, op["from"]))
                fresh_entry(value)
                op = {"op": "add", "path": path, "value": value}
            result = jsonpatch.apply_patch(result, [op], in_place=True)
            if applied is not None:
                applied.append(operation)
        identities.finish(result)
        return result


def _identity(value):
    if isinstance(value, dict):
        for key in ("id", "uid"):
            if isinstance(value.get(key), (str, int)):
                return key, value[key]
    return None


def preserve_hidden(new, old):
    """Only match existing identities; never copy a replaced item's metadata."""
    if isinstance(new, dict):
        if any(hidden_field(str(k)) for k in new):
            raise ValueError("指纹和哈希由程序维护，请勿在模型编辑内容中提供")
        old = old if isinstance(old, dict) else {}
        if _identity(new) is not None and _identity(old) != _identity(new):
            old = {}
        result = {k: preserve_hidden(v, old.get(k)) for k, v in new.items()}
        for key in ("id", "uid"):
            if key in old and key not in new:
                result[key] = copy.deepcopy(old[key])
        result.update({k: copy.deepcopy(v) for k, v in old.items() if hidden_field(str(k))})
        return result
    if isinstance(new, list):
        old = old if isinstance(old, list) else []
        by_id = {ident: v for v in old if (ident := _identity(v)) is not None}

        def previous(value, index):
            if (ident := _identity(value)) is not None:
                return by_id.get(ident)
            candidate = old[index] if index < len(old) else None
            return None if _identity(candidate) is not None else candidate

        return [preserve_hidden(v, previous(v, i)) for i, v in enumerate(new)]
    return new
