"""A navigable view of the current draft. No directory data is written into a card."""

from collections import Counter, defaultdict
from dataclasses import dataclass, field

import jsonpointer

from tavern.character.cards import card_root, card_view, is_initvar, lore_entry_view, lore_title
from tavern.character.editor_config import FIELD_PURPOSES
from tavern.character.editor_reads import relocate
from tavern.character.lore_purpose import NARRATIVE, lore_purpose, lore_rows
from tavern.core.cache_policy import fingerprint
from tavern.core.model_view import hidden_field

# Properties useful for locating or configuring a document. Arbitrary extension
# payloads (which can themselves contain entire scripts) remain available via raw.
ATTRIBUTES = {
    "entries": frozenset(
        """comment name keys key secondary_keys keysecondary enabled disable constant selective
        selectiveLogic selective_logic insertion_order order position depth role probability useProbability
        scan_depth scanDepth caseSensitive case_sensitive matchWholeWords match_whole_words use_regex
        excludeRecursion exclude_recursion preventRecursion prevent_recursion group groupWeight groupOverride
        group_weight group_override cooldown delay sticky""".split()
    ),
    "regex": frozenset(
        """scriptName name findRegex placement disabled markdownOnly promptOnly runOnEdit
        substituteRegex minDepth maxDepth trimStrings""".split()
    ),
    "scripts": frozenset({"name", "enabled", "type"}),
}


def child(path, key):
    return path + "/" + jsonpointer.escape(str(key))


def preview(text, size=80):
    return " ".join(text[:size].split())


@dataclass
class Document:
    branch: str
    path: str
    value: object
    title: str
    body: str | None = None  # Relative pointer. Empty selects the value itself.
    identity: str = ""
    labels: dict = field(default_factory=dict)
    ref: str = ""

    @property
    def text(self):
        if self.body is not None:
            return jsonpointer.resolve_pointer(self.value, self.body)
        return None

    def listing(self, project, compact=False):
        row = {"ref": self.ref, "title": self.title}
        if "group" in self.labels:
            row["group"] = self.labels["group"]
        if self.branch == "openings" or self.labels.get("untitled"):
            row["preview"] = preview(project(self.text or ""))
        if self.labels.get("enabled") is False:
            row["enabled"] = False
        if self.labels.get("constant"):
            row["constant"] = True
        if self.labels.get("purpose", NARRATIVE) != NARRATIVE:
            row["purpose"] = self.labels["purpose"]
        if not compact and isinstance(self.text, str):
            row["chars"] = len(self.text)
        return row

    def matches(self, branch):
        return (
            branch == "all"
            or self.branch == branch
            or (branch == "constants" and self.labels.get("constant_narrative") is True)
        )

    def attribute_paths(self):
        keys = ATTRIBUTES.get(self.branch, ())
        if not isinstance(self.value, dict) or not keys:
            return
        for key in self.value:
            if key in keys:
                yield child("", key)
        if self.branch == "entries":
            ext = self.value.get("extensions", {})
            for key in ext:
                if key in keys:
                    yield child("/extensions", key)
            if "purpose" in (ext.get("terrarium") or {}):
                yield "/extensions/terrarium/purpose"


def script_documents(items, path, groups, active=True):
    if not isinstance(items, list):
        return
    for index, raw in enumerate(items):
        here = child(path, index)
        if isinstance(raw, list) and len(raw) == 2:
            raw, here = raw[1], child(here, 1)
        if not isinstance(raw, dict):
            continue
        name = raw.get("name") or f"脚本 {index + 1}"
        enabled = active and raw.get("enabled", True)
        if isinstance(raw.get("scripts"), list):
            yield from script_documents(raw["scripts"], child(here, "scripts"), [*groups, name], active)
        else:
            yield Document(
                "scripts",
                here,
                raw,
                name,
                "/content" if isinstance(raw.get("content"), str) else None,
                str(raw.get("id", "")),
                {"group": groups, "enabled": enabled},
            )


