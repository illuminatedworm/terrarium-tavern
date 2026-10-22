"""Global-settings entry points for selective personal data transfer."""

import hashlib
import json
from datetime import datetime

from fastapi import File, Form, UploadFile
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool

from tavern.storage import worldbooks
from tavern.storage.transfer import (
    MAX_UPLOAD,
    ExportSelection,
    export_package,
    import_package,
    preview,
    read_package,
)


def register_transfer_routes(app, store):
    @app.get("/api/data-transfer")
    async def catalog():
        cards = store.cards()
        defaults = worldbooks.cards_defaults(store, [item["id"] for item in cards])
        return {
            "personas": [{"id": item["id"], "name": item["name"]} for item in store.personas()],
            "worldbooks": [{"id": item["id"], "name": item["name"]} for item in worldbooks.summaries(store)],
            # Defaults let the export page show which bindings stay behind.
            "cards": [
                {"id": item["id"], "name": item["name"], "defaults": defaults[item["id"]]} for item in cards
            ],
            "sessions": [
                {
                    "id": item["id"],
                    "name": item["title"] or item["name"] or "对话",
                    "agent_mode": item["agent_mode"],
                }
                for item in store.sessions()
            ],
        }

    @app.post("/api/data-transfer/export")
    async def export(selection: ExportSelection):
        content = await run_in_threadpool(export_package, store, selection)
        filename = f"tavern-data-{datetime.now().strftime('%Y%m%d-%H%M%S')}.zip"
        return Response(
            content,
            media_type="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Cache-Control": "no-store",
            },
        )

    async def read(file):
        content = await file.read(MAX_UPLOAD + 1)
        return content, await run_in_threadpool(read_package, content, store)

    @app.post("/api/data-transfer/preview")
    async def inspect(file: UploadFile = File(...)):
        content, (package, _) = await read(file)
        return {
            **preview(package),
            "sha256": hashlib.sha256(content).hexdigest(),
            "settings_revisions": store.settings_document().revisions,
        }

    @app.post("/api/data-transfer/import")
    async def restore(file: UploadFile = File(...), options: str = Form(...)):
        try:
            config = json.loads(options)
        except json.JSONDecodeError:
            raise ValueError("导入选项无效") from None
        if not isinstance(config, dict) or type(config.get("restore_settings", False)) is not bool:
            raise ValueError("导入选项无效")
        content, (package, blobs) = await read(file)
        if config.get("sha256") != hashlib.sha256(content).hexdigest():
            raise ValueError("数据包已改变，请重新选择并预览")
        return await run_in_threadpool(
            import_package,
            store,
            package,
            blobs,
            config.get("restore_settings", False),
            config.get("settings_revisions"),
        )
