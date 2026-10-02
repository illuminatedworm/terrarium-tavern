import asyncio

from fastapi import Query, Request
from fastapi.responses import StreamingResponse

from tavern.core.models import (
    CardEditorCreate,
    CardEditorRequest,
    CardEditorRevision,
    CardEditorSave,
    ContentEdit,
)
from tavern.storage.store import dump


def register_editor_routes(app, editor):
    from tavern.character.editor_api import register_conversation_routes
    register_conversation_routes(app, editor)
    @app.get("/api/card-edits")
    async def orphan_edits():
        with editor.store.connection() as db:
            return [dict(row) for row in db.execute("""SELECT w.id,w.created,
                json_extract(a.value,'$.card') AS card, json_extract(a.value,'$.source.name') AS name
                FROM card_editor_conversations w JOIN json_each(w.payload,'$.cards') a
                LEFT JOIN cards c ON c.id=json_extract(a.value,'$.card')
                WHERE json_array_length(w.payload,'$.cards')=1
                    AND json_extract(a.value,'$.card') IS NOT NULL AND c.id IS NULL ORDER BY w.created DESC""")]

    @app.get("/api/cards/{cid}/edits")
    async def list_edits(cid: str):
        return editor.edits.list(cid)

    @app.post("/api/cards/{cid}/edits")
    async def create(cid: str, payload: CardEditorCreate):
        return editor.edits.create(cid, payload.document, payload.expected_revision)

    @app.get("/api/card-edits/{wid}")
    async def get(wid: str):
        return editor.edits.get(wid)

    @app.post("/api/card-edits/{wid}/new-context")
    async def new_context(wid: str, payload: CardEditorRevision):
        return editor.edits.new_context(wid, payload.expected_revision)

    @app.put("/api/card-edits/{wid}")
    async def update(wid: str, payload: ContentEdit):
        if payload.expected_revision is None:
            raise ValueError("缺少草稿版本")
        return editor.edits.update(wid, payload.document, payload.expected_revision)

    @app.delete("/api/card-edits/{wid}")
    async def discard(wid: str, payload: CardEditorRevision):
        editor.edits.delete(wid, payload.expected_revision)
        return {"deleted": True}

    @app.post("/api/card-edits/{wid}/undo")
    async def undo(wid: str, payload: CardEditorRevision):
        return editor.edits.undo(wid, payload.expected_revision)

    @app.post("/api/card-edits/{wid}/reload")
    async def reload(wid: str, payload: CardEditorRevision):
        return editor.edits.reload(wid, payload.expected_revision)

    @app.post("/api/card-edits/{wid}/save")
    async def save(wid: str, payload: CardEditorSave):
        return editor.edits.save(wid, payload.expected_revision, payload.mode, payload.operation_id)

    @app.post("/api/card-edits/{wid}/runs")
    async def submit(wid: str, payload: CardEditorRequest):
        return editor.submit(wid, payload)

    @app.get("/api/card-edit-runs/{rid}")
    async def get_run(rid: str):
        return editor.edits.run(rid)

    @app.post("/api/card-edit-runs/{rid}/cancel")
    async def cancel(rid: str):
        return await editor.cancel(rid)

    @app.get("/api/card-edit-runs/{rid}/activity")
    async def activity(rid: str, after: int = Query(0, ge=0)):
        editor.edits.run(rid)
        return editor.edits.activity(rid, after)

    @app.get("/api/card-edit-runs/{rid}/events")
    async def events(rid: str, request: Request, after: int = Query(0, ge=0)):
        editor.edits.run(rid)

        async def stream():
            cursor = max(after, int(request.headers.get("last-event-id", "0")))
            while True:
                batch = editor.edits.events(rid, cursor)
                for event in batch:
                    cursor = event["seq"]
                    yield f"id: {cursor}\ndata: {dump(event)}\n\n"
                if len(batch) == 200:
                    if await request.is_disconnected():
                        return
                    continue
                if editor.edits.run(rid)["status"] not in ("queued", "running"):
                    yield "event: end\ndata: {}\n\n"
                    return
                if await request.is_disconnected():
                    return
                await asyncio.sleep(.15)
        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})
