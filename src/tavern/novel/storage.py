"""Durable research tasks and mailboxes, independent of story transactions."""

import json
import time

from tavern.core.errors import NotFound
from tavern.novel.source import available, directory
from tavern.storage.schema import add_columns
from tavern.storage.store import Conflict, dump, uid

MAX_LIVING = 3  # Investigators not yet destroyed, per branch.
MAX_STARTS = 5  # Starts per story turn, including destroyed and regenerated attempts.
NEEDS_SOURCE = "请导入参考文件，或填写调查指引并开启联网查询"
DISABLED = "当前分支的资料调查者尚未授权或已关闭"

JOB_COLUMNS = """(
    id TEXT PRIMARY KEY, session TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    ref INTEGER NOT NULL, source TEXT REFERENCES novel_sources(id),
    operation TEXT NOT NULL, round_position INTEGER NOT NULL, origin_turn TEXT NOT NULL,
    task TEXT NOT NULL, background TEXT NOT NULL, status TEXT NOT NULL,
    config TEXT NOT NULL, progress TEXT NOT NULL DEFAULT '{}',
    requests INTEGER NOT NULL DEFAULT 0, elapsed REAL NOT NULL DEFAULT 0,
    usage TEXT NOT NULL DEFAULT '{}', error TEXT, created REAL NOT NULL,
    web_enabled INTEGER NOT NULL DEFAULT 0, web_guidance TEXT NOT NULL DEFAULT '',
    UNIQUE(session,ref), UNIQUE(session,operation))"""


def initialize(store):
    with store.connection() as db:
        # Rebuilding the job table must leave trace and mailbox children in place.
        # Disable FK actions only on this connection and check the affected tables before commit.
        db.execute("PRAGMA foreign_keys=OFF")
        db.executescript(f"""
            BEGIN IMMEDIATE;
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
            CREATE TABLE IF NOT EXISTS novel_jobs{JOB_COLUMNS};
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
            CREATE INDEX IF NOT EXISTS novel_mail_pending ON novel_mail(sender,job,seq) WHERE delivered IS NULL AND revoked=0;
            CREATE INDEX IF NOT EXISTS novel_events_session ON novel_events(session,seq);
        """)
        if any(
            column["name"] == "source" and column["notnull"]
            for column in db.execute("PRAGMA table_info(novel_jobs)")
        ):
            fields = "id,session,ref,source,operation,round_position,origin_turn,task,background,status,config,progress,requests,elapsed,usage,error,created"
            db.execute(f"CREATE TABLE novel_jobs_upgrade{JOB_COLUMNS}")
            db.execute(f"INSERT INTO novel_jobs_upgrade({fields}) SELECT {fields} FROM novel_jobs")
            db.execute("DROP TABLE novel_jobs")
            db.execute("ALTER TABLE novel_jobs_upgrade RENAME TO novel_jobs")
            for table in ("novel_jobs", "novel_trace", "novel_mail"):
                if db.execute(f"PRAGMA foreign_key_check({table})").fetchone():
                    raise ValueError("资料调查迁移发现无效的任务或消息引用")
        db.execute("CREATE INDEX IF NOT EXISTS novel_jobs_session ON novel_jobs(session,ref)")
        db.execute(
            "CREATE INDEX IF NOT EXISTS novel_jobs_living ON novel_jobs(session,ref) WHERE status!='destroyed'"
        )
        db.execute("CREATE INDEX IF NOT EXISTS novel_jobs_round ON novel_jobs(session,round_position)")
        add_columns(
            db,
            "novel_bindings",
            {
                "advisor_enabled": "INTEGER NOT NULL DEFAULT 0",
                "advisor_revision": "INTEGER NOT NULL DEFAULT 0",
                "web_enabled": "INTEGER NOT NULL DEFAULT 0",
                "web_guidance": "TEXT NOT NULL DEFAULT ''",
            },
        )


def research_sources(root, binding) -> tuple[str | None, bool]:
    source = binding["source"]
    file = source if source and available(directory(root, source)) else None
    web = bool(binding["web_enabled"] and (file or binding["web_guidance"].strip()))
    return file, web


def research_enabled(root, binding) -> bool:
    return bool(binding and binding["enabled"] and any(research_sources(root, binding)))


