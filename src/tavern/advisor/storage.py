"""One disposable consultation per story, separate from story messages and snapshots."""

import json
import time

from tavern.core.errors import NotFound
from tavern.storage.store import Conflict, dump


def initialize(store):
    with store.connection() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS advisor_conversations(
                session TEXT PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,
                revision INTEGER NOT NULL DEFAULT 0, options TEXT NOT NULL DEFAULT '{}',
                context TEXT NOT NULL DEFAULT '{}');
            CREATE TABLE IF NOT EXISTS advisor_runs(
                id TEXT PRIMARY KEY, session TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                operation TEXT NOT NULL, request TEXT NOT NULL, status TEXT NOT NULL,
                response TEXT NOT NULL DEFAULT '', error TEXT, created REAL NOT NULL,
                progress TEXT, UNIQUE(session,operation));
            CREATE TABLE IF NOT EXISTS advisor_exchanges(
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                session TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                run TEXT NOT NULL UNIQUE, user_text TEXT NOT NULL, response TEXT NOT NULL,
                trace TEXT NOT NULL, story_head TEXT, story_sources TEXT NOT NULL, created REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS advisor_exchanges_session ON advisor_exchanges(session,seq);
            CREATE TABLE IF NOT EXISTS advisor_events(
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                run TEXT NOT NULL REFERENCES advisor_runs(id) ON DELETE CASCADE,
                kind TEXT NOT NULL, payload TEXT NOT NULL, created REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS advisor_events_run ON advisor_events(run,seq);
        """)


def fork(db, source, target, message_ids):
    row = db.execute("SELECT options FROM advisor_conversations WHERE session=?", (source,)).fetchone()
    if not row:
        return
    db.execute("INSERT INTO advisor_conversations(session,options) VALUES (?,?)", (target, row[0]))


def cleanup_plan(db, settings, now):
    rows = [
        dict(r)
        for r in db.execute("""SELECT r.id,r.created,r.status,r.progress IS NOT NULL AS pending,
        length(CAST(r.request AS BLOB))+length(CAST(r.response AS BLOB))+
        coalesce((SELECT sum(length(CAST(e.payload AS BLOB))) FROM advisor_events e WHERE e.run=r.id),0) AS bytes
        FROM advisor_runs r ORDER BY r.created""")
    ]
    total = sum(r["bytes"] for r in rows)
    remaining, selected = total, []
    for row in rows:
        if row["pending"] or row["status"] in ("queued", "running"):
            continue
        if (
            row["created"] < now - settings.log_retention_days * 86400
            or remaining > settings.log_max_mib * 1024**2
        ):
            selected.append(row["id"])
            remaining -= row["bytes"]
    return selected, {
        "advisor_detail_bytes": total,
        "advisor_reclaimable_bytes": total - remaining,
        "advisor_protected_bytes": sum(
            r["bytes"] for r in rows if r["pending"] or r["status"] in ("queued", "running")
        ),
    }


class AdvisorStore:
    def __init__(self, store):
        self.store = store

    def get(self, sid):
        with self.store.connection() as db:
            if not db.execute("SELECT 1 FROM sessions WHERE id=?", (sid,)).fetchone():
                raise NotFound("故事不存在")
            row = db.execute("SELECT * FROM advisor_conversations WHERE session=?", (sid,)).fetchone()
            result = dict(row) if row else {"session": sid, "revision": 0, "options": "{}", "context": "{}"}
            result["options"] = json.loads(result["options"])
            result["context"] = json.loads(result["context"])
            rows = db.execute(
                "SELECT * FROM advisor_exchanges WHERE session=? ORDER BY seq", (sid,)
            ).fetchall()
            result["exchanges"] = [
                {**dict(r), "trace": json.loads(r["trace"]), "story_sources": json.loads(r["story_sources"])}
                for r in rows
            ]
            run = db.execute(
                "SELECT id FROM advisor_runs WHERE session=? ORDER BY created DESC,rowid DESC LIMIT 1", (sid,)
            ).fetchone()
        result["run"] = self.run(run[0]) if run else None
        return result

    @staticmethod
    def ensure(db, sid, expected=None):
        if not db.execute("SELECT 1 FROM sessions WHERE id=?", (sid,)).fetchone():
            raise NotFound("故事不存在")
        db.execute("INSERT OR IGNORE INTO advisor_conversations(session) VALUES (?)", (sid,))
        row = db.execute("SELECT * FROM advisor_conversations WHERE session=?", (sid,)).fetchone()
        if expected is not None and row["revision"] != expected:
            raise Conflict("参谋 agent 对话已改变，请刷新后重试")
        return row

    def update_options(self, sid, expected, options):
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self.ensure(db, sid, expected)
            if db.execute(
                "SELECT 1 FROM advisor_runs WHERE session=? AND (status IN ('queued','running') OR progress IS NOT NULL)",
                (sid,),
            ).fetchone():
                raise Conflict("请先完成或丢弃参谋 agent 未完成的咨询")
            merged = {**json.loads(row["options"]), **options}
            db.execute(
                "UPDATE advisor_conversations SET options=?,context='{}',revision=revision+1 WHERE session=?",
                (dump(merged), sid),
            )
        return self.get(sid)

    def run(self, rid, *, private=False):
        with self.store.connection() as db:
            row = db.execute("SELECT * FROM advisor_runs WHERE id=?", (rid,)).fetchone()
        if not row:
            raise NotFound("参谋 agent 任务不存在")
        result = dict(row)
        result["request"] = json.loads(result["request"])
        result["resumable"] = bool(result["progress"] and result["status"] in ("error", "cancelled"))
        if private:
            result["progress"] = json.loads(result["progress"]) if result["progress"] else None
        else:
            result.pop("progress")
        return result

    def event(self, rid, kind, payload):
        with self.store.connection() as db:
            db.execute(
                "INSERT INTO advisor_events(run,kind,payload,created) VALUES (?,?,?,?)",
                (rid, kind, dump(payload), time.time()),
            )

    def events(self, rid, after=0):
        with self.store.connection() as db:
            return [
                {**dict(r), "payload": json.loads(r["payload"])}
                for r in db.execute(
                    "SELECT * FROM advisor_events WHERE run=? AND seq>? ORDER BY seq LIMIT 200", (rid, after)
                )
            ]

    def discard(self, sid, expected):
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self.ensure(db, sid, expected)
            if db.execute(
                "SELECT 1 FROM advisor_runs WHERE session=? AND status IN ('queued','running')", (sid,)
            ).fetchone():
                raise Conflict("请先停止参谋 agent")
            db.execute(
                "UPDATE advisor_runs SET progress=NULL,status='discarded',error=NULL WHERE session=? AND progress IS NOT NULL",
                (sid,),
            )
            db.execute("UPDATE advisor_conversations SET revision=revision+1 WHERE session=?", (sid,))
        return self.get(sid)

    def clear(self, sid, expected):
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self.ensure(db, sid, expected)
            # Stop first so no provider task can recreate progress or commit a preference later.
            if db.execute(
                "SELECT 1 FROM advisor_runs WHERE session=? AND status IN ('queued','running')", (sid,)
            ).fetchone():
                raise Conflict("请先停止参谋 agent，再清空对话")
            db.execute("DELETE FROM advisor_exchanges WHERE session=?", (sid,))
            db.execute("DELETE FROM advisor_runs WHERE session=?", (sid,))
            db.execute("DELETE FROM context_token_samples WHERE owner=?", ("advisor:" + sid,))
            # Keep the row and advance its revision to reject stale tabs and requests.
            db.execute(
                "UPDATE advisor_conversations SET context='{}',revision=revision+1 WHERE session=?", (sid,)
            )
        return self.get(sid)
