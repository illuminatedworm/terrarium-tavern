"""Image-only bridge for opaque card frames; no general URL fetching capability."""

import asyncio
import base64
import json
from contextlib import aclosing

import httpx
from fastapi import HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field


class ImageRequest(BaseModel):
    url: str = Field(min_length=1, max_length=8192)
    message_id: str


class MarkupRequest(BaseModel):
    html: str = Field(max_length=16 * 1024 * 1024)
    message_id: str


def register_image_routes(app, store, dependencies):
    async def connected(request, work):
        async def disconnect():
            while (await request.receive())["type"] != "http.disconnect":
                pass  # Await ASGI events, never poll or hold a timed background loop.

        operation = asyncio.create_task(work)
        gone = asyncio.create_task(disconnect())
        try:
            await asyncio.wait({operation, gone}, return_when=asyncio.FIRST_COMPLETED)
            if operation.done():
                return operation.result()
            gone.result()
            return None
        finally:
            operation.cancel()
            gone.cancel()
            await asyncio.gather(operation, gone, return_exceptions=True)

    def source_card(sid, message_id):
        # Each image request needs only branch membership and that message's
        # revision; loading the full session view per image made galleries slow.
        on_branch, revised = store.branch_message_revision(sid, message_id)
        if not on_branch:
            raise HTTPException(404, "图片所属消息不在当前分支")
        card = store.session_card(sid)
        return {**card, "document": card["document"] if revised is None else revised}

    @app.post("/api/sessions/{sid}/images")
    async def image(sid: str, payload: ImageRequest, request: Request):
        try:
            result = await connected(request, dependencies.image(source_card(sid, payload.message_id), payload.url))
        except httpx.HTTPError as error:
            raise HTTPException(502, "图片加载失败：" + str(error)) from error
        if result is None:
            return Response(status_code=499)
        raw, mime = result
        return {"url": f'data:{mime};base64,{base64.b64encode(raw).decode()}'}

    @app.post("/api/sessions/{sid}/images/markup")
    async def markup(sid: str, payload: MarkupRequest, request: Request):
        try:
            html = await connected(request, dependencies.prepare_markup(source_card(sid, payload.message_id), payload.html))
        except httpx.HTTPError as error:
            raise HTTPException(502, "图片加载失败：" + str(error)) from error
        if html is None:
            return Response(status_code=499)
        return {"html": html}

    @app.post("/api/sessions/{sid}/images/stream")
    async def stream(sid: str, payload: MarkupRequest):
        """NDJSON: first the locally prepared HTML and its remote image URLs, then one
        line per image as it finishes, so prose shows images progressively."""
        card = source_card(sid, payload.message_id)
        html = dependencies.markup(card, payload.html)
        urls = dependencies.remote_images(html)

        def line(value):
            return json.dumps(value, ensure_ascii=False) + "\n"

        async def lines():
            yield line({"html": html, "urls": urls})
            async with aclosing(dependencies.stream_images(card, urls)) as images:
                async for url, data, error in images:
                    if error is None:
                        yield line({"url": url, "data": data})
                    else:
                        if isinstance(error, httpx.HTTPStatusError):
                            reason = f"HTTP {error.response.status_code} {url}"
                        elif isinstance(error, httpx.HTTPError):
                            reason = f"{type(error).__name__} {url}"
                        else:
                            reason = str(error)
                        yield line({"url": url, "error": reason})

        return StreamingResponse(lines(), media_type="application/x-ndjson")
