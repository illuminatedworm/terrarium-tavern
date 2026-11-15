"""Event-driven post-commit queue. Unknown remote outcomes require a manual retry."""

import asyncio
import json
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from functools import partial

from tavern.core.errors import NotFound
from tavern.media import storage
from tavern.media.providers import ADAPTERS, connection, reference_bytes
from tavern.storage.store import Conflict

MEDIA_ENABLED = "json_extract(s.media_config, '$.' || j.kind || '.enabled') = 1"


async def file_io(operation, *args):
    """Cancellation must not leave a file reader or SQLite publisher behind."""
    task = asyncio.create_task(asyncio.to_thread(operation, *args))
    interrupted = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            interrupted = True
    result = task.result()
    if interrupted:
        raise asyncio.CancelledError
    return result


class MediaService:
    def __init__(self, store):
        self.store = store
        self.tasks = {}
        self.listeners = defaultdict(set)
        self.wake = asyncio.Event()
        self.closed = False
        self.loop = None
        self.cancelled = set()
        self.deleting = set()
        store.media_listener = self.notify

    async def start(self):
        self.loop = asyncio.get_running_loop()
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                "SELECT j.id,j.session,j.status FROM media_jobs j JOIN sessions s ON s.id=j.session "
                f"WHERE j.status='running' OR (j.status='queued' AND NOT coalesce({MEDIA_ENABLED},0))"
            ).fetchall()
            for row in rows:
                if row["status"] == "running":
                    status = "interrupted"
                    error = "应用中断，服务端结果不明；未自动重发，手动重试可能再次计费"
                else:
                    status = "cancelled"
                    error = "此故事已关闭生成，未发起请求"
                db.execute(
                    "UPDATE media_jobs SET status=?,error=?,updated=?,seq=? WHERE id=?",
                    (
                        status,
                        error,
                        time.time(),
                        storage.next_sequence(db, row["session"]),
                        row["id"],
                    ),
                )
        self.tasks["dispatcher"] = asyncio.create_task(self.dispatch())
        self.wake.set()

    def notify(self, sid):
        # Commits and API actions can finish on FastAPI's worker threads.
        if self.loop and not self.closed:
            self.loop.call_soon_threadsafe(self.changed, sid)

    def changed(self, sid, jid=None):
        self.wake.set()
        if not self.listeners.get(sid):
            return
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT media_sequence FROM sessions WHERE id=?", (sid,)).fetchone()
            sequence = storage.next_sequence(db, sid) if row else None
        if row is None:
            for queue in self.listeners[sid]:
                while not queue.empty():
                    queue.get_nowait()
                queue.put_nowait(None)
            return
        event = (
            {"seq": sequence, "job": storage.job(self.store, sid, jid)}
            if jid
            else {"seq": sequence, "reset": True}
        )
        for queue in self.listeners[sid]:
            # A slow/disconnected observer can always reconstruct authoritative state.
            if queue.full():
                while not queue.empty():
                    queue.get_nowait()
                queue.put_nowait({"seq": sequence, "reset": True})
            else:
                queue.put_nowait(event)

    async def dispatch(self):
        while not self.closed:
            await self.wake.wait()
            self.wake.clear()
            while not self.closed and len(self.tasks) < 3:
                with self.store.connection() as db:
                    db.execute("BEGIN IMMEDIATE")
                    row = db.execute(
                        "SELECT j.* FROM media_jobs j JOIN sessions s ON s.id=j.session "
                        f"WHERE j.status='queued' AND {MEDIA_ENABLED} "
                        "AND j.session NOT IN (SELECT value FROM json_each(?)) "
                        "AND j.id NOT IN (SELECT value FROM json_each(?)) "
                        "ORDER BY j.updated,j.rowid LIMIT 1",
                        (json.dumps(list(self.deleting)), json.dumps(list(self.tasks))),
                    ).fetchone()
                    if row is None:
                        break
                    db.execute(
                        "UPDATE media_jobs SET status='running',updated=?,seq=? WHERE id=?",
                        (time.time(), storage.next_sequence(db, row["session"]), row["id"]),
                    )
                jid = row["id"]
                task = asyncio.create_task(self.run(row))
                self.tasks[jid] = task
                task.add_done_callback(partial(self.finished_task, row))
                self.changed(row["session"], jid)

    def finish(self, row, result):
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT status FROM media_jobs WHERE id=?", (row["id"],)).fetchone()
            if current is None or current[0] != "running":
                return
            request = json.loads(row["request"])
            name = request["speaker"] if row["kind"] == "speech" else "剧情插图"
            asset = storage.add_asset(
                self.store,
                db,
                row["session"],
                data=result.data,
                kind=row["kind"],
                mime=result.mime,
                name=name,
                description=request.get("prompt", ""),
                message=row["message"],
            )
            db.execute(
                "UPDATE media_jobs SET status='completed',asset=?,error='',updated=?,seq=? WHERE id=?",
                (asset, time.time(), storage.next_sequence(db, row["session"]), row["id"]),
            )

    def failed(self, row, status, error):
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute(
                "SELECT 1 FROM media_jobs WHERE id=? AND status='running'", (row["id"],)
            ).fetchone():
                db.execute(
                    "UPDATE media_jobs SET status=?,error=?,updated=?,seq=? WHERE id=?",
                    (status, error, time.time(), storage.next_sequence(db, row["session"]), row["id"]),
                )

    async def run(self, row):
        key = ""
        try:
            request = json.loads(row["request"])
            key, proxy = connection(self.store, request)
            references = await file_io(reference_bytes, self.store, row["session"], request)
            async with asyncio.timeout(request["connection"]["timeout_seconds"]):
                result = await ADAPTERS[request["connection"]["adapter"]].generate(
                    request,
                    key=key,
                    proxy=proxy,
                    references=references,
                )
            # A SQLite/blob publication already running in a worker must finish
            # before shutdown releases this owner. Never report a partial save.
            await file_io(self.finish, row, result)
        except Exception as error:
            # This is the background task boundary: expose failure on its own
            # media card without reverting the committed story or retrying it.
            detail = str(error) or type(error).__name__
            if key:
                detail = detail.replace(key, "[API Key]")
            self.failed(row, "failed", detail)

    def finished_task(self, row, task):
        jid = row["id"]
        try:
            # A task cancelled before its first step never enters run().
            if task.cancelled():
                manual = jid in self.cancelled
                self.failed(
                    row,
                    "cancelled" if manual else "interrupted",
                    "已停止本地处理；服务端可能已计费，未自动重发",
                )
            else:
                task.result()
        finally:
            del self.tasks[jid]
            self.cancelled.discard(jid)
            self.changed(row["session"], jid)

    def request_cancel(self, jid):
        if task := self.tasks.get(jid):
            self.cancelled.add(jid)
            task.cancel()
            return task

    async def cancel(self, sid, jid):
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status FROM media_jobs WHERE id=? AND session=?", (jid, sid)).fetchone()
            if row is None:
                raise NotFound("媒体任务不存在")
            if row["status"] == "queued":
                db.execute(
                    "UPDATE media_jobs SET status='cancelled',error='用户取消，未发起请求',updated=?,seq=? "
                    "WHERE id=?",
                    (time.time(), storage.next_sequence(db, sid), jid),
                )
        if row["status"] == "queued":
            self.changed(sid, jid)
        elif row["status"] == "running" and (task := self.request_cancel(jid)):
            await asyncio.gather(task, return_exceptions=True)

    async def cancel_running(self, sid, kinds=("speech", "image")):
        with self.store.connection() as db:
            rows = db.execute(
                "SELECT id FROM media_jobs WHERE session=? AND status='running' "
                "AND kind IN (SELECT value FROM json_each(?))",
                (sid, json.dumps(kinds)),
            ).fetchall()
        tasks = [task for (jid,) in rows if (task := self.request_cancel(jid))]
        await asyncio.gather(*tasks, return_exceptions=True)

    @asynccontextmanager
    async def deleting_session(self, sid):
        if sid in self.deleting:
            raise Conflict("正在删除此故事")
        # Keep queued jobs ineligible through cancellation and the final delete
        # transaction. On failure, surviving queued jobs are still recoverable.
        self.deleting.add(sid)
        try:
            await self.cancel_running(sid)
            yield
        finally:
            self.deleting.remove(sid)
            self.changed(sid)

    def stop(self):
        self.closed = True
        self.wake.set()
        for queues in self.listeners.values():
            for queue in queues:
                if queue.full():
                    queue.get_nowait()
                queue.put_nowait(None)
        for task in list(self.tasks.values()):
            task.cancel()

    async def close(self):
        self.stop()
        await asyncio.gather(*list(self.tasks.values()), return_exceptions=True)
