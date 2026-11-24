"""Dialogue records belong to a pending attempt, then to its committed reply version."""

import json
import sqlite3


def save_admissions(db, records, *, turn=None, message=None):
    db.executemany(
        "INSERT INTO story_lore_admissions(turn,message_id,anchor_id,id,ref,title,text) VALUES (?,?,?,?,?,?,?)",
        ((turn, message, r["anchor_id"], r["id"], r["ref"], r["title"], r["text"]) for r in records),
    )


def turn_directory(db: sqlite3.Connection, tid: str) -> list[dict]:
    return [
        dict(row)
        for row in db.execute(
            "SELECT anchor_id,id,title FROM story_lore_admissions WHERE turn=? ORDER BY rowid", (tid,)
        )
    ]


def copy_reply_admissions(db, original, copied, ids):
    records = db.execute(
        "SELECT anchor_id,id,ref,title,text FROM story_lore_admissions WHERE message_id=? ORDER BY rowid",
        (original,),
    )
    save_admissions(
        db,
        ({**row, "anchor_id": ids.get(row["anchor_id"], row["anchor_id"])} for row in records),
        message=copied,
    )


def upgrade_admissions(db: sqlite3.Connection) -> None:
    """Recover each original admission once, including continuations of an edited draft."""
    from tavern.story.lore_records import freeze_admissions

    events = db.execute(
        "SELECT t.id,t.status,t.session,e.payload,c.message,json_extract(t.request,'$.resume_turn_id') AS resumed "
        "FROM events e JOIN turns t ON t.id=e.turn LEFT JOIN changes c ON c.turn=t.id WHERE e.kind='input_ready' "
        "AND json_type(e.payload,'$.lore_admissions') IS NULL AND json_type(e.payload,'$.message')='object' "
        "AND (json_array_length(e.payload,'$.lore_injections')>0 OR json_extract(t.request,'$.resume_turn_id') IS NOT NULL) "
        "ORDER BY e.seq"
    )
    for row in events:
        owner = row["message"] if row["status"] == "committed" else None
        if not owner and row["status"] == "committed":
            continue
        frozen = []
        if row["resumed"]:
            frozen = db.execute(
                "SELECT anchor_id,id,ref,title,text FROM story_lore_admissions WHERE turn=? ORDER BY rowid",
                (row["resumed"],),
            ).fetchall()
        if not frozen:
            event = json.loads(row["payload"])
            message = event["message"]
            records = [
                r
                for r in event.get("lore_injections", ())
                if not r.get("manually_removed") and r.get("source_ids", [])[-1:] == [message["id"]]
            ]
            if not records:
                continue
            snapshot = message["snapshot"]
            if owner:
                maps = db.execute(
                    "SELECT json_extract(snapshot,'$.story.lore_references'),json_extract(snapshot,'$.story.model_references') "
                    "FROM messages WHERE id=?",
                    (owner,),
                ).fetchone()
            else:
                maps = db.execute(
                    "SELECT json_extract(payload,'$.snapshot.story.lore_references'),"
                    "json_extract(payload,'$.snapshot.story.model_references') FROM story_progress WHERE turn=?",
                    (row["id"],),
                ).fetchone()
            story = dict(snapshot.get("story", {}))
            if maps:
                for name, value in zip(("lore_references", "model_references"), maps, strict=True):
                    if value is not None:
                        story[name] = json.loads(value)
            document = (snapshot.get("content") or {}).get("document")
            session = db.execute(
                "SELECT persona,CASE WHEN ? THEN json_extract(card_data,'$.document') END AS document "
                "FROM sessions WHERE id=?",
                (document is None, row["session"]),
            ).fetchone()
            frozen = freeze_admissions(
                document if document is not None else json.loads(session["document"]),
                snapshot,
                session["persona"],
                records,
                saved_references=story,
            )
        if owner:
            # Use this reply's tombstones. A shared user/first-message anchor can
            # also be marked by removal on a different regeneration of the reply.
            removed = {
                tuple(key)
                for key in db.execute(
                    "SELECT json_extract(r.value,'$.anchor_id'), json_extract(r.value,'$.id') "
                    "FROM messages m, json_each(m.snapshot,'$.lore_injections') r "
                    "WHERE m.id=? AND json_extract(r.value,'$.manually_removed')=1",
                    (owner,),
                )
            }
            if removed:
                frozen = [r for r in frozen if (r["anchor_id"], r["id"]) not in removed]
        save_admissions(db, frozen, message=owner, turn=None if owner else row["id"])
