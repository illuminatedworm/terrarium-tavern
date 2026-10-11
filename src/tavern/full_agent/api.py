"""User-visible handoff/notes and explicit recovery from a full window."""
import copy
from typing import Literal

from fastapi import Query
from pydantic import BaseModel, Field

from tavern.core.models import TurnRequest
from tavern.core.tokens import count_text
from tavern.full_agent.activity import compact, message_items, part_page, turn_items
from tavern.full_agent.state import full_state, note_view, rollover
from tavern.full_agent.tools import RESULT_FIELDS, SPECS, Handoff, result_field, tool_definitions
from tavern.storage.store import Conflict, dump


class ContextEdit(Handoff):
    expected_revision: int = Field(ge=0)
    new_window: bool = False
    keep_recent: int = Field(default=0, ge=0, le=12)


class ActivityQuery(BaseModel):
    message_ids: list[str] = Field(min_length=1, max_length=100)


class ToolSettings(BaseModel):
    expected_revision: int = Field(ge=0)
    disabled_tools: list[str] = Field(max_length=len(SPECS))
    tool_result_options: dict[str, dict[str, bool]] | None = None



def require_full(store, sid):
    session = store.session(sid)
    if session["agent_mode"] != "full_agent":
        raise ValueError("此功能仅适用于 Full Agent 会话")
    return session


