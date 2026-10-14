"""Advisor API and reconnectable event observation."""
import asyncio

from fastapi import Query, Request
from fastapi.responses import StreamingResponse

from tavern.advisor.runtime import AdvisorRevisionRequest, ConsultationRequest, OptionsRequest
from tavern.storage.store import dump


def register_routes(app, advisor):
    @app.get("/api/sessions/{sid}/advisor")
    async def read(sid: str):
        return advisor.public(sid)

    @app.patch("/api/sessions/{sid}/advisor/options")
    async def options(sid: str, payload: OptionsRequest):
        return advisor.options(sid, payload)

    @app.get("/api/sessions/{sid}/advisor/preview")
    async def preview(sid: str):
        return advisor.preview(sid)

    @app.post("/api/sessions/{sid}/advisor/runs")
    async def submit(sid: str, payload: ConsultationRequest):
        return advisor.submit(sid, payload)

    @app.post("/api/sessions/{sid}/advisor/discard")
    async def discard(sid: str, payload: AdvisorRevisionRequest):
        advisor.data.discard(sid, payload.expected_revision)
        return advisor.public(sid)

    @app.post("/api/sessions/{sid}/advisor/clear")
    async def clear(sid: str, payload: AdvisorRevisionRequest):
        advisor.data.clear(sid, payload.expected_revision)
        return advisor.public(sid)

    @app.get("/api/advisor-runs/{rid}")
    async def run(rid: str):
        return advisor.data.run(rid)

    @app.post("/api/advisor-runs/{rid}/cancel")
    async def cancel(rid: str):
        return await advisor.cancel(rid)

    @app.get("/api/advisor-runs/{rid}/events")
    async def events(rid: str, request: Request, after: int = Query(0, ge=0)):
        advisor.data.run(rid)

        async def stream():
            cursor = max(after, int(request.headers.get("last-event-id", "0")))
            while True:
                batch = advisor.data.events(rid, cursor)
                for event in batch:
                    cursor = event["seq"]
                    yield f"id: {cursor}\ndata: {dump(event)}\n\n"
                try:
                    active = advisor.data.run(rid)["status"] in ("queued", "running")
                except KeyError:
                    active = False  # Another tab may have cleared the consultation.
                if len(batch) < 200 and not active:
                    yield "event: end\ndata: {}\n\n"
                    return
                if await request.is_disconnected():
                    return
                await asyncio.sleep(.15)
        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})
