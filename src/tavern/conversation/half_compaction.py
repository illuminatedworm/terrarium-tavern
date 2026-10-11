"""Writer checkpoints over recipe-compiled sources, independent of Tracker coverage.

The compiler retains a complete source view for validation/replay. Lore presence
uses a separate active view: encrypting a source must not pin its frozen lore for
the rest of the story. None of these helpers commits a snapshot.
"""
import copy
from uuid import uuid4

from tavern.character.cards import lore_entries
from tavern.conversation.compaction import project_compact
from tavern.core.message_adapter import adapter_snapshot

STATE = "half_context"


def enabled(settings):
    return settings.runtime_mode == "half_agent" and settings.context_strategy == "openai_compact"


def local_window_enabled(settings):
    return settings.chunk_mode != "off" and not enabled(settings)


def lore_key(record):
    return record["anchor_id"], record["id"]


def source_lore(history, snapshot, records=None):
    """Recover frozen originals, with explicit removals winning over older traces.

    Old snapshots are the migration source; new trace records own only each
    exchange's additions. Missing old temporary prompts are never guessed.
    """
    allowed = {m["id"] for m in history}
    result, removed = {}, set()
    for message in history:
        sources = [*(records or {}).get(message["id"], {}).get("lore", []),
                   *message.get("snapshot", {}).get("lore_injections", [])]
        for row in sources:
            if row["anchor_id"] in allowed:
                result[lore_key(row)] = copy.deepcopy(row)
                if row.get("manually_removed"):
                    removed.add(lore_key(row))
    for row in snapshot.get("lore_injections", []):
        result[lore_key(row)] = copy.deepcopy(row)
        if row.get("manually_removed"):
            removed.add(lore_key(row))
    for anchor, eid in removed:
        result[anchor, eid] = {"anchor_id": anchor, "id": eid, "manually_removed": True}
    return list(result.values())


def retired_lore(snapshot):
    return {lore_key(row) for row in snapshot.get(STATE, {}).get("retired_lore", [])}


def active_lore(snapshot):
    retired = retired_lore(snapshot)
    return [row for row in snapshot.get("lore_injections", []) if lore_key(row) not in retired]


def project(result, session, history, snapshot, settings):
    """Project only after recipe layout, append frames and message adaptation.

    Offsets refer to source exchanges, never to SDK roles. The current input
    and all following frames stay outside maintenance even when mapped to a
    developer/assistant role. Custom history overrides cannot borrow coverage
    from the real story's checkpoint.
    """
    rows = copy.deepcopy(result["trace"])
    ids = {m["id"] for m in history}
    record = result.get("half_agent_record", {})
    input_id = record.get("input_id")
    fixed = next((i for i, row in enumerate(rows)
                  if row.get("message_id") in ids or row.get("anchor_id") in ids
                  or row.get("message_id") == input_id), len(rows))
    body = rows[fixed:]
    # A recipe may put the pending input inside an old exchange or repeat its
    # history block. Only cut where no selected source exchange crosses the
    # boundary; overlapping/repeated exchanges form one indivisible batch.
    pending = next((i for i, row in enumerate(body)
                    if row.get("message_id") == input_id or row.get("anchor_id") == input_id
                    or row.get("current_lore")), len(body))
    owners, finals, owner = {}, set(), 0
    for index, message in enumerate(history):
        if message["role"] == "user":
            owner = index
        owners[message["id"]] = owner
        if message["role"] == "assistant":
            finals.add(message["id"])
    ranges, complete_owners = {}, set()
    for position, row in enumerate(body):
        mid = row.get("message_id") or row.get("anchor_id")
        if mid not in owners:
            continue
        owner = owners[mid]
        interval = ranges.setdefault(owner, [position, position])
        interval[1] = position
        if row.get("message_id") in finals:
            complete_owners.add(owner)
    groups = []
    for owner, (start, end) in sorted(ranges.items(), key=lambda value: value[1][0]):
        if groups and start <= groups[-1]["end"]:
            groups[-1]["end"] = max(groups[-1]["end"], end)
            groups[-1]["complete"] &= owner in complete_owners
        else:
            groups.append({"start": start, "end": end, "history_start": owner, "complete": owner in complete_owners})
    pending = min(pending, next((group["start"] for group in groups if not group["complete"]), len(body)))
    completed = max((group["end"] + 1 for group in groups if group["end"] < pending), default=0)
    starts = [{"offset": 0, "history_start": 0}] if completed else []
    starts.extend({"offset": group["start"], "history_start": group["history_start"]} for group in groups
                  if 0 < group["start"] < completed)
    result["_replay_complete"] = completed
    result["_replay_starts"] = starts
    result["_compact_rows"] = body
    result["_compact_policy"] = {
        "mode": "half_agent", "recipe": session.get("recipe"), "adapter": adapter_snapshot(settings),
        "policy": record.get("policy"), "lore_mode": session.get("lore_injection_mode"),
    }
    project_compact(result, snapshot.get(STATE, {}), settings, [], fixed)
    if history and any(m["role"] == "assistant" and m.get("parent")
                       and m["id"] not in result.get("_half_record_ids", ()) for m in history):
        result["diagnostics"].append({"reason": "旧历史按剧情及快照建立基线；未记录的历史临时提示无法还原。"})
    # Traces may contain cleaned narrative while native attachments include the
    # retrieval side channel. Shared projection deliberately uses portable text.
    result["compact_requires_rebuild"] = bool(result["compact_requires_rebuild"] and settings.compact_auto)
    return result


