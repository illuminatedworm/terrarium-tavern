"""Desktop dictionary preparation and its reconnectable, application-wide observer."""

import asyncio
import json
from typing import Literal

from fastapi import HTTPException, Request
from fastapi.responses import StreamingResponse


def register_dictionary_routes(app, dictionary):
    @app.get("/api/dictionaries/japanese")
    async def status():
        return dictionary.status()

    @app.get("/api/dictionaries/japanese/events")
    async def events():
        async def stream():
            async for value in dictionary.events():
                yield "data: " + json.dumps(value, ensure_ascii=False) + "\n\n"

        return StreamingResponse(
            stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"}
        )

    @app.post("/api/dictionaries/japanese/download")
    async def download():
        dictionary.start(retry=True)
        return dictionary.status()

    @app.post("/api/dictionaries/japanese/cancel")
    async def cancel():
        await dictionary.cancel()
        return dictionary.status()

    @app.post("/api/dictionaries/japanese/import")
    async def import_dictionary(request: Request, kind: Literal["wheel", "dic"] = "wheel"):
        if request.headers.get("content-type", "").split(";", 1)[0].strip() != "application/octet-stream":
            raise HTTPException(415, "词典导入需要 application/octet-stream 原始文件")
        length = request.headers.get("content-length")
        if length is not None and not length.isdecimal():
            raise ValueError("词典文件长度无效")
        task = dictionary.start(
            retry=True, stream=request.stream(), size=int(length) if length is not None else None, kind=kind
        )
        if task is not None:
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                await dictionary.cancel()
                raise
        if not dictionary.is_ready():
            raise ValueError(dictionary.error or "词典导入已取消")
        return dictionary.status()
