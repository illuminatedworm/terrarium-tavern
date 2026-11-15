"""Message identity rewrites shared by story forks and latest-reply versions.

A fork copies a committed prefix into a new story. A content-only version (a
conversation card edit or card interaction) copies the latest reply beside the
original. Citations and replayed request traces follow the new message IDs.
"""

import json

from tavern.story.state import remap_sources


def remap_snapshot(snapshot, ids):
    remap_sources(snapshot, ids)
    for record in snapshot.get("lore_injections", []):
        record["anchor_id"] = ids.get(record["anchor_id"], record["anchor_id"])
        record["source_ids"] = [ids.get(mid, mid) for mid in record.get("source_ids", [])]


def copy_traces(db, session, ids, *, new_story):
    """Give each copied message the request trace recorded for its original."""
    for original, copied in ids.items():
        for row in db.execute(
            "SELECT seq,window_id,payload FROM story_trace WHERE message_id=?", (original,)
        ).fetchall():
            payload = json.loads(row["payload"])
            if new_story:
                payload.pop(
                    "_novel_mail", None
                )  # A delivered report copies; its source mailbox receipt does not.
            for key in ("_story_received", "_roleplay_received"):
                receipt = payload.get(key)
                if receipt:
                    receipt["input_id"] = ids.get(receipt["input_id"], receipt["input_id"])
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
                "INSERT INTO story_trace VALUES (?,?,?,?,?)",
                (session, copied, row["seq"], row["window_id"], json.dumps(payload, ensure_ascii=False)),
            )


def latest_version(session, message):
    """Rewrite ``message``'s snapshot as a sibling version of the latest reply.

    Returns the pending field that makes the commit copy the reply's trace.
    """
    remap_snapshot(message["snapshot"], {session["head"]: message["id"]})
    return {"version_of": session["head"]}
