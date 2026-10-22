"""Worldbook library and shelf-card defaults for new conversations."""

from urllib.parse import quote

from fastapi.responses import Response

from tavern.character.cards import export_lorebook
from tavern.core.models import CardDefaultsEdit, WorldbookEdit
from tavern.storage import worldbooks


def register_worldbook_routes(app, store):
    @app.get("/api/worldbooks")
    async def library():
        return worldbooks.summaries(store)

    @app.post("/api/worldbooks", status_code=201)
    async def create(edit: WorldbookEdit):
        return worldbooks.save(store, None, edit)

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
