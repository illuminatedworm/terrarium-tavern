"""Worldbook library and shelf-card defaults for new conversations."""

import json
from urllib.parse import quote

from fastapi import File, UploadFile
from fastapi.responses import Response

from tavern.character.cards import MAX_BYTES, export_lorebook
from tavern.character.lore_purpose import upgrade_lore_purposes
from tavern.character.worldbooks import validate_book
from tavern.core.models import CardDefaultsEdit, WorldbookEdit
from tavern.storage import worldbooks


def register_worldbook_routes(app, store):
    @app.get("/api/worldbooks")
    async def library():
        return worldbooks.summaries(store)

    @app.post("/api/worldbooks", status_code=201)
    async def create(edit: WorldbookEdit):
        return worldbooks.save(store, None, edit)

    @app.post("/api/worldbooks/prepare-import")
    async def prepare_import(file: UploadFile = File(...)):
        data = await file.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise ValueError("世界书超过 32 MB")
        book = json.loads(data)
        upgrade_lore_purposes(book)
        validate_book("导入的世界书", book)
        return book

    @app.get("/api/worldbooks/{wid}")
    async def read(wid: str):
        return worldbooks.worldbook(store, wid)

    @app.put("/api/worldbooks/{wid}")
    async def update(wid: str, edit: WorldbookEdit):
        return worldbooks.save(store, wid, edit)

    @app.delete("/api/worldbooks/{wid}")
    async def delete(wid: str):
        worldbooks.delete(store, wid)
        return {"deleted": wid}

    @app.get("/api/worldbooks/{wid}/export")
    async def export(wid: str):
        row = worldbooks.worldbook(store, wid)
        return Response(
            export_lorebook(row["book"]),
            media_type="application/json",
            headers={
                "Content-Disposition": 'attachment; filename="worldbook.json"; '
                f"filename*=UTF-8''{quote(row['name'] + '.json')}"
            },
        )

    @app.get("/api/cards/{cid}/defaults")
    async def card_defaults(cid: str):
        return worldbooks.card_defaults(store, cid)

    @app.put("/api/cards/{cid}/defaults")
    async def save_card_defaults(cid: str, edit: CardDefaultsEdit):
        return worldbooks.save_defaults(store, cid, edit)
