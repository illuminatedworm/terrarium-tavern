"""Atomic document transfers between working drafts; library saves stay per card."""

import copy
from uuid import uuid4

import jsonpointer

from tavern.character.card_workflow import validate_document
from tavern.character.cards import card_root, card_view
from tavern.character.editor_documents import child, documents
from tavern.character.editor_lore import LoreIdentities, assign_id, fresh_entry, identity, rows
from tavern.character.editor_worldbook import is_worldbook, validate_worldbook_document


def script_list(data, root):
    ext = data.setdefault("extensions", {})
    helper = ext.get("tavern_helper", {})
    path = root + "/extensions/tavern_helper"
    if isinstance(helper, list):
        for i, pair in enumerate(helper):
            if isinstance(pair, list) and len(pair) == 2 and pair[0] == "scripts":
                return pair[1], child(child(path, i), 1)
    elif "scripts" in helper:
        return helper["scripts"], child(path, "scripts")
    if "TavernHelper_scripts" in ext:
        return ext["TavernHelper_scripts"], root + "/extensions/TavernHelper_scripts"
    if isinstance(helper, list):
        helper.append(["scripts", []])
        return helper[-1][1], child(child(path, len(helper) - 1), 1)
    ext.setdefault("tavern_helper", {})["scripts"] = []
    return ext["tavern_helper"]["scripts"], child(path, "scripts")


def remove_at(doc, path):
    parent, _, key = path.rpartition("/")
    container = jsonpointer.resolve_pointer(doc, parent)
    if isinstance(container, list):
        container.pop(int(key))
    else:
        del container[jsonpointer.unescape(key)]


def remaining_handles(nodes, removed, path, document):
    """Known removal: keep even identically authored sibling documents distinguishable."""
    parent, _, key = path.rpartition("/")
    sequence = isinstance(jsonpointer.resolve_pointer(document, parent), list)
    pins = {}
    for n in nodes.values():
        if n.ref == removed.ref or not n.ref.startswith("N"):
            continue
        target = n.path
        if n.branch == removed.branch == "openings":
            number = n.labels["opening"] - int(n.labels["opening"] > removed.labels["opening"])
            target = card_root(document) + (
                "/first_mes" if number == 1 else f"/alternate_greetings/{number - 2}"
            )
        elif sequence and target.startswith(parent + "/"):
            index, separator, suffix = target[len(parent) + 1 :].partition("/")
            if int(index) > int(key):
                target = child(parent, int(index) - 1) + separator + suffix
        pins[target] = n.ref
    return pins


