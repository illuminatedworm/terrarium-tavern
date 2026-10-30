"""User-visible source authorization, research controls and event observation."""

import asyncio
import tempfile
from pathlib import Path

from fastapi import HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from tavern.core.model_view import HANDLE
from tavern.novel.source import MAX_BYTES, NovelText, directory, import_file
from tavern.storage.store import dump, uid


class Authorization(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expected_revision: int = Field(ge=0)
    enabled: bool = False
    unbind: bool = False


def register_routes(app, service, main_runtime):
    store, data = service.store, service.data

    @app.get("/api/sessions/{sid}/novel")
    async def read(sid: str):
        return service.public(sid)

    @app.post("/api/sessions/{sid}/novel/source")
    async def upload(
        sid: str,
        request: Request,
        expected_revision: int = Query(..., ge=0),
        filename: str = Query(..., min_length=1, max_length=255),
        encoding: str = Query("utf-8"),
    ):
        if request.headers.get("content-type", "").split(";", 1)[0] != "application/octet-stream":
            raise HTTPException(415, "原著上传需要 application/octet-stream 原始文件")
        length = request.headers.get("content-length")
        if length is not None and (not length.isdecimal() or int(length) > MAX_BYTES):
            raise ValueError("原著文件长度无效或超过 64 MiB")
        with store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            data.editable(db, sid, expected_revision)
        # Bound the request while receiving, before a multipart parser could spool
        # an arbitrarily large file. Staging and publishing never modify the old copy.
        with tempfile.TemporaryDirectory(prefix="tavern-novel-", dir=store.root) as temporary:
            root, identity, size = Path(temporary), uid(), 0
            with (root / "upload").open("w+b") as incoming:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > MAX_BYTES:
                        raise ValueError("原著文件不能超过 64 MiB")
                    incoming.write(chunk)
                incoming.seek(0)

                def ingest():
                    metadata = import_file(root, identity, incoming, filename, encoding)
                    data.bind(sid, expected_revision, identity, metadata, prepared=directory(root, identity))

                task = asyncio.create_task(asyncio.to_thread(ingest))
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    await task  # Wait for the file writer before closing its stream/staging directory.
                    raise
        service.notify()
        return service.public(sid)

    @app.patch("/api/sessions/{sid}/novel")
    async def authorize(sid: str, payload: Authorization):
        if payload.unbind and payload.enabled:
            raise ValueError("解绑和开启不能同时进行")
        if payload.enabled:
            binding = data.binding(sid)
            if not binding["source"]:
                raise ValueError("请先导入原著文件")
            NovelText(store.root, data.source(binding["source"]))
        data.configure(sid, payload.expected_revision, enabled=payload.enabled, unbind=payload.unbind)
        service.notify()
        return service.public(sid)

    def owned_source(sid, source_id):
        with store.connection() as db:
            if not db.execute(
                "SELECT 1 FROM novel_source_history WHERE session=? AND source=?", (sid, source_id)
            ).fetchone():
                raise ValueError("此文件不属于当前对话的原著历史")
        return data.source(source_id)

    @app.get("/api/sessions/{sid}/novel/sources/{source_id}/download")
    async def download(sid: str, source_id: str):
        source = owned_source(sid, source_id)
        book = NovelText(store.root, source)
        return FileResponse(book.folder / "original", filename=source["name"], media_type="text/plain")

    @app.get("/api/sessions/{sid}/novel/sources/{source_id}/text")
    async def excerpt(
        sid: str, source_id: str, start: int = Query(0, ge=0), length: int = Query(8000, ge=1, le=20000)
    ):
        return await asyncio.to_thread(
            NovelText(store.root, owned_source(sid, source_id)).read, start, length
        )

    @app.get("/api/sessions/{sid}/novel/jobs/{ref}/trace")
    async def trace(sid: str, ref: str, after: int = Query(0, ge=0)):
        job = data.resolve(sid, ref)
        records = data.trace(job["id"], after=after, limit=50)
        refs = job["progress"].get("model_references", {})
        reserved = set(job["progress"].get("reserved_references", []))

        def visible(text):
            return HANDLE.sub(
                lambda match: refs.get(match[0], match[0]) if match[0] not in reserved else match[0], text
            )

        for record in records:
            record["message"] = {
                k: v
                for k, v in record["message"].items()
                if k in {"role", "content", "tool_calls", "tool_call_id", "through", "_novel_compaction"}
            }
            record["message"]["content"] = visible(record["message"]["content"])
            for call in record["message"].get("tool_calls", []):
                call["function"]["arguments"] = visible(call["function"]["arguments"])
        return {
            "records": records,
            "has_more": len(records) == 50,
            "detail_expired": bool(job["progress"].get("detail_expired")),
            "reports": [dict(r) for r in report_rows(job["id"])],
        }

    def report_rows(job_id):
        with store.connection() as db:
            return db.execute(
                "SELECT kind,content,created FROM novel_mail WHERE job=? AND sender='worker' ORDER BY seq",
                (job_id,),
            ).fetchall()

    @app.get("/api/sessions/{sid}/novel/jobs")
    async def older_jobs(sid: str, before: int = Query(..., ge=1)):
        jobs = data.jobs(sid, before=before)
        return {"jobs": jobs, "archive_before": data.archive_before(sid, jobs)}

    @app.post("/api/sessions/{sid}/novel/jobs/{ref}/{action}")
    async def control(sid: str, ref: str, action: str):
        job = data.resolve(sid, ref)
        if action == "destroy":
            await service.destroy(job["id"])
        elif action == "pause":
            await service.pause(job["id"])
        elif action == "resume":
            if job["status"] not in {"paused", "failed"}:
                raise ValueError("只能恢复暂停或失败的调查者")
            service.launch(job["id"])
        else:
            raise ValueError("未知调查操作")
        return service.public(sid)

    @app.post("/api/sessions/{sid}/novel/stop-all")
    async def stop_all(sid: str):
        session = store.session(sid)
        for turn in session["turns"]:
            if turn["status"] in {"queued", "running", "awaiting_review"}:
                await main_runtime.cancel(turn["id"])
        for job in data.jobs(sid):
            if job["status"] == "running":
                await service.pause(job["id"])
        return service.public(sid)

    @app.get("/api/novel/activity")
    async def activity():
        with store.connection() as db:
            rows = db.execute(
                "SELECT j.session,s.title,j.ref,j.status FROM novel_jobs j "
                "JOIN sessions s ON s.id=j.session WHERE j.status!='destroyed' ORDER BY j.created"
            ).fetchall()
            cursor = db.execute("SELECT coalesce(max(seq),0) FROM novel_events").fetchone()[0]
        return {"jobs": [dict(row) for row in rows], "cursor": cursor}

    @app.get("/api/novel/events")
    async def events(request: Request, after: int = Query(0, ge=0)):
        async def stream():
            cursor = max(after, int(request.headers.get("last-event-id", "0")))
            while not service.closed:
                generation = service.generation
                batch = data.events(cursor)
                for event in batch:
                    cursor = event["seq"]
                    yield f"id: {cursor}\ndata: {dump(event)}\n\n"
                if await request.is_disconnected():
                    return
                if len(batch) == 200:
                    continue
                await service.wait_change(generation)
                if generation != service.generation:
                    # Session deletion cascades its audit events; still refresh the
                    # global activity list instead of leaving a deleted branch's badge.
                    yield "event: refresh\ndata: {}\n\n"
                yield ": heartbeat\n\n"
            yield "event: end\ndata: {}\n\n"

        return StreamingResponse(
            stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"}
        )
