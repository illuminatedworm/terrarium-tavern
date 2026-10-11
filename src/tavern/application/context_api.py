"""Shared, revision-checked context controls for the two story modes."""
from typing import Literal

from tavern.application.context_operations import ContextOperationRequest


def register_context_routes(app, store, context_operations):
    @app.get("/api/sessions/{sid}/context-operation")
    def context_operation(sid: str):
        store.session(sid)
        return context_operations.latest(sid)

    @app.post("/api/sessions/{sid}/context-operations", status_code=202)
    async def start_context_operation(sid: str, request: ContextOperationRequest):
        return context_operations.start(sid, request)

    @app.post("/api/sessions/{sid}/context-operations/{oid}/{action}")
    async def change_context_operation(sid: str, oid: str, action: Literal["cancel", "resume"]):
        if context_operations.get(oid)["session"] != sid:
            raise ValueError("上下文操作不属于此对话")
        return await context_operations.cancel(oid) if action == "cancel" else context_operations.resume(oid)

    @app.put("/api/sessions/{sid}/compact-auto")
    def compact_auto(sid: str, edit: dict):
        session = store.session(sid)
        if session["channel_origin"] != "chatgpt" or type(edit.get("enabled")) is not bool:
            raise ValueError("自动压缩仅适用于官方渠道，请提供有效开关")
        return store.update_session(sid, edit.get("expected_revision"), compact_auto=int(edit["enabled"]))
