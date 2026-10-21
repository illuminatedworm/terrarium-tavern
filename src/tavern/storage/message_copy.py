"""Message identity rewrites shared by story forks and latest-reply versions.

A fork copies a committed prefix into a new story. A content-only version (a
conversation card edit or card interaction) copies the latest reply beside the
original. Either way the copy owns everything that referred to its original:
memory coverage, citations and the request traces later turns replay.
"""

import json

from tavern.conversation.half_compaction import remap_state
from tavern.conversation.half_context import remap_record
from tavern.full_agent.state import remap_sources
from tavern.memories.memory import fingerprint, window_valid
from tavern.memories.memory_archive import remap_archive


def remap_snapshot(snapshot, ids, original, copied, *, new_story):
    """Point one snapshot at copied messages.

    ``original`` and ``copied`` are the owner's branch before and after copying,
    ending at the owner. Coverage valid on the original stays valid on the copy.
    """
    remap_sources(snapshot, ids)
    if new_story:
        # A new story derives its own Writer checkpoint. A sibling version keeps
        # it: checkpoints are validated by content hashes and anchor on inputs.
        remap_state(snapshot, ids)
    for memory in snapshot.get("memory_document", {}).get("records", []):
        for source in memory.get("sources", []) + memory.get("deleted_sources", []):
            source["message_id"] = ids.get(source["message_id"], source["message_id"])
        for setting in memory.get("setting_sources", []):
            setting["anchor"] = ids.get(setting.get("anchor"), setting.get("anchor"))
    remap_archive(snapshot, ids)
    for record in snapshot.get("lore_injections", []):
        record["anchor_id"] = ids.get(record["anchor_id"], record["anchor_id"])
        record["source_ids"] = [ids.get(mid, mid) for mid in record.get("source_ids", [])]
    for key in ("context_window", "memory_archive"):
        window = snapshot.get(key)
        if not window:
            continue
        if window_valid(window, original):
            window["fingerprint"] = fingerprint(copied[: window["covered_count"]])
        else:
            snapshot.pop(key)


def copy_traces(db, session, ids, *, new_story):
    """Give each copied message the request trace recorded for its original."""
    for original, copied in ids.items():
        for row in db.execute(
            "SELECT seq,window_id,payload FROM full_agent_trace WHERE message_id=?", (original,)
        ).fetchall():
            payload = json.loads(row["payload"])
            value = payload.get("_runtime_context", {}).get("value")
            if isinstance(value, dict) and "input_id" in value:
                value["input_id"] = ids.get(value["input_id"], value["input_id"])
            for owner, key in (
                (payload, "_history_observations"),
                (payload.get("_context_baseline", {}), "history"),
            ):
                if key in owner:
                    owner[key] = {ids.get(mid, mid): observed for mid, observed in owner[key].items()}
            db.execute(
                "INSERT INTO full_agent_trace VALUES (?,?,?,?,?)",
                (session, copied, row["seq"], row["window_id"], json.dumps(payload, ensure_ascii=False)),
            )
        row = db.execute(
            "SELECT seq,payload FROM half_agent_trace WHERE message_id=?", (original,)
        ).fetchone()
        if row:
            payload = remap_record(json.loads(row["payload"]), ids, new_story=new_story)
            db.execute(
                "INSERT INTO half_agent_trace VALUES (?,?,?,?,?)",
                (session, copied, row["seq"], payload["window"], json.dumps(payload, ensure_ascii=False)),
            )


def latest_version(session, message):
    """Rewrite ``message``'s snapshot as a sibling version of the latest reply.

    Returns the pending field that makes the commit copy the reply's trace.
    """
    history = session["messages"]
    remap_snapshot(
        message["snapshot"],
        {session["head"]: message["id"]},
        history,
        [*history[:-1], message],
        new_story=False,
    )
    return {"version_of": session["head"]}
