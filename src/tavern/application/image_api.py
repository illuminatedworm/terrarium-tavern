"""Image-only bridge for opaque card frames; no general URL fetching capability."""

import asyncio
import base64

import httpx
from fastapi import HTTPException, Request, Response
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
        session = store.session(sid)
        message = next((m for m in session["messages"] if m["id"] == message_id), None)
        if message is None:
            raise HTTPException(404, "图片所属消息不在当前分支")
        card = store.session_card(sid)
        document = (message["snapshot"].get("content") or {}).get("document", card["document"])
        return {**card, "document": document}

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
