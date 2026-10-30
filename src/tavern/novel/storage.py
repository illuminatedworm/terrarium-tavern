"""Durable research tasks and mailboxes, independent of story transactions."""

import json
import time

from tavern.core.errors import NotFound
from tavern.storage.store import Conflict, dump, uid

MAX_LIVING = 3  # Investigators not yet destroyed, per branch.
MAX_STARTS = 5  # Starts per story turn, including destroyed and regenerated attempts.


def initialize(store):
    with store.connection() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS novel_sources(
                id TEXT PRIMARY KEY, name TEXT NOT NULL, encoding TEXT NOT NULL,
                bytes INTEGER NOT NULL, characters INTEGER NOT NULL, created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS novel_bindings(
                session TEXT PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,
                source TEXT REFERENCES novel_sources(id), enabled INTEGER NOT NULL DEFAULT 0,
                revision INTEGER NOT NULL DEFAULT 0, next_ref INTEGER NOT NULL DEFAULT 1);
            CREATE TABLE IF NOT EXISTS novel_source_history(
                session TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                source TEXT NOT NULL REFERENCES novel_sources(id), PRIMARY KEY(session,source));
            CREATE TABLE IF NOT EXISTS novel_jobs(
                id TEXT PRIMARY KEY, session TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                ref INTEGER NOT NULL, source TEXT NOT NULL REFERENCES novel_sources(id),
                operation TEXT NOT NULL, round_position INTEGER NOT NULL, origin_turn TEXT NOT NULL,
                task TEXT NOT NULL, background TEXT NOT NULL, status TEXT NOT NULL,
                config TEXT NOT NULL, progress TEXT NOT NULL DEFAULT '{}',
                requests INTEGER NOT NULL DEFAULT 0, elapsed REAL NOT NULL DEFAULT 0,
                usage TEXT NOT NULL DEFAULT '{}', error TEXT, created REAL NOT NULL,
                UNIQUE(session,ref), UNIQUE(session,operation));
            CREATE TABLE IF NOT EXISTS novel_trace(
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                job TEXT NOT NULL REFERENCES novel_jobs(id) ON DELETE CASCADE,
                message TEXT NOT NULL, created REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS novel_trace_job ON novel_trace(job,seq);
            CREATE TABLE IF NOT EXISTS novel_mail(
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                job TEXT NOT NULL REFERENCES novel_jobs(id) ON DELETE CASCADE,
                sender TEXT NOT NULL, kind TEXT NOT NULL, content TEXT NOT NULL,
                operation TEXT NOT NULL, delivered TEXT, revoked INTEGER NOT NULL DEFAULT 0,
                created REAL NOT NULL, UNIQUE(job,sender,operation));
            CREATE INDEX IF NOT EXISTS novel_mail_job ON novel_mail(job,seq);
            CREATE TABLE IF NOT EXISTS novel_events(
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                session TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                job TEXT, kind TEXT NOT NULL, created REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS novel_jobs_session ON novel_jobs(session,ref);
            CREATE INDEX IF NOT EXISTS novel_jobs_living ON novel_jobs(session,ref) WHERE status!='destroyed';
            CREATE INDEX IF NOT EXISTS novel_jobs_round ON novel_jobs(session,round_position);
            CREATE INDEX IF NOT EXISTS novel_mail_pending ON novel_mail(sender,job,seq) WHERE delivered IS NULL AND revoked=0;
            CREATE INDEX IF NOT EXISTS novel_events_session ON novel_events(session,seq);
        """)


def event(db, sid, kind, job=None):
    db.execute(
        "INSERT INTO novel_events(session,job,kind,created) VALUES (?,?,?,?)", (sid, job, kind, time.time())
    )


def invalidate(db, sid, reason):
    """Rewriting story history revokes every surviving task, including late replies."""
    changed = db.execute(
        "UPDATE novel_jobs SET status='destroyed',error=? WHERE session=? AND status!='destroyed'",
        (reason, sid),
    )
    if not changed.rowcount:
        return
    db.execute(
        "UPDATE novel_mail SET revoked=1 WHERE delivered IS NULL AND job IN "
        "(SELECT id FROM novel_jobs WHERE session=?)",
        (sid,),
    )
    event(db, sid, "invalidated")


def fork(db, source, target):
    db.execute(
        "INSERT INTO novel_bindings SELECT ?,source,enabled,0,next_ref FROM novel_bindings WHERE session=?",
        (target, source),
    )
    db.execute(
        "INSERT INTO novel_source_history SELECT ?,source FROM novel_source_history WHERE session=?",
        (target, source),
    )


def cleanup_plan(db, settings, now):
    rows = [
        dict(row)
        for row in db.execute(
            "SELECT j.id,j.status,j.created,coalesce(sum(length(CAST(t.message AS BLOB))),0) AS bytes "
            "FROM novel_jobs j LEFT JOIN novel_trace t ON t.job=j.id GROUP BY j.id ORDER BY j.created"
        )
    ]
    total = sum(row["bytes"] for row in rows)
    remaining, selected = total, []
    for row in rows:
        if row["status"] != "destroyed":
            continue
        if (
            row["created"] < now - settings.log_retention_days * 86400
            or remaining > settings.log_max_mib * 1024**2
        ):
            selected.append(row["id"])
            remaining -= row["bytes"]
    return selected, {
        "novel_detail_bytes": total,
        "novel_reclaimable_bytes": total - remaining,
        "novel_protected_bytes": sum(r["bytes"] for r in rows if r["status"] != "destroyed"),
    }


class NovelStore:
    def __init__(self, store):
        self.store = store

    def source(self, source_id):
        with self.store.connection() as db:
            row = db.execute("SELECT * FROM novel_sources WHERE id=?", (source_id,)).fetchone()
        if row is None:
            raise NotFound("原著参考文件不存在")
        return dict(row)

    def binding(self, sid):
        with self.store.connection() as db:
            row = db.execute("SELECT * FROM novel_bindings WHERE session=?", (sid,)).fetchone()
        return (
            dict(row) if row else {"session": sid, "source": None, "enabled": 0, "revision": 0, "next_ref": 1}
        )

    @staticmethod
    def editable(db, sid, expected):
        session = db.execute("SELECT agent_mode FROM sessions WHERE id=?", (sid,)).fetchone()
        if session is None:
            raise NotFound("对话不存在")
        if session[0] != "full_agent":
            raise ValueError("原著调查仅限 Full Agent 对话")
        db.execute("INSERT OR IGNORE INTO novel_bindings(session) VALUES (?)", (sid,))
        row = db.execute("SELECT * FROM novel_bindings WHERE session=?", (sid,)).fetchone()
        if row["revision"] != expected:
            raise Conflict("原著设置已改变，请刷新后重试")
        if db.execute(
            "SELECT 1 FROM novel_jobs WHERE session=? AND status!='destroyed' LIMIT 1", (sid,)
        ).fetchone():
            raise Conflict("存在尚未销毁的调查者；请先销毁，再替换、解绑或停用原著")
        if db.execute(
            "SELECT 1 FROM turns WHERE session=? AND status IN ('queued','running','awaiting_review')", (sid,)
        ).fetchone():
            raise Conflict("请先完成或停止主对话当前回合，再修改原著授权")
        return row

    def bind(self, sid, expected, source_id, metadata, *, prepared=None):
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self.editable(db, sid, expected)
            if prepared is not None:
                from tavern.novel.source import directory

                destination = directory(self.store.root, source_id)
                destination.parent.mkdir(parents=True, exist_ok=True)
                prepared.rename(destination)
            db.execute(
                "INSERT INTO novel_sources VALUES (?,?,?,?,?,?)",
                (
                    source_id,
                    metadata["name"],
                    metadata["encoding"],
                    metadata["bytes"],
                    metadata["characters"],
                    time.time(),
                ),
            )
            db.execute("INSERT INTO novel_source_history VALUES (?,?)", (sid, source_id))
            db.execute(
                "UPDATE novel_bindings SET source=?,enabled=0,revision=revision+1 WHERE session=?",
                (source_id, sid),
            )
            event(db, sid, "source_imported")

    def configure(self, sid, expected, *, enabled=None, unbind=False):
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self.editable(db, sid, expected)
            if enabled and not row["source"]:
                raise ValueError("请先导入原著文件")
            db.execute(
                "UPDATE novel_bindings SET source=?,enabled=?,revision=revision+1 WHERE session=?",
                (None if unbind else row["source"], 0 if unbind else int(enabled), sid),
            )
            event(db, sid, "authorization")

    def jobs(self, sid, *, living_only=False, before=None):
        fields = (
            "SELECT id,ref,source,task,background,status,config,requests,elapsed,usage,error,created,round_position,"
            "(SELECT count(*) FROM novel_mail WHERE job=novel_jobs.id AND sender='worker' AND delivered IS NULL AND revoked=0) AS pending_messages,"
            "(SELECT count(*) FROM novel_mail WHERE job=novel_jobs.id AND sender='worker' AND delivered IS NOT NULL) AS delivered_messages "
            "FROM novel_jobs WHERE session=? "
        )
        if living_only:
            query, params = fields + "AND status!='destroyed' ORDER BY ref DESC", (sid,)
        elif before is not None:
            query, params = (
                fields + "AND status='destroyed' AND ref<? ORDER BY ref DESC LIMIT 100",
                (sid, before),
            )
        else:
            query, params = (
                fields + "AND (status!='destroyed' OR id IN "
                "(SELECT id FROM novel_jobs WHERE session=? AND status='destroyed' ORDER BY ref DESC LIMIT 100)) ORDER BY ref DESC",
                (sid, sid),
            )
        with self.store.connection() as db:
            rows = db.execute(query, params).fetchall()
        return [
            {
                **dict(row),
                "ref": f"R{row['ref']}",
                "config": json.loads(row["config"]),
                "usage": json.loads(row["usage"]),
            }
            for row in rows
        ]

    def archive_before(self, sid, jobs):
        earliest = min((int(job["ref"][1:]) for job in jobs if job["status"] == "destroyed"), default=None)
        if earliest is None:
            return None
        with self.store.connection() as db:
            more = db.execute(
                "SELECT 1 FROM novel_jobs WHERE session=? AND status='destroyed' AND ref<? LIMIT 1",
                (sid, earliest),
            ).fetchone()
        return earliest if more else None

    def job(self, job_id):
        with self.store.connection() as db:
            row = db.execute("SELECT * FROM novel_jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise NotFound("原著调查者不存在")
        result = dict(row)
        for key in ("config", "progress", "usage"):
            result[key] = json.loads(result[key])
        return result

    def resolve(self, sid, ref):
        # Public handles are branch-local; UUIDs are never accepted from models.
        if not ref.startswith("R") or not ref[1:].isdigit():
            raise ValueError("请使用列表中的 R 编号")
        with self.store.connection() as db:
            row = db.execute(
                "SELECT id FROM novel_jobs WHERE session=? AND ref=?", (sid, int(ref[1:]))
            ).fetchone()
        if row is None:
            raise ValueError("此分支不存在该调查者")
        return self.job(row[0])

    @staticmethod
    def count_starts(db, sid, position):
        return db.execute(
            "SELECT count(*) FROM novel_jobs WHERE session=? AND round_position=?", (sid, position)
        ).fetchone()[0]

    def starts(self, sid, position):
        with self.store.connection() as db:
            return self.count_starts(db, sid, position)

    def create(self, sid, operation, position, tid, task, background, config):
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute(
                "SELECT id FROM novel_jobs WHERE session=? AND operation=?", (sid, operation)
            ).fetchone()
            if previous:
                return previous[0], False
            binding = db.execute(
                "SELECT source,enabled,next_ref FROM novel_bindings WHERE session=?", (sid,)
            ).fetchone()
            if binding is None or not binding["enabled"] or not binding["source"]:
                raise ValueError("用户尚未授权此对话查阅原著")
            if (
                db.execute(
                    "SELECT count(*) FROM novel_jobs WHERE session=? AND status!='destroyed'", (sid,)
                ).fetchone()[0]
                >= MAX_LIVING
            ):
                raise ValueError("同时最多三个调查者，请主动销毁不再需要的调查者")
            if self.count_starts(db, sid, position) >= MAX_STARTS:
                raise ValueError("本轮已启动五次调查；销毁、重试和重生成都不退回启动次数")
            # Forks inherit the next handle, not workers. Never let an inherited
            # report's R handle silently start addressing a newly created investigator.
            ref = binding["next_ref"]
            db.execute("UPDATE novel_bindings SET next_ref=next_ref+1 WHERE session=?", (sid,))
            job_id = uid()
            db.execute(
                "INSERT INTO novel_jobs(id,session,ref,source,operation,round_position,origin_turn,"
                "task,background,status,config,created) VALUES (?,?,?,?,?,?,?,?,?,'running',?,?)",
                (
                    job_id,
                    sid,
                    ref,
                    binding["source"],
                    operation,
                    position,
                    tid,
                    task,
                    background,
                    dump(config),
                    time.time(),
                ),
            )
            event(db, sid, "started", job_id)
        return job_id, True

    def trace(self, job_id, *, after=0, limit=None):
        sql = "SELECT seq,message,created FROM novel_trace WHERE job=? AND seq>? ORDER BY seq"
        with self.store.connection() as db:
            rows = db.execute(
                sql + (" LIMIT ?" if limit else ""), (job_id, after, limit) if limit else (job_id, after)
            ).fetchall()
        return [{**dict(row), "message": json.loads(row["message"])} for row in rows]

    def save(self, job, *, message=None, mail=None, usage=None):
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status FROM novel_jobs WHERE id=?", (job["id"],)).fetchone()
            if row is None or row[0] == "destroyed":
                return False  # An explicit revocation wins over every late network response.
            db.execute(
                "UPDATE novel_jobs SET status=?,progress=?,requests=?,elapsed=?,error=?,usage=? WHERE id=?",
                (
                    job["status"],
                    dump(job["progress"]),
                    job["requests"],
                    job["elapsed"],
                    job["error"],
                    dump(usage if usage is not None else job["usage"]),
                    job["id"],
                ),
            )
            if message is not None:
                db.execute(
                    "INSERT INTO novel_trace(job,message,created) VALUES (?,?,?)",
                    (job["id"], dump(message), time.time()),
                )
            if mail:
                db.execute(
                    "INSERT OR IGNORE INTO novel_mail(job,sender,kind,content,operation,created) "
                    "VALUES (?,'worker',?,?,?,?)",
                    (job["id"], mail["kind"], mail["content"], mail["operation"], time.time()),
                )
            event(db, job["session"], "updated", job["id"])
        return True

    def send(self, job_id, text, operation):
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT session,status FROM novel_jobs WHERE id=?", (job_id,)).fetchone()
            if row is None or row["status"] == "destroyed":
                raise ValueError("调查者已销毁，请按需新建")
            inserted = db.execute(
                "INSERT OR IGNORE INTO novel_mail(job,sender,kind,content,operation,created) "
                "VALUES (?,'main','message',?,?,?)",
                (job_id, text, operation, time.time()),
            )
            if inserted.rowcount:
                event(db, row["session"], "message", job_id)
                return True
            prior = db.execute(
                "SELECT delivered,revoked FROM novel_mail WHERE job=? AND sender='main' AND operation=?",
                (job_id, operation),
            ).fetchone()
            return prior["delivered"] is None and not prior["revoked"]

    def receive_worker(self, job):
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if (
                db.execute("SELECT status FROM novel_jobs WHERE id=?", (job["id"],)).fetchone()[0]
                == "destroyed"
            ):
                return []
            rows = db.execute(
                "SELECT * FROM novel_mail WHERE job=? AND sender='main' "
                "AND delivered IS NULL AND revoked=0 ORDER BY seq",
                (job["id"],),
            ).fetchall()
            messages = []
            for row in rows:
                # One label for follow-ups and answers alike, matching the investigator prompt's term.
                message = {"role": "user", "content": "委派方消息：\n" + row["content"]}
                db.execute(
                    "INSERT INTO novel_trace(job,message,created) VALUES (?,?,?)",
                    (job["id"], dump(message), time.time()),
                )
                db.execute("UPDATE novel_mail SET delivered='worker' WHERE seq=?", (row["seq"],))
                messages.append(message)
            if messages:
                # Consuming an answer and leaving the waiting/reported state is
                # one durable operation, including a crash before the next model request.
                job["progress"].pop("waiting_for_answer", None)
                job["progress"].pop("reported", None)
                job["status"] = "running"
                db.execute(
                    "UPDATE novel_jobs SET status='running',progress=? WHERE id=?",
                    (dump(job["progress"]), job["id"]),
                )
            return messages

    def inbox(self, sid):
        with self.store.connection() as db:
            rows = db.execute(
                "SELECT m.*,j.ref,j.task,j.background,j.round_position,s.name AS source_name,s.created AS source_created FROM novel_mail m "
                "JOIN novel_jobs j ON j.id=m.job JOIN novel_sources s ON s.id=j.source WHERE j.session=? AND m.sender='worker' "
                "AND m.delivered IS NULL AND m.revoked=0 ORDER BY m.seq",
                (sid,),
            ).fetchall()
        return [dict(row) for row in rows]

    def destroy(self, job_id):
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT session FROM novel_jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise NotFound("原著调查者不存在")
            db.execute("UPDATE novel_jobs SET status='destroyed' WHERE id=?", (job_id,))
            db.execute("UPDATE novel_mail SET revoked=1 WHERE job=? AND delivered IS NULL", (job_id,))
            event(db, row[0], "destroyed", job_id)

    def events(self, after):
        with self.store.connection() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM novel_events WHERE seq>? ORDER BY seq LIMIT 200", (after,)
                )
            ]

    def dispatch_state(self, job_id):
        with self.store.connection() as db:
            row = db.execute(
                "SELECT status,EXISTS(SELECT 1 FROM novel_mail m WHERE m.job=j.id "
                "AND m.sender='main' AND m.delivered IS NULL AND m.revoked=0) AS pending "
                "FROM novel_jobs j WHERE id=?",
                (job_id,),
            ).fetchone()
        return dict(row) if row else None

    def mark_running(self, job_id):
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT session,status FROM novel_jobs WHERE id=?", (job_id,)).fetchone()
            if row is None or row["status"] == "destroyed":
                raise ValueError("调查者已销毁，不能恢复")
            if row["status"] != "running":
                db.execute("UPDATE novel_jobs SET status='running',error=NULL WHERE id=?", (job_id,))
                event(db, row["session"], "resumed", job_id)

    def settle_unstarted_pause(self, job_id):
        with self.store.connection() as db:
            # Task.cancel() before the coroutine's first instruction cannot run its
            # finally block. Settle that queued case without reviving a destroyed task.
            changed = db.execute(
                "UPDATE novel_jobs SET status='paused',error='调查已暂停，已用额度保留' "
                "WHERE id=? AND status='running'",
                (job_id,),
            )
            if changed.rowcount:
                sid = db.execute("SELECT session FROM novel_jobs WHERE id=?", (job_id,)).fetchone()[0]
                event(db, sid, "paused", job_id)