def documents(document):
    data = card_view(document)
    root = card_root(document)
    if root:
        for key, value in document.items():
            if key != "data" and not hidden_field(key):
                path = child("", key)
                yield Document("fields", path, value, key, "" if isinstance(value, str) else None, ref=path)
    for key, value in data.items():
        if key in {"character_book", "alternate_greetings", "first_mes", "extensions"} or hidden_field(key):
            continue
        path = child(root, key)
        yield Document(
            "fields",
            path,
            value,
            FIELD_PURPOSES.get(key, key),
            "" if isinstance(value, str) else None,
            ref=path,
        )
    if "first_mes" in data:
        value = data["first_mes"]
        yield Document(
            "openings",
            child(root, "first_mes"),
            value,
            "开场 1（默认）",
            "",
            labels={"opening": 1},
        )
    for index, text in enumerate(data.get("alternate_greetings", []), 2):
        yield Document(
            "openings",
            child(root + "/alternate_greetings", index - 2),
            text,
            f"开场 {index}",
            "",
            labels={"opening": index},
        )
    book = data.get("character_book") or {}
    book_path = root + "/character_book"
    for key, value in book.items():
        if key != "entries" and not hidden_field(key):
            path = child(book_path, key)
            yield Document("fields", path, value, key, "" if isinstance(value, str) else None, ref=path)
    keyed = isinstance(book.get("entries"), dict)
    for index, (slot, raw) in enumerate(lore_rows(book)):
        e = lore_entry_view(slot, raw, keyed, index)
        purpose = lore_purpose(raw)
        ext = raw.get("extensions", {})
        body = "/extensions/content" if ext.get("content") is not None else "/content"
        title = lore_title(e)
        labels = {
            "purpose": purpose,
            "enabled": e["enabled"],
            "constant": bool(e.get("constant")),
            "constant_narrative": bool(
                e["enabled"] and e.get("constant") and purpose == NARRATIVE and not is_initvar(e)
            ),
        }
        if not title:
            labels["untitled"] = True
        native_id = e["id"] if keyed or any(k in m for k in ("id", "uid") for m in (raw, ext)) else ""
        yield Document(
            "entries",
            child(book_path + "/entries", slot),
            raw,
            title or f"条目 {index + 1}",
            body
            if isinstance(e.get("content"), str)
            and jsonpointer.resolve_pointer(raw, body, default=None) is not None
            else None,
            native_id,
            labels,
        )
    extensions = data.get("extensions", {})
    for key, value in extensions.items():
        if key == "regex_scripts":
            for index, rule in enumerate(value if isinstance(value, list) else []):
                if isinstance(rule, dict):
                    yield Document(
                        "regex",
                        child(root + "/extensions/regex_scripts", index),
                        rule,
                        rule.get("scriptName") or rule.get("name") or f"正则 {index + 1}",
                        "/replaceString" if isinstance(rule.get("replaceString"), str) else None,
                        str(rule.get("id", "")),
                        {"enabled": not rule.get("disabled", False)},
                    )
        elif not hidden_field(key):
            path = child(root + "/extensions", key)
            yield Document("extensions", path, value, key, "" if isinstance(value, str) else None, ref=path)
    helper = extensions.get("tavern_helper", {})
    helper_path = root + "/extensions/tavern_helper"
    helper_scripts = None
    if isinstance(helper, dict):
        helper_scripts = helper.get("scripts")
        yield from script_documents(helper_scripts, child(helper_path, "scripts"), ["Tavern Helper"])
    elif isinstance(helper, list):
        for index, pair in enumerate(helper):
            if isinstance(pair, list) and len(pair) == 2 and pair[0] == "scripts":
                helper_scripts = pair[1]
                yield from script_documents(
                    helper_scripts, child(child(helper_path, index), 1), ["Tavern Helper"]
                )
    yield from script_documents(
        extensions.get("TavernHelper_scripts"),
        root + "/extensions/TavernHelper_scripts",
        ["TavernHelper_scripts"],
        helper_scripts is None,
    )


class DocumentDirectory:
    """Persist only handles, paths and comparison evidence; ambiguous external changes retire a handle."""

    def __init__(self, owner, state=None):
        self.owner = owner
        self.state = state or {"next": 1, "cards": {}}
        self.cache = {}

    def index(self, card, pinned=None):
        ref, document = card["ref"], card["document"]
        cached = self.cache.get(ref)
        if cached and cached[0] is document and pinned is None:
            return cached[1]
        nodes = list(documents(document))
        old = self.state["cards"].get(ref, {})
        by_id, by_text = defaultdict(list), defaultdict(list)
        for handle, (branch, path, identity, digest) in old.items():
            if identity:
                by_id[branch, identity].append(handle)
            by_text[branch, digest].append(handle)
        movable = [n for n in nodes if not n.ref]
        digests = {n.path: fingerprint(n.value) for n in movable}
        counts = Counter((n.branch, digests[n.path]) for n in movable)
        ids = Counter((n.branch, n.identity) for n in movable if n.identity)
        old_paths = {record[1]: handle for handle, record in old.items()}
        # Identical unkeyed siblings can retain positions only when the branch is
        # unchanged. Never guess after an ambiguous reorder or an external rewrite.
        unchanged = {
            branch
            for branch in {n.branch for n in movable}
            if [(n.path, digests[n.path]) for n in movable if n.branch == branch]
            == [(p, d) for b, p, _, d in old.values() if b == branch]
        }
        records = {}
        reserved = set((pinned or {}).values())
        for n in movable:
            digest = digests[n.path]
            candidates = by_id[n.branch, n.identity] if n.identity and ids[n.branch, n.identity] == 1 else []
            if len(candidates) != 1 and counts[n.branch, digest] == 1:
                candidates = by_text[n.branch, digest]
            handle = (pinned or {}).get(n.path)
            if not handle:
                handle = (
                    candidates[0]
                    if len(candidates) == 1
                    else old_paths.get(n.path)
                    if n.branch in unchanged
                    else None
                )
                if handle in reserved:
                    handle = None
            if not handle or handle in records:
                handle = f"N{self.state['next']}"
                self.state["next"] += 1
            n.ref = handle
            records[handle] = [n.branch, n.path, n.identity, digest]
        relocate(self.owner.reads, ref, old, records)
        self.state["cards"][ref] = records
        result = {n.ref: n for n in nodes}
        self.cache[ref] = document, result
        return result

    def resolve(self, card, ref):
        node = self.index(card).get(ref)
        if node is None:
            raise ValueError("该文档引用已失效或不属于此卡，请重新列目录或搜索")
        return node

    def prune(self, refs):
        for ref in self.state["cards"].keys() - set(refs):
            del self.state["cards"][ref]
            self.cache.pop(ref, None)