def job_access_error(enabled, file, web, job) -> str | None:
    if not enabled:
        return DISABLED
    if job["source"] and job["source"] != file:
        return "调查使用的文件副本当前不可用"
    if job["web_enabled"] and not web:
        return "请重新开启联网查询，再继续该调查"
    return None


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
        "INSERT INTO novel_bindings(session,source,enabled,next_ref,advisor_enabled,web_enabled,web_guidance) "
        "SELECT ?,source,enabled,next_ref,advisor_enabled,web_enabled,web_guidance FROM novel_bindings WHERE session=?",
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
            raise NotFound("参考文件不存在")
        return dict(row)

    def binding(self, sid):
        with self.store.connection() as db:
            row = db.execute("SELECT * FROM novel_bindings WHERE session=?", (sid,)).fetchone()
        return (
            dict(row)
            if row
            else {
                "session": sid,
                "source": None,
                "enabled": 0,
                "revision": 0,
                "next_ref": 1,
                "advisor_enabled": 0,
                "web_enabled": 0,
                "web_guidance": "",
            }
        )

    def active_sources(self, sid) -> tuple[str | None, bool]:
        binding = self.binding(sid)
        return research_sources(self.store.root, binding) if binding["enabled"] else (None, False)

    def enabled(self, sid):
        return any(self.active_sources(sid))

    def require_job_access(self, job, db=None):
        binding = (
            db.execute("SELECT * FROM novel_bindings WHERE session=?", (job["session"],)).fetchone()
            if db is not None
            else self.binding(job["session"])
        )
        file, web = research_sources(self.store.root, binding)
        reason = job_access_error(binding["enabled"] and (file or web), file, web, job)
        if reason:
            raise ValueError(reason)

    @staticmethod
    def advisor_access(db, sid):
        row = db.execute(
            "SELECT source,advisor_revision FROM novel_bindings WHERE session=? AND advisor_enabled=1", (sid,)
        ).fetchone()
        return dict(row) if row else None

    @staticmethod
    def binding_for_update(db, sid, expected):
        session = db.execute("SELECT 1 FROM sessions WHERE id=?", (sid,)).fetchone()
        if session is None:
            raise NotFound("对话不存在")
        db.execute("INSERT OR IGNORE INTO novel_bindings(session) VALUES (?)", (sid,))
        row = db.execute("SELECT * FROM novel_bindings WHERE session=?", (sid,)).fetchone()
        if row["revision"] != expected:
            raise Conflict("资料调查设置已改变，请刷新后重试")
        return row

    def editable(self, db, sid, expected):
        row = self.binding_for_update(db, sid, expected)
        self.check_file_editable(db, row)
        return row

    @staticmethod
    def check_file_editable(db, row):
        if (
            row["source"]
            and db.execute(
                "SELECT 1 FROM novel_jobs WHERE session=? AND source=? AND status!='destroyed' LIMIT 1",
                (row["session"], row["source"]),
            ).fetchone()
        ):
            raise Conflict("有尚未销毁的调查者使用当前文件，请先销毁对应调查者，再替换或解绑")
        if db.execute(
            "SELECT 1 FROM turns WHERE session=? AND status IN ('queued','running','awaiting_review')",
            (row["session"],),
        ).fetchone():
            raise Conflict("请先完成或停止主对话当前回合，再修改参考文件")

    def bind(self, sid, expected, source_id, metadata, *, prepared=None):
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            binding = self.editable(db, sid, expected)
            enabled = research_enabled(self.store.root, binding)
            if prepared is not None:
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
                "UPDATE novel_bindings SET source=?,enabled=?,advisor_enabled=0,revision=revision+1 WHERE session=?",
                (source_id, int(enabled), sid),
            )
            event(db, sid, "source_imported")

    def configure(self, sid, expected, *, enabled=None, unbind=False, web_enabled=None, web_guidance=None):
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self.binding_for_update(db, sid, expected)
            if unbind:
                self.check_file_editable(db, row)
            updated = dict(row)
            if unbind:
                updated.update(source=None, advisor_enabled=0)
            if web_enabled is not None:
                updated["web_enabled"] = int(web_enabled)
            if web_guidance is not None:
                updated["web_guidance"] = web_guidance
            has_source = any(research_sources(self.store.root, updated))
            if enabled and not has_source:
                raise ValueError(NEEDS_SOURCE)
            requested = research_enabled(self.store.root, row) if enabled is None else enabled
            updated["enabled"] = int(has_source and requested)
            db.execute(
                "UPDATE novel_bindings SET source=:source,enabled=:enabled,advisor_enabled=:advisor_enabled,"
                "web_enabled=:web_enabled,web_guidance=:web_guidance,revision=revision+1 WHERE session=:session",
                updated,
            )
            event(db, sid, "authorization")
            if row["enabled"] and not updated["enabled"]:
                return self.pause_in_flight(db, sid, "资料调查者已关闭，已用额度保留")
            if row["web_enabled"] and not updated["web_enabled"]:
                return self.pause_in_flight(db, sid, "联网查询已关闭，已用额度保留", require_web=True)
            return []

    @staticmethod
    def pause_in_flight(db, sid, reason, *, require_web=False):
        # Commit revocation and status together. Late results must not complete a paused job.
        scope = " AND web_enabled=1" if require_web else ""
        return [
            row[0]
            for row in db.execute(
                "UPDATE novel_jobs SET status='paused',error=? WHERE session=? "
                "AND (status='running' OR (status IN ('waiting','completed') AND EXISTS("
                "SELECT 1 FROM novel_mail WHERE job=novel_jobs.id AND sender='main' "
                "AND delivered IS NULL AND revoked=0)))" + scope + " RETURNING id",
                (reason, sid),
            )
        ]

    def configure_advisor(self, sid, expected, enabled):
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self.binding_for_update(db, sid, expected)
            if enabled and not row["source"]:
                raise ValueError("请先导入参考文件")
            if row["advisor_enabled"] != enabled:
                # Revoking and reopening the same book must not reauthorize a pending consultation.
                db.execute(
                    "UPDATE novel_bindings SET advisor_enabled=?,advisor_revision=advisor_revision+1,"
                    "revision=revision+1 WHERE session=?",
                    (int(enabled), sid),
                )
                event(db, sid, "advisor_authorization")

    def jobs(self, sid, *, living_only=False, before=None):
        fields = (
            "SELECT id,ref,source,task,background,status,config,requests,elapsed,usage,error,created,round_position,"
            "web_enabled,web_guidance,"
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
                "web_enabled": bool(row["web_enabled"]),
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
            raise NotFound("资料调查者不存在")
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
            binding = db.execute("SELECT * FROM novel_bindings WHERE session=?", (sid,)).fetchone()
            if binding is None or not binding["enabled"]:
                raise ValueError(DISABLED)
            file, web = research_sources(self.store.root, binding)
            if not (file or web):
                raise ValueError(NEEDS_SOURCE)
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
                "task,background,status,config,created,web_enabled,web_guidance) "
                "VALUES (?,?,?,?,?,?,?,?,?,'running',?,?,?,?)",
                (
                    job_id,
                    sid,
                    ref,
                    file,
                    operation,
                    position,
                    tid,
                    task,
                    background,
                    dump(config),
                    time.time(),
                    int(web),
                    binding["web_guidance"] if web else "",
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
            row = db.execute("SELECT status,error FROM novel_jobs WHERE id=?", (job["id"],)).fetchone()
            if row is None or row["status"] == "destroyed":
                return False  # An explicit revocation wins over every late network response.
            if row["status"] == "paused" and job["status"] != "paused":
                return False
            db.execute(
                "UPDATE novel_jobs SET status=?,progress=?,requests=?,elapsed=?,error=?,usage=? WHERE id=?",
                (
                    job["status"],
                    dump(job["progress"]),
                    job["requests"],
                    job["elapsed"],
                    row["error"] if row["status"] == "paused" else job["error"],
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
            row = db.execute(
                "SELECT session,status,source,web_enabled FROM novel_jobs WHERE id=?", (job_id,)
            ).fetchone()
            if row is None or row["status"] == "destroyed":
                raise ValueError("调查者已销毁，请按需新建")
            self.require_job_access(row, db)
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
            if db.execute("SELECT status FROM novel_jobs WHERE id=?", (job["id"],)).fetchone()[0] in {
                "destroyed",
                "paused",
            }:
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
                "SELECT m.*,j.ref,j.task,j.round_position,j.web_enabled,"
                "json_extract(j.config,'$.auto_destroy') AS auto_destroy,"
                "s.name AS source_name,s.created AS source_created FROM novel_mail m "
                "JOIN novel_jobs j ON j.id=m.job LEFT JOIN novel_sources s ON s.id=j.source "
                "WHERE j.session=? AND m.sender='worker' "
                "AND m.delivered IS NULL AND m.revoked=0 ORDER BY m.seq",
                (sid,),
            ).fetchall()
        return [dict(row) for row in rows]

    def destroy(self, job_id):
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT session FROM novel_jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise NotFound("资料调查者不存在")
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
            row = db.execute(
                "SELECT session,status,source,web_enabled FROM novel_jobs WHERE id=?", (job_id,)
            ).fetchone()
            if row is None or row["status"] == "destroyed":
                raise ValueError("调查者已销毁，不能恢复")
            self.require_job_access(row, db)
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
