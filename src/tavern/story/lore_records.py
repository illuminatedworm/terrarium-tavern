"""Freeze automatic worldbook admissions and present them at their dialogue anchors."""

import json
import time

from tavern.character.cards import card_view, lore_entries, lore_title
from tavern.conversation.context import macros
from tavern.core.errors import Gone, NotFound
from tavern.core.model_view import ModelView
from tavern.storage.lore_admissions import save_admissions, turn_directory
from tavern.story.content import entry_rows
from tavern.story.lore import RUNTIME_LORE_SOURCE, injection_references, lore_match


def freeze_admissions(
    document, snapshot, persona, records, *, model_view=None, saved_references=None, send_titles=True
) -> list[dict]:
    if not records:
        return []
    selected = {r["id"] for r in records}
    if model_view is not None:
        rows = entry_rows(document)
        view, refs = injection_references(document, snapshot, model_view, rows=rows)
        titles = {e["id"]: lore_title(e) for _, _, e in rows if e["id"] in selected}
    else:
        # The original script may have added/reordered entries before the first model
        # request. Only a persisted handle map establishes an old admission's number.
        # A failed turn (including one with duplicate entry IDs) remains readable.
        story = snapshot.get("story", {}) if saved_references is None else saved_references
        refs = {eid: ref for ref, eid in story.get("lore_references", {}).items()}
        view = ModelView(
            {"document": document, "state": snapshot["state"]},
            references=dict(story.get("model_references", {})),
        )
        titles = {r["id"]: r["title"] for r in records}
        if "世界书" in titles.values():
            for e in lore_entries(document):
                if titles.get(e["id"]) == "世界书" and not lore_title(e):
                    titles[e["id"]] = ""
    card = card_view(document)
    frozen = []
    for r in records:
        eid = r["id"]
        title = macros(titles[eid], card, persona, snapshot["state"], snapshot.get("scopes"))
        frozen.append(
            {
                "id": eid,
                "anchor_id": r["anchor_id"],
                "ref": refs[eid] if model_view is not None else refs.get(eid),
                "text": view.text(r["content"]),
                "title": view.text(title).strip() if send_titles else title,
            }
        )
    if not send_titles:
        # A hidden title can expand into an opaque value absent from the document.
        # Allocate display-only handles after the visible bodies, outside the model's map.
        title_view = ModelView(
            {"document": document, "state": snapshot["state"]}, references=dict(view.reverse)
        )
        for record in frozen:
            record["title"] = title_view.text(record["title"]).strip()
    return frozen


def publish_input_ready(store, tid, message, active, admissions):
    # A failed first model connection may have no progress. Save the presentation
    # and its SSE directory in one transaction before an observer can see either.
    with store.connection() as db:
        save_admissions(db, admissions, turn=tid)
        event = {"message": message, "lore_injections": active, "lore_admissions": turn_directory(db, tid)}
        db.execute(
            "INSERT INTO events(turn,kind,payload,created) VALUES (?,'input_ready',?,?)",
            (tid, json.dumps(event, ensure_ascii=False), time.time()),
        )


def _present(store, records, payload):
    if not records:
        return []
    settings = store.settings_document().story
    rows = []
    for r in records:
        entry = lore_match(
            r["ref"],
            r["text"],
            settings.lore_preview_chars,
            title=r["title"] if settings.lore_injection_title else "",
        )
        if payload:
            rows.append({"id": r["id"], "payload": {"source": RUNTIME_LORE_SOURCE, **entry}})
        else:
            truncated = "preview" in entry
            rows.append(
                {
                    "id": r["id"],
                    "ref": entry["ref"],
                    "opening": entry["preview" if truncated else "text"],
                    "truncated": truncated,
                }
            )
    return rows


def message_lore_previews(store, sid: str, mid: str, anchor_id: str, *, payload: bool = False) -> list[dict]:
    with store.connection() as db:
        db.execute("BEGIN")
        member = db.execute(
            "WITH RECURSIVE branch(id) AS (SELECT head FROM sessions WHERE id=? UNION "
            "SELECT m.parent FROM messages m JOIN branch b ON m.id=b.id WHERE m.session=? AND m.parent IS NOT NULL) "
            "SELECT 1 FROM branch WHERE id=?",
            (sid, sid, mid),
        ).fetchone()
        if member is None:
            raise NotFound("注入记录不属于当前分支")
        records = db.execute(
            "SELECT id,ref,title,text FROM story_lore_admissions WHERE message_id=? AND anchor_id=? ORDER BY rowid",
            (mid, anchor_id),
        ).fetchall()
        if not records:
            # Imported stories and old messages whose input_ready expired only have the snapshot.
            row = db.execute("SELECT snapshot FROM messages WHERE id=?", (mid,)).fetchone()
            snapshot = json.loads(row[0])
            selected = [
                r
                for r in snapshot.get("lore_injections", ())
                if r["anchor_id"] == anchor_id and not r.get("manually_removed")
            ]
            if selected:
                document = (snapshot.get("content") or {}).get("document")
                session = db.execute("SELECT persona,card_data FROM sessions WHERE id=?", (sid,)).fetchone()
                if document is None:
                    document = json.loads(session["card_data"])["document"]
                records = freeze_admissions(document, snapshot, session["persona"], selected)
    return _present(store, records, payload)


def turn_lore_previews(store, sid: str, tid: str, anchor_id: str, *, payload: bool = False) -> list[dict]:
    with store.connection() as db:
        db.execute("BEGIN")
        records = db.execute(
            "SELECT a.id,a.ref,a.title,a.text FROM story_lore_admissions a JOIN turns t ON t.id=a.turn "
            "WHERE t.session=? AND a.turn=? AND a.anchor_id=? ORDER BY a.rowid",
            (sid, tid, anchor_id),
        ).fetchall()
        if not records:
            source = db.execute(
                "SELECT json_extract(e.payload,'$.detail_expired') FROM events e JOIN turns t ON t.id=e.turn "
                "WHERE t.session=? AND t.id=? AND t.status!='committed' AND e.kind='input_ready' "
                "ORDER BY e.seq DESC LIMIT 1",
                (sid, tid),
            ).fetchone()
            if source is None:
                raise NotFound("本回合没有世界书注入记录")
            if source[0]:
                raise Gone("本回合的世界书注入明细已过期")
    return _present(store, records, payload)


def saved_input_ready(store, tid: str) -> dict | None:
    """Restore just the pending input and its directory without replaying a finished stream."""
    with store.connection() as db:
        db.execute("BEGIN")
        row = db.execute(
            "SELECT json_extract(payload,'$.message.id') AS id, json_extract(payload,'$.message.raw') AS raw,"
            "json_extract(payload,'$.message.display') AS display, json_extract(payload,'$.message.ooc') AS ooc "
            "FROM events WHERE turn=? AND kind='input_ready' ORDER BY seq DESC LIMIT 1",
            (tid,),
        ).fetchone()
        if row is None:
            # A resumed attempt owns its copied progress before the worker starts its
            # event stream. A crash there must not orphan its input or admission directory.
            row = db.execute(
                "SELECT json_extract(payload,'$.user.id') AS id, json_extract(payload,'$.user.raw') AS raw,"
                "json_extract(payload,'$.user.display') AS display, json_extract(payload,'$.user.ooc') AS ooc "
                "FROM story_progress WHERE turn=?",
                (tid,),
            ).fetchone()
        if row is not None and row["id"] is not None:
            return {"message": dict(row), "lore_admissions": turn_directory(db, tid)}
    return None
