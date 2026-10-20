"""Archive of episodes that retention removed from the active memory projection.

Each message snapshot stores only its own delta under ``memory_retired``. The
branch archive is the chain fold of those deltas, so forks inherit their prefix
and regeneration sees its pre-turn state without copying a growing list into
every snapshot. Drafts collect this turn's retirements under ``memory_retiring``;
every persisting gate calls ``settle_archive`` with the parent chain's entries
so a draft's carried copy of an ancestor delta is dropped, never duplicated.

Entries are keyed by record ID and later deltas win. A tombstone
``{"id": ..., "removed": True}`` hides an entry (manual delete, rebuild).
"""

import copy

from tavern.memories.memory_records import CATEGORIES, render_record, with_source_status

ARCHIVE = "memory_retired"
RETIRING = "memory_retiring"
TRANSIENT_FIELDS = ("source_stale",)


def archive_entry(record, archived_round):
    """Freeze a retired record; drop UI-only source status so copies compare equal."""
    entry = {k: copy.deepcopy(v) for k, v in record.items() if k not in TRANSIENT_FIELDS}
    for source in entry.get("sources", []):
        source.pop("state", None)
    entry["archived_round"] = archived_round
    return entry


def _snapshot(message):
    snapshot = message.get("snapshot", {})
    return snapshot if isinstance(snapshot, dict) else {}


def inherited_entries(history):
    """Raw ``id → entry`` map (tombstones included) of the chain ending at ``history[-1]``."""
    result = {}
    for message in history:
        for entry in _snapshot(message).get(ARCHIVE, []):
            result[entry["id"]] = entry
    return result


def read_archive(history, snapshot=None):
    """Effective archive of a branch; ``snapshot`` adds an uncommitted draft's delta."""
    raw = inherited_entries(history)
    if snapshot is not None:
        for entry in [*snapshot.get(ARCHIVE, []), *snapshot.get(RETIRING, [])]:
            raw[entry["id"]] = entry
    return [copy.deepcopy(e) for e in raw.values() if not e.get("removed")]


def settle_archive(snapshot, inherited):
    """Persist only this message's own entries: the delta against its parent chain."""
    own = {}
    for entry in [*snapshot.get(ARCHIVE, []), *snapshot.pop(RETIRING, [])]:
        own[entry["id"]] = entry
    delta = [entry for rid, entry in own.items() if inherited.get(rid) != entry]
    if delta:
        snapshot[ARCHIVE] = delta
    else:
        snapshot.pop(ARCHIVE, None)
    return snapshot


def retire_all(snapshot, history):
    """Tombstone every automatic entry; a rebuild re-derives them, user edits stay."""
    snapshot.setdefault(RETIRING, []).extend(
        {"id": entry["id"], "removed": True}
        for entry in read_archive(history, snapshot)
        if not entry.get("manual")
    )


def edit_archive(snapshot, history, edit):
    """Apply a user's archive edit or delete to a head snapshot's pending delta."""
    entry = next((e for e in read_archive(history, snapshot) if e["id"] == edit.id), None)
    if entry is None:
        raise ValueError("记忆条目不存在")
    if edit.action == "delete":
        changed = {"id": entry["id"], "removed": True}
    elif edit.action == "edit":
        if not edit.content.strip():
            raise ValueError("记忆内容不能为空")
        # Archived records stay episodes; the edit only revises wording and kind.
        changed = {**entry, "content": edit.content.strip(), "kind": edit.kind, "manual": True}
    else:
        raise ValueError("归档记忆不支持新增")
    snapshot.setdefault(RETIRING, []).append(changed)


def with_archive_status(entries, history):
    return with_source_status({"records": entries}, history)["records"]


def retrieval_entries(entries):
    """Archive records in the shape the lore BM25/vector/rerank helpers index."""
    return [
        {
            "id": "memory:" + e["id"],
            "content": e["content"],
            "keys": list(e.get("people", [])),
            "comment": "",
            "_record": e,
        }
        for e in entries
    ]


def render_recall(entries):
    """Writer text for recalled archive entries; labels mirror the active projection."""
    lines = [render_record(entry) for entry in entries]
    if not lines:
        return ""
    return (
        "<tavern_memory>\n相关旧事（按当前剧情从已归档的"
        + CATEGORIES["episode"]
        + "召回；仅是过去发生的经历，当前状态以记忆与剧情状态为准）：\n"
        + "\n".join(lines)
        + "\n</tavern_memory>"
    )


def remap_archive(snapshot, ids):
    for key in (ARCHIVE, RETIRING):
        for entry in snapshot.get(key, []):
            for source in entry.get("sources", []):
                source["message_id"] = ids.get(source["message_id"], source["message_id"])
            for setting in entry.get("setting_sources", []):
                setting["anchor"] = ids.get(setting.get("anchor"), setting.get("anchor"))