def install(snapshot, checkpoint, compiled, history):
    """Stage coverage and lore exits without touching the local/memory watermarks."""
    state = snapshot.setdefault(STATE, {})
    state["compaction"] = copy.deepcopy(checkpoint)
    if not any(item.get("type") == "compaction" for item in checkpoint.get("items", [])):
        return
    state["compaction"]["generation"] = uuid4().hex
    retired = {lore_key(row): row for row in state.get("retired_lore", [])}
    turn_number = sum(m["role"] == "user" for m in history) + 1
    covered = compiled["_compact_rows"][:checkpoint.get("compressed_count", len(checkpoint["covered"]))]
    for row in covered:
        anchor = row.get("anchor_id")
        if anchor:
            for eid in row.get("lore_ids", []):
                retired.setdefault((anchor, eid), {"anchor_id": anchor, "id": eid, "removed_at": turn_number})
    # Removed injections have no text row, but their presence reservation still
    # exits with the exchange. The removal tombstone remains in durable sources.
    covered_anchors = {row.get("message_id") for row in covered if row.get("message_id")}
    for row in compiled.get("next_lore_injections", []):
        if row["anchor_id"] in covered_anchors:
            retired.setdefault(lore_key(row), {"anchor_id": row["anchor_id"], "id": row["id"], "removed_at": turn_number})
    state["retired_lore"] = list(retired.values())
    originals = {tuple(key) for key in compiled.get("_source_lore_keys", ())}
    remaining = [row for row in compiled.get("next_lore_injections", snapshot.get("lore_injections", []))
                 if lore_key(row) in originals and lore_key(row) not in retired]
    snapshot["lore_injections"] = copy.deepcopy(remaining)
    active_ids = {row["id"] for row in remaining}
    exits = {}
    for row in retired.values():
        exits[row["id"]] = max(exits.get(row["id"], 0), row["removed_at"])
    for eid, removed_at in exits.items():
        if eid not in active_ids:
            old = snapshot.setdefault("lore", {}).setdefault(eid, {})
            old.update(present=False)
            old["removed_at"] = max(old.get("removed_at", 0), removed_at)


def completed_context(compiled):
    """Keep current dynamic frames out of a maintenance checkpoint as well."""
    count = compiled["_replay_complete"]
    body, header = compiled["_compact_body"], compiled["_compact_header"]
    covered = compiled["_compact_covered"]
    usable = compiled["compaction_opaque"] and covered <= count
    prefix = compiled["messages"][:len(header) + 1] if usable else header
    return {**compiled, "_compact_body": body[:count],
            "messages": [*prefix, *body[covered if usable else 0:count]],
            "compaction_opaque": usable, "_compact_covered": covered if usable else 0}


def valid_source_lore(document, records):
    allowed = {entry["id"] for entry in lore_entries(document, "writer") if entry.get("enabled", True)}
    return [row for row in records if row["id"] in allowed]


def remap_state(snapshot, ids):
    state = snapshot.get(STATE)
    if not state:
        return
    # A branch/import derives its own baseline. Opaque protocol IDs must never
    # be interpreted as local references by generic string replacement.
    state.pop("compaction", None)
    state["retired_lore"] = [{**row, "anchor_id": ids[row["anchor_id"]]}
                              for row in state.get("retired_lore", []) if row["anchor_id"] in ids]
