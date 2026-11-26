"""Advisor API and reconnectable event observation."""

import asyncio

from fastapi import Query, Request
from fastapi.responses import StreamingResponse

from tavern.advisor.presentation import exchange_trace, preview_view
from tavern.advisor.runtime import AdvisorRevisionRequest, ConsultationRequest, OptionsRequest
from tavern.core.errors import NotFound
from tavern.storage.store import dump


def register_routes(app, advisor):
    @app.get("/api/sessions/{sid}/advisor")
    def read(sid: str, reader: bool = False, after: int = Query(0, ge=0)):
        return advisor.public(sid, reader=reader, after=after)

    @app.get("/api/sessions/{sid}/advisor/exchanges/{seq}/trace")
    def trace(sid: str, seq: int):
        with advisor.store.connection() as db:
            return exchange_trace(db, sid, seq)

    @app.patch("/api/sessions/{sid}/advisor/options")
    def options(sid: str, payload: OptionsRequest, reader: bool = False, after: int = Query(0, ge=0)):
        return advisor.options(sid, payload, reader=reader, after=after)

    @app.get("/api/sessions/{sid}/advisor/preview")
    def preview(sid: str, reader: bool = False):
        value = advisor.preview(sid, local_estimate=True)
        return preview_view(value) if reader else value

    @app.get("/api/sessions/{sid}/advisor/stats")
    def stats(sid: str):
        preview = advisor.preview(sid, stats_only=True)
        return {key: preview[key] for key in ("context_usage", "context_limit")}

    @app.post("/api/sessions/{sid}/advisor/runs")
    async def submit(sid: str, payload: ConsultationRequest):
        return advisor.submit(sid, payload)

    @app.post("/api/sessions/{sid}/advisor/discard")
    def discard(sid: str, payload: AdvisorRevisionRequest, reader: bool = False, after: int = Query(0, ge=0)):
        advisor.data.discard(sid, payload.expected_revision)
        return advisor.public(sid, reader=reader, after=after)

    @app.post("/api/sessions/{sid}/advisor/clear")
    def clear(sid: str, payload: AdvisorRevisionRequest, reader: bool = False):
        advisor.data.clear(sid, payload.expected_revision)
        return advisor.public(sid, reader=reader)

    @app.get("/api/advisor-runs/{rid}")
    async def run(rid: str):
        return advisor.data.run(rid)

    @app.post("/api/advisor-runs/{rid}/cancel")
    async def cancel(rid: str):
        return await advisor.cancel(rid)

    @app.get("/api/advisor-runs/{rid}/events")
    async def events(rid: str, request: Request, after: int = Query(0, ge=0), reader: bool = False):
        advisor.data.run_status(rid)

        async def stream():
            cursor = max(after, int(request.headers.get("last-event-id", "0")))
            while True:
                # A terminal state read before the batch includes its final
                # event even when the producer finishes during a previous yield.
                try:
                    active = advisor.data.run_status(rid) in ("queued", "running")
                except NotFound:
                    active = False  # Another tab may have cleared the consultation.
                batch = advisor.data.events(rid, cursor, reader=reader)
                for event in batch:
                    cursor = event["seq"]
                    yield f"id: {cursor}\ndata: {dump(event)}\n\n"
                if len(batch) < 200 and not active:
                    yield "event: end\ndata: {}\n\n"
                    return
                if await request.is_disconnected():
                    return
                if len(batch) < 200:
                    await asyncio.sleep(0.15)

        return StreamingResponse(
            stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"}
        )
