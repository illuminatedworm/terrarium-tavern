"""Revision-checked adapter changes can be saved while a model call is running."""
from pydantic import BaseModel, ConfigDict, Field

from tavern.core.message_adapter import catalog
from tavern.storage.message_adapters import recipe_types


class AdapterUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_adapter_revision: int = Field(ge=0)
    config: dict


def register_message_adapter_routes(app, store):
    @app.get("/api/message-adapter/types")
    async def adapter_types(lane: str = "foreground"):
        if lane not in ("foreground", "background", "full_agent", "card_editor", "advisor"):
            raise ValueError("未知模型调用链")
        recipe = recipe_types(store.settings_document().half_agent.context_recipe.model_dump())
        extra = [b["message_type"] for b in recipe["blocks"] if b.get("message_type")] if lane == "foreground" else []
        return catalog(lane, extra)

    @app.get("/api/sessions/{sid}/message-adapter")
    async def story_adapter(sid: str):
        return store.message_adapter_settings("story", sid)

    @app.put("/api/sessions/{sid}/message-adapter")
    async def save_story_adapter(sid: str, update: AdapterUpdate):
        return store.save_message_adapter("story", sid, update.expected_adapter_revision, update.config)

    @app.get("/api/card-editor/conversations/{wid}/message-adapter")
    async def editor_adapter(wid: str):
        return store.message_adapter_settings("editor", wid)

    @app.put("/api/card-editor/conversations/{wid}/message-adapter")
    async def save_editor_adapter(wid: str, update: AdapterUpdate):
        return store.save_message_adapter("editor", wid, update.expected_adapter_revision, update.config)