def register_full_agent_routes(app, store, context_operations=None):
    @app.put("/api/sessions/{sid}/channel-origin")
    def classify_legacy(sid: str, edit: dict):
        from tavern.core.channels import CHANNELS
        if edit.get("channel_origin") not in CHANNELS:
            raise ValueError("未知渠道")
        with store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM turns WHERE session=? AND status IN ('queued','running','awaiting_review')", (sid,)).fetchone():
                raise Conflict("请先完成或取消当前回合")
            result = db.execute("UPDATE sessions SET channel_origin=?,revision=revision+1 WHERE id=? AND revision=? AND channel_origin IS NULL",
                                (edit["channel_origin"], sid, edit.get("expected_revision")))
            if result.rowcount != 1:
                raise Conflict("渠道归属已经确定或会话已改变，不能跨渠道修改")
        return store.session(sid)
    @app.get("/api/sessions/{sid}/full-agent/tools")
    def list_tools(sid: str, update_variables: bool = False, allow_content_edit: bool = False):
        session = require_full(store, sid)
        settings = store.session_settings(session).for_role("full_agent")
        request = TurnRequest(operation_id="tools", expected_revision=session["revision"],
                              update_variables=update_variables, allow_content_edit=allow_content_edit)
        available = {t["function"]["name"] for t in tool_definitions(
            request, session, settings.full_agent_config, settings=settings)}
        # Keep the model's variable tool schema stable for prefix caching; the
        # execution gate and this UI availability follow only the turn request.
        if not update_variables:
            available.discard("patch_state")
        document = (session["snapshot"].get("content") or {}).get("document", store.session_card(sid)["document"])
        definitions = tool_definitions(request, session, settings.full_agent_config, settings=settings,
                                       catalog=True, document=document)
        edit_capabilities = {"character": "revise_content" in available and "read_character" in available,
                             "lore_update_remove": "revise_content" in available and "read_lore_entry" in available,
                             "lore_add": "revise_content" in available}
        return {"tools": [{**t["function"], "enabled": t["function"]["name"] not in session["disabled_tools"],
                           "available": t["function"]["name"] in available,
                           **({"edit_capabilities": edit_capabilities} if t["function"]["name"] == "revise_content" else {}),
                           "result_options": {key: result_field(session, t["function"]["name"], key)
                                              for key in RESULT_FIELDS.get(t["function"]["name"], {})}}
                          for t in definitions]}

    @app.put("/api/sessions/{sid}/full-agent/tools")
    def save_tools(sid: str, edit: ToolSettings):
        require_full(store, sid)
        if set(edit.disabled_tools) - set(SPECS):
            raise ValueError("未知的 Full Agent 工具")
        session = require_full(store, sid)
        options = session["tool_result_options"] if edit.tool_result_options is None else edit.tool_result_options
        options = dict(options)
        if any(name not in RESULT_FIELDS or set(fields) - set(RESULT_FIELDS[name])
               for name, fields in options.items()):
            raise ValueError("未知的工具返回选项")
        with store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM context_operations WHERE session=? AND status IN ('queued','running')", (sid,)).fetchone():
                raise Conflict("请先完成或取消上下文整理")
            if db.execute("SELECT 1 FROM turns WHERE session=? AND status IN ('queued','running','awaiting_review')",
                          (sid,)).fetchone():
                raise Conflict("请先完成或取消当前回合")
            result = db.execute("UPDATE sessions SET disabled_tools=?,tool_result_options=?,revision=revision+1 "
                                "WHERE id=? AND revision=?",
                                (dump(sorted(set(edit.disabled_tools))), dump(options), sid, edit.expected_revision))
            if result.rowcount != 1:
                raise Conflict("会话已改变，请刷新后重试")
        return store.session(sid)

    @app.post("/api/sessions/{sid}/full-agent/activity")
    def dialogue_activity(sid: str, query: ActivityQuery):
        groups = message_items(store, require_full(store, sid), query.message_ids)
        return {"messages": {mid: compact(items) for mid, items in groups.items()}}

    @app.get("/api/sessions/{sid}/full-agent/messages/{mid}/activity/{activity_id}")
    def dialogue_activity_part(sid: str, mid: str, activity_id: str,
                               part: Literal["arguments", "result", "content"], offset: int = Query(0, ge=0)):
        items = message_items(store, require_full(store, sid), [mid])[mid]
        return part_page(items, activity_id, part, offset)

    @app.get("/api/sessions/{sid}/full-agent/turns/{tid}/activity")
    def pending_activity(sid: str, tid: str):
        require_full(store, sid)
        return {"items": compact(turn_items(store, sid, tid))}

    @app.get("/api/sessions/{sid}/full-agent/turns/{tid}/activity/{activity_id}")
    def pending_activity_part(sid: str, tid: str, activity_id: str,
                              part: Literal["arguments", "result", "content"], offset: int = Query(0, ge=0)):
        require_full(store, sid)
        return part_page(turn_items(store, sid, tid), activity_id, part, offset)

    @app.get("/api/sessions/{sid}/full-agent")
    def full_agent_state(sid: str):
        session = require_full(store, sid)
        state = full_state(session["snapshot"])
        settings = store.session_settings(session, require_origin=False)
        return {"window": {k: v for k, v in state["window"].items() if k != "id"},
                "pressure_ratio": settings.full_agent_config.pressure_ratio,
                "context_strategy": session["context_strategy"], "channel_origin": session["channel_origin"],
                "compact_auto": session["compact_auto"], "context_operation": context_operations.latest(sid),
                "handoff": state["handoff"],
                "notes": [note_view(n, session["messages"]) for n in state["notes"].values()],
                "history_messages": len(session["messages"])}

    @app.put("/api/sessions/{sid}/full-agent/context")
    def edit_context(sid: str, edit: ContextEdit):
        session = require_full(store, sid)
        if not session["head"]:
            raise ValueError("空白故事还没有可保存交接笔记的剧情节点；请先发送一条消息")
        settings = store.session_settings(session).for_role("full_agent")
        if session["context_strategy"] == "openai_compact":
            raise ValueError("云端 compact 模式不使用 handoff；请先切回本地换窗")
        if not edit.content.strip() or count_text(edit.content, settings) > settings.full_agent_config.handoff_tokens:
            raise ValueError("handoff 不能为空且不能超过配置的 token 上限")
        snapshot = copy.deepcopy(session["snapshot"])
        state = full_state(snapshot)
        if edit.new_window:
            rollover(state, session["messages"], edit.content, edit.keep_recent)
        else:
            state["handoff"] = {"text": edit.content, "revision": state["handoff"]["revision"] + 1,
                                   "stale": False}
        state.pop("edited_sources", None)
        with store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM context_operations WHERE session=? AND status IN ('queued','running')", (sid,)).fetchone():
                raise Conflict("请先完成或取消上下文整理")
            if db.execute("SELECT 1 FROM turns WHERE session=? AND status IN ('queued','running','awaiting_review')",
                          (sid,)).fetchone():
                raise Conflict("请先完成或取消当前回合")
            result = db.execute("UPDATE sessions SET revision=revision+1 WHERE id=? AND revision=?",
                                (sid, edit.expected_revision))
            if result.rowcount != 1 or edit.expected_revision != session["revision"]:
                raise Conflict("会话已改变，请刷新后重试")
            db.execute("UPDATE messages SET snapshot=? WHERE id=? AND session=?",
                       (dump(snapshot), session["head"], sid))
        return store.session(sid)
