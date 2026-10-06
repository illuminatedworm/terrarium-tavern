"""Validate a variable proposal without executing MVU or any card callback."""
import copy

import jsonpatch
import jsonpointer

from tavern.core.model_view import hidden_field, preserve_hidden

METADATA = {"$internal", "$meta", "$arrayMeta"}
FORBIDDEN = METADATA | {"__proto__", "prototype", "constructor"}
MARKER = "$__META_EXTENSIBLE__$"


class ProtectedStateField(ValueError):
    pass


class InvalidStatePath(ValueError):
    pass


class InvalidStatePatch(ValueError):
    pass


def preserve_metadata(value, previous):
    # Validate model-supplied hash fields once. preserve_hidden recursively
    # restores legitimate old hashes; do not re-validate those restored fields
    # as if the model had supplied them when visiting a nested object/array.
    try:
        restored = preserve_hidden(value, previous)
    except ValueError as error:
        raise ProtectedStateField(str(error)) from None
    return _metadata(restored, previous)


def _metadata(value, previous):
    if isinstance(value, dict):
        previous = previous if isinstance(previous, dict) else {}
        if value.keys() & METADATA:
            raise ProtectedStateField("MVU 内部字段由程序维护")
        return {**{k: _metadata(v, previous.get(k)) for k, v in value.items()},
                **{k: copy.deepcopy(v) for k, v in previous.items() if k in METADATA}}
    if isinstance(value, list) and isinstance(previous, list):
        visible = [v for v in previous if v != MARKER]
        by_id = {v["id"]: v for v in visible if isinstance(v, dict) and isinstance(v.get("id"), (str, int))}
        def prior(item, index):
            if isinstance(item, dict) and "id" in item:
                return by_id.get(item["id"])
            candidate = visible[index] if index < len(visible) else None
            return None if isinstance(candidate, dict) and "id" in candidate else candidate
        restored = [_metadata(v, prior(v, i)) for i, v in enumerate(value)]
        if MARKER in previous and MARKER not in restored:
            restored.insert(min(previous.index(MARKER), len(restored)), MARKER)
        return restored
    return value


def pointer(raw, path, *, insertion=False):
    parts = jsonpointer.JsonPointer(path).parts
    actual, current = [], raw
    for i, part in enumerate(parts):
        if part in FORBIDDEN or hidden_field(part):
            raise ProtectedStateField("变量路径包含程序维护字段")
        if isinstance(current, list):
            visible = [j for j, value in enumerate(current) if value != MARKER]
            is_insert = insertion and i == len(parts) - 1
            if part == "-" and is_insert:
                index = visible[-1] + 1 if visible else 0
            elif part.isdigit() and int(part) < len(visible):
                index = visible[int(part)]
            elif part.isdigit() and is_insert and int(part) == len(visible):
                index = visible[-1] + 1 if visible else 0
            else:
                raise InvalidStatePath("变量数组索引无效")
            actual.append(str(index))
            current = current[index] if index < len(current) else None
        else:
            actual.append(part)
            current = current.get(part) if isinstance(current, dict) else None
    return "".join("/" + p.replace("~", "~0").replace("/", "~1") for p in actual)


def stage(raw, operations):
    result, accepted = copy.deepcopy(raw), []
    for operation in operations:
        op = copy.deepcopy(operation)
        action = op.get("op")
        op["path"] = pointer(result, op["path"], insertion=action in ("add", "copy", "move"))
        if "from" in op:
            op["from"] = pointer(result, op["from"])
        if action in ("add", "replace") and "value" in op:
            parent = jsonpointer.resolve_pointer(result, op["path"].rpartition("/")[0], default=None)
            previous = None if action == "add" and isinstance(parent, list) else jsonpointer.resolve_pointer(result, op["path"], default=None)
            op["value"] = preserve_metadata(op["value"], previous)
        result = jsonpatch.apply_patch(result, [op])
        accepted.append(op)
    if not isinstance(result, dict):
        raise InvalidStatePatch("stat_data 必须保持为对象")
    return accepted, result
