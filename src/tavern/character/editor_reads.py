"""Window-local evidence of complete card values; never sent to the model."""
import jsonpointer

from tavern.core.cache_policy import fingerprint
from tavern.core.model_view import hidden_field


def contains(parent, path):
    return parent == "" or path == parent or path.startswith(parent + "/")


def remember(reads, ref, document, path):
    """Index subtrees so a changed sibling does not invalidate an unchanged field."""
    value = jsonpointer.resolve_pointer(document, path)
    records = reads.setdefault(ref, {})
    for previous in list(records):
        if contains(path, previous):
            del records[previous]

    def visit(value, pointer):
        records[pointer] = fingerprint(value)
        children = value.items() if isinstance(value, dict) else enumerate(value) if isinstance(value, list) else []
        for key, child in children:
            if not hidden_field(str(key)):
                visit(child, pointer + "/" + str(key).replace("~", "~0").replace("/", "~1"))

    visit(value, path)


def known(reads, ref, document, path):
    expected = reads.get(ref, {}).get(path)
    if expected is None:
        return False
    try:
        return expected == fingerprint(jsonpointer.resolve_pointer(document, path))
    except jsonpointer.JsonPointerException:
        return False


def after_patch(reads, ref, before, after, operations):
    """Carry known deterministic writes without blessing unseen array neighbours."""
    paths = [op[key] for op in operations for key in ("path", "from") if key in op]
    covering = [path for path in reads.get(ref, {})
                if all(contains(path, target) for target in paths) and known(reads, ref, before, path)
                and not any((op["op"] == "remove" and op["path"] == path)
                            or (op["op"] == "move" and op["from"] == path) for op in operations)]
    if covering:
        path = min(covering, key=len)
        try:
            remember(reads, ref, after, path)
            return
        except jsonpointer.JsonPointerException:
            pass  # A removed subtree has no current value to remember.
    for op in operations:
        if op["op"] not in {"add", "replace", "copy", "move"}:
            continue
        try:
            value = (jsonpointer.resolve_pointer(before, op["from"]) if op["op"] in {"copy", "move"}
                     else op["value"])
            path = op["path"]
            if path.endswith("/-"):
                parent = path.rpartition("/")[0]
                array = jsonpointer.resolve_pointer(after, parent)
                if isinstance(array, list):
                    path = parent + "/" + str(len(array) - 1)
            if fingerprint(jsonpointer.resolve_pointer(after, path)) == fingerprint(value):
                remember(reads, ref, after, path)
        except jsonpointer.JsonPointerException:
            continue
