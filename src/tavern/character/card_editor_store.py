"""Card editing workspaces are independent of story sessions and turn snapshots."""

import json
import time

from tavern.character.editor_activity import PUBLIC_KINDS
from tavern.character.editor_storage import Conversations
from tavern.storage.store import dump


def initialize_editor_tables(store):
    with store.connection() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS card_edits(
                id TEXT PRIMARY KEY, card TEXT NOT NULL, source TEXT NOT NULL,
                base_revision INTEGER NOT NULL, base_document TEXT NOT NULL,
                document TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS card_edit_versions(
                id INTEGER PRIMARY KEY AUTOINCREMENT, workspace TEXT NOT NULL,
                document TEXT NOT NULL, reason TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS card_edit_runs(
                id TEXT PRIMARY KEY, workspace TEXT NOT NULL, operation TEXT NOT NULL,
                request TEXT NOT NULL, status TEXT NOT NULL, response TEXT NOT NULL DEFAULT '',
                error TEXT, created REAL NOT NULL, UNIQUE(workspace,operation));
            CREATE TABLE IF NOT EXISTS card_edit_events(
                seq INTEGER PRIMARY KEY AUTOINCREMENT, run TEXT NOT NULL,
                kind TEXT NOT NULL, payload TEXT NOT NULL, created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS card_edit_saves(
                workspace TEXT NOT NULL, operation TEXT NOT NULL, result TEXT NOT NULL,
                PRIMARY KEY(workspace,operation));
            CREATE TABLE IF NOT EXISTS card_edit_contexts(
                workspace TEXT PRIMARY KEY, payload TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS card_edit_events_run ON card_edit_events(run,seq);
            CREATE INDEX IF NOT EXISTS card_edit_versions_workspace ON card_edit_versions(workspace,id);
            CREATE INDEX IF NOT EXISTS card_edits_card ON card_edits(card);
        """)


    from tavern.character.editor_storage import initialize
    initialize(store)
    from tavern.character.editor_attachments import migrate_message_attachments
    migrate_message_attachments(store)


def recover_editor_runs(store):
    with store.connection() as db:
        db.execute("UPDATE card_edit_runs SET status='interrupted',error='服务重启，可继续本轮或丢弃进度' "
                   "WHERE status IN ('queued','running')")


class EditorStore(Conversations):
    def __init__(self, store):
        self.store = store


    def run(self, rid):
        with self.store.connection() as db:
            row = db.execute("SELECT * FROM card_edit_runs WHERE id=?", (rid,)).fetchone()
        if row is None:
            raise KeyError("编辑任务不存在")
        return {**dict(row), "request": json.loads(row["request"])}


    def context(self, wid):
        with self.store.connection() as db:
            row = db.execute("SELECT payload FROM card_edit_contexts WHERE workspace=?", (wid,)).fetchone()
        return json.loads(row[0]) if row else None


    def event(self, rid, kind, payload):
        with self.store.connection() as db:
            db.execute("INSERT INTO card_edit_events(run,kind,payload,created) VALUES (?,?,?,?)",
                       (rid, kind, dump(payload), time.time()))


    def events(self, rid, after=0):
        with self.store.connection() as db:
            return [{**dict(r), "payload": json.loads(r["payload"])} for r in db.execute(
                "SELECT * FROM card_edit_events WHERE run=? AND seq>? ORDER BY seq LIMIT 200", (rid, after))]


    def activity(self, rid, after=0):
        with self.store.connection() as db:
            placeholders = ",".join("?" for _ in PUBLIC_KINDS)
            rows = db.execute(f"""SELECT * FROM card_edit_events WHERE run=? AND seq>?
                AND kind IN ({placeholders}) ORDER BY seq LIMIT 201""", (rid, after, *PUBLIC_KINDS)).fetchall()
        events = [{**dict(row), "payload": json.loads(row["payload"])} for row in rows[:200]]
        return {"events": events, "has_more": len(rows) > 200,
                "next_cursor": events[-1]["seq"] if events else after}
