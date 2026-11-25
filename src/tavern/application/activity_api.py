"""Share an HTTP connection for app-wide activity, leaving room for story reads."""

import asyncio
from collections.abc import AsyncGenerator
from contextlib import aclosing

from anyio import CancelScope
from fastapi import Query, Request
from fastapi.responses import StreamingResponse

from tavern.application.dictionary_api import dictionary_event_stream
from tavern.novel.api import novel_event_stream


async def activity_events(novel: AsyncGenerator[str, None], dictionary: AsyncGenerator[str, None]):
    # Keep one pending read per producer so an idle topic cannot block the other.
    async with aclosing(novel), aclosing(dictionary):
        pending = {asyncio.create_task(anext(source, None)): source for source in (novel, dictionary)}
        try:
            while pending:
                ready, _ = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                for read in ready:
                    source = pending.pop(read)
                    if (frame := read.result()) is not None:
                        yield frame
                        pending[asyncio.create_task(anext(source, None))] = source
        finally:
            for read in pending:
                read.cancel()
            # Disconnect cancels the ASGI stream. Finish the reads before closing their generators.
            with CancelScope(shield=True):
                await asyncio.gather(*pending, return_exceptions=True)


def register_activity_routes(app, service):
    @app.get("/api/activity/events")
    async def events(request: Request, after: int = Query(0, ge=0)):
        # Only investigation events set an SSE id. Dictionary updates send a fresh snapshot on reconnect.
        cursor = max(after, int(request.headers.get("last-event-id", "0")))
        return StreamingResponse(
            activity_events(
                novel_event_stream(service.story.novel, cursor),
                dictionary_event_stream(service.japanese_dictionary, named=True),
            ),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache"},
        )
