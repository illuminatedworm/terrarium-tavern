"""One disposable consultation per story, separate from story messages and snapshots."""

import json
import time

from tavern.core.errors import NotFound
from tavern.storage.branch_history import BRANCH_MESSAGES, branch_history
from tavern.storage.store import Conflict, dump, new_snapshot
from tavern.story.state import read_story_state


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


def fork(db, source, target):
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

    @staticmethod
    def state(db, sid, *, public=False):
        context = "context"
        if public:
            context = """json_object('summary',json_extract(context,'$.summary'),'through',json_extract(context,'$.through'),
                'created',json_extract(context,'$.created'),'strategy',json_extract(context,'$.strategy'))"""
        row = db.execute(
            f"SELECT revision,options,{context} AS context FROM advisor_conversations WHERE session=?", (sid,)
        ).fetchone()
        return {
            "session": sid,
            "revision": row["revision"] if row else 0,
            "options": json.loads(row["options"]) if row else {},
            "context": json.loads(row["context"]) if row else {},
        }

    @staticmethod
    def exchanges(db, sid, after=0):
        return [
            {**dict(r), "trace": json.loads(r["trace"]), "story_sources": json.loads(r["story_sources"])}
            for r in db.execute(
                "SELECT * FROM advisor_exchanges WHERE session=? AND seq>? ORDER BY seq", (sid, after)
            )
        ]

    def latest_run(self, db, sid, *, reader=False):
        row = db.execute(
            "SELECT id FROM advisor_runs WHERE session=? ORDER BY created DESC,rowid DESC LIMIT 1", (sid,)
        ).fetchone()
        return self.run(row[0], db=db, reader=reader) if row else None

    def get(self, sid):
        with self.store.connection() as db:
            db.execute("BEGIN")
            if not db.execute("SELECT 1 FROM sessions WHERE id=?", (sid,)).fetchone():
                raise NotFound("故事不存在")
            return {
                **self.state(db, sid),
                "exchanges": self.exchanges(db, sid),
                "run": self.latest_run(db, sid),
            }

    @staticmethod
    def story_head(db, sid):
        row = db.execute(
            "SELECT id,head,channel_origin,revision,opening_locked FROM sessions WHERE id=?", (sid,)
        ).fetchone()
        if row is None:
            raise NotFound("故事不存在")
        return dict(row)

    @staticmethod
    def frozen_story(db, sid):
        row = db.execute(
            """SELECT id,revision,head,channel_origin,persona,persona_data,turn_preference,plot_enabled,
                context_strategy,title,empty_window_id FROM sessions WHERE id=?""",
            (sid,),
        ).fetchone()
        if row is None:
            raise NotFound("故事不存在")
        story = dict(row)
        empty_window = story.pop("empty_window_id")
        story["persona_data"] = json.loads(story["persona_data"])
        story["plot_enabled"] = bool(story["plot_enabled"])
        story["messages"] = branch_history(db, sid, story["head"])
        if story["messages"]:
            snapshot = story["messages"][-1]["snapshot"]
        else:
            snapshot = new_snapshot()
            snapshot["story"]["window"]["id"] = empty_window or sid
        story["snapshot"] = snapshot
        read_story_state(snapshot)
        # This transaction owns the read-only snapshot, including every past
        # message's macro inputs. The library card may have changed meanwhile.
        story["document"] = (snapshot.get("content") or {}).get("document") or json.loads(
            db.execute(
                "SELECT json_extract(card_data,'$.document') FROM sessions WHERE id=?", (sid,)
            ).fetchone()[0]
        )
        return story

    @staticmethod
    def branch_sources(db, sid, head, ids):
        from tavern.advisor.context import source_hash

        if not ids:
            return {}
        # The hash uses only model text, state and content, and only sources
        # still on this branch can validate a persisted consultation.
        rows = db.execute(
            BRANCH_MESSAGES
            + """SELECT m.id,m.model,
                (SELECT json_group_object(key,value) FROM json_each(m.snapshot) WHERE key IN ('state','content')) AS snapshot
                FROM branch b JOIN messages m ON m.id=b.id WHERE b.id IN (SELECT value FROM json_each(?))""",
            (sid, head, sid, dump(list(ids))),
        )
        return {r["id"]: source_hash(dict(r)) for r in rows}

    @staticmethod
    def ensure(db, sid, expected=None):
        if not db.execute("SELECT 1 FROM sessions WHERE id=?", (sid,)).fetchone():
            raise NotFound("故事不存在")
        db.execute("INSERT OR IGNORE INTO advisor_conversations(session) VALUES (?)", (sid,))
        row = db.execute(
            "SELECT revision,options FROM advisor_conversations WHERE session=?", (sid,)
        ).fetchone()
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

    def run_status(self, rid):
        with self.store.connection() as db:
            row = db.execute("SELECT status FROM advisor_runs WHERE id=?", (rid,)).fetchone()
        if row is None:
            raise NotFound("参谋 agent 任务不存在")
        return row[0]

    def run(self, rid, *, private=False, db=None, reader=False):
        if db is None:
            with self.store.connection() as connection:
                return self.run(rid, private=private, db=connection, reader=reader)
        if private:
            columns = "*"
        elif reader:
            columns = """id,status,error,progress IS NOT NULL AS resumable,json_extract(progress,'$.story.revision') AS story_revision,
                CASE WHEN status IN ('queued','running') OR progress IS NOT NULL THEN request ELSE '{}' END AS request"""
        else:
            columns = (
                "id,session,operation,request,status,response,error,created,progress IS NOT NULL AS resumable"
            )
        row = db.execute(f"SELECT {columns} FROM advisor_runs WHERE id=?", (rid,)).fetchone()
        if not row:
            raise NotFound("参谋 agent 任务不存在")
        result = dict(row)
        result["request"] = json.loads(result["request"])
        result["resumable"] = bool(
            (result["progress"] if private else result["resumable"])
            and result["status"] in ("error", "cancelled")
        )
        if private:
            result["progress"] = json.loads(result["progress"]) if result["progress"] else None
        return result

    def event(self, rid, kind, payload):
        with self.store.connection() as db:
            db.execute(
                "INSERT INTO advisor_events(run,kind,payload,created) VALUES (?,?,?,?)",
                (rid, kind, dump(payload), time.time()),
            )

    def events(self, rid, after=0, *, reader=False):
        columns = "*"
        if reader:
            # A live tool badge only uses the error status. The full result is
            # already durable in the run and available in the completed trace.
            columns = """seq,run,kind,created,CASE kind
                WHEN 'tool_finished' THEN json_object('tool',json_extract(payload,'$.tool'),
                    'call_id',json_extract(payload,'$.call_id'),'result',json_object('error',
                    json(CASE WHEN coalesce(json_extract(payload,'$.result.error'),'') NOT IN ('',0) THEN 'true' ELSE 'false' END)))
                WHEN 'completed' THEN json_object('preference_updated',json(CASE WHEN json_extract(payload,'$.preference_updated') THEN 'true' ELSE 'false' END))
                WHEN 'compacted' THEN '{}'
                ELSE payload END AS payload"""
        with self.store.connection() as db:
            batch = [
                {**dict(r), "payload": json.loads(r["payload"])}
                for r in db.execute(
                    f"SELECT {columns} FROM advisor_events WHERE run=? AND seq>? ORDER BY seq LIMIT 200",
                    (rid, after),
                )
            ]
            if reader:
                for event in batch:
                    if event["kind"] == "completed" and event["payload"].get("preference_updated"):
                        row = db.execute(
                            """SELECT s.id,s.revision,s.turn_preference FROM sessions s
                            JOIN advisor_runs r ON r.session=s.id WHERE r.id=?""",
                            (rid,),
                        ).fetchone()
                        if row:
                            event["payload"]["story_update"] = dict(row)
            return batch

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