def transfer(tools, args):
    source = tools.card(args.card_ref)
    dest = tools.card(args.destination_card_ref)
    node = tools.document_node(args.card_ref, args.ref)
    moving = args.action == "move"
    same = source is dest
    old_dest = tools.directory.index(dest)
    changed = {dest["ref"]: copy.deepcopy(dest["document"])}
    if moving and not same:
        changed[source["ref"]] = copy.deepcopy(source["document"])
    out = changed[dest["ref"]]
    origin = changed.get(source["ref"], source["document"])
    data = card_view(out)
    root, source_root = card_root(out), card_root(origin)
    if is_worldbook(dest) and node.branch not in {"entries", "fields"}:
        raise ValueError("独立世界书只能接收世界书条目及其现有字段")
    src_path = node.path
    raw = copy.deepcopy(node.value)
    if node.branch == "scripts":
        parent = src_path.rpartition("/")[0]
        pair = jsonpointer.resolve_pointer(origin, parent)
        if isinstance(pair, list) and len(pair) == 2 and isinstance(pair[0], str):
            src_path, raw = parent, copy.deepcopy(pair)
    source_pins = (
        remaining_handles(tools.directory.index(source), node, src_path, source["document"]) if moving else {}
    )
    identities = (
        {ref: LoreIdentities(doc) for ref, doc in changed.items()} if node.branch == "entries" else {}
    )
    if moving:
        if node.branch == "openings" and node.labels["opening"] == 1:
            original = card_view(origin)
            alternatives = original.get("alternate_greetings") or []
            if not alternatives:
                raise ValueError("必须保留一个默认开场，请复制或先添加另一个开场")
            original["first_mes"] = alternatives.pop(0)
        else:
            remove_at(origin, src_path)
    if node.branch == "entries":
        if data.get("character_book") is None:
            data["character_book"] = {}
        entries = data["character_book"].setdefault("entries", [])
        if moving:
            slot = jsonpointer.JsonPointer(node.path).parts[-1]
            source_entries = (card_view(source["document"])["character_book"])["entries"]
            eid = identity(slot, raw, isinstance(source_entries, dict))
            if not same and any(
                identity(k, entry, isinstance(entries, dict)) == eid for k, entry in rows(entries)
            ):
                raise ValueError("目标世界书已有相同身份的条目，移动未执行")
            # An implicit slot/index must not change meaning in a new collection.
            if "id" not in raw and "id" not in raw.get("extensions", {}):
                assign_id(raw, eid)
        else:
            eid = fresh_entry(raw)
        if isinstance(entries, dict):
            key = eid if eid not in entries else uuid4().hex
            entries[key] = raw
        else:
            key = len(entries)
            entries.append(raw)
        target_path = child(root + "/character_book/entries", key)
    elif node.branch == "openings":
        if "first_mes" not in data and not data.get("alternate_greetings"):
            data["first_mes"] = raw
            target_path = root + "/first_mes"
        else:
            array = data.setdefault("alternate_greetings", [])
            target_path = child(root + "/alternate_greetings", len(array))
            array.append(raw)
    elif node.branch in {"regex", "scripts"}:
        if node.branch == "regex":
            array = data.setdefault("extensions", {}).setdefault("regex_scripts", [])
            path = root + "/extensions/regex_scripts"
        elif same:
            path = src_path.rpartition("/")[0]
            array = jsonpointer.resolve_pointer(out, path)
        else:
            array, path = script_list(data, root)
        script = raw[1] if isinstance(raw, list) else raw
        if not moving:
            script["id"] = uuid4().hex
        elif (
            moving
            and not same
            and node.identity
            and any(
                n.branch == node.branch and n.identity == node.identity
                for n in tools.directory.index(dest).values()
            )
        ):
            raise ValueError("目标卡已有相同身份的脚本，移动未执行")
        target_path = child(path, len(array))
        array.append(raw)
        if isinstance(raw, list):
            target_path = child(target_path, 1)
    else:
        if source_root and not node.path.startswith(source_root + "/"):
            if not root:
                raise ValueError("目标文档没有对应的外层格式字段")
            target_path = node.path
        else:
            target_path = root + node.path.removeprefix(source_root)
        if same:
            raise ValueError("同一卡内的固定字段具有唯一位置")
        parent, _, key = target_path.rpartition("/")
        if parent == root + "/extensions":
            data.setdefault("extensions", {})
        elif parent == root + "/character_book" and data.get("character_book") is None:
            data["character_book"] = {"entries": []}
        container = jsonpointer.resolve_pointer(out, parent)
        key = jsonpointer.unescape(key)
        if key in container:
            tools.require_read(dest["ref"], target_path)
        container[key] = raw
    items = {item["ref"]: item for item in (source, dest)}
    for ref, doc in changed.items():
        if ref in identities:
            identities[ref].finish(doc)
        (validate_worldbook_document if is_worldbook(items[ref]) else validate_document)(doc)
    if not any(n.path == target_path for n in documents(out)):
        raise ValueError("目标位置无法表示同类文档，草稿未修改")
    # Both sides have passed validation before either staged document is published.
    for ref, doc in changed.items():
        items[ref].update(document=doc, kind="draft")
        pins = (
            source_pins.copy()
            if moving and ref == source["ref"]
            else {n.path: n.ref for n in old_dest.values() if n.ref.startswith("N") and n.path != target_path}
        )
        if moving and ref == dest["ref"] and node.ref.startswith("N"):
            pins[target_path] = node.ref
        tools.directory.index(items[ref], pins)
    target = next(n for n in tools.directory.index(dest).values() if n.path == target_path)
    tools.owner.event("draft_change", {"card_ref": dest["ref"], "reason": args.comment})
    return {
        "accepted": True,
        "action": args.action,
        "card_ref": dest["ref"],
        "ref": tools.public_ref(target),
        "path": tools.view.pointer(target_path),
        "source_removed": moving,
    }
