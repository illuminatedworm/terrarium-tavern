"""User-visible checkpoint/notes and explicit recovery from a full window."""
import copy
from typing import Literal

from fastapi import Query
from pydantic import BaseModel, Field

from tavern.core.tokens import count_text
from tavern.full_agent.activity import compact, message_items, part_page, turn_items
from tavern.full_agent.state import full_state, note_view, rollover
from tavern.full_agent.tools import Checkpoint
from tavern.storage.store import Conflict, dump


class ContextEdit(Checkpoint):
    expected_revision: int = Field(ge=0)
    new_window: bool = False
    keep_recent: int = Field(default=0, ge=0, le=12)


class ActivityQuery(BaseModel):
    message_ids: list[str] = Field(min_length=1, max_length=100)


def require_full(store, sid):
    session = store.session(sid)
    if session["agent_mode"] != "full_agent":
        raise ValueError("此功能仅适用于 Full Agent 会话")
    return session


def register_full_agent_routes(app, store):
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
        return {"window": {k: v for k, v in state["window"].items() if k != "id"},
                "checkpoint": state["checkpoint"],
                "notes": [note_view(n, session["messages"]) for n in state["notes"].values()],
                "history_messages": len(session["messages"])}

    @app.put("/api/sessions/{sid}/full-agent/context")
    def edit_context(sid: str, edit: ContextEdit):
        session = require_full(store, sid)
        if not session["head"]:
            raise ValueError("空白故事还没有可保存交接笔记的剧情节点；请先发送一条消息")
        settings = store.settings().for_role("full_agent")
        if not edit.content.strip() or count_text(edit.content, settings) > settings.full_agent_config.checkpoint_tokens:
            raise ValueError("checkpoint 不能为空且不能超过配置的 token 上限")
        snapshot = copy.deepcopy(session["snapshot"])
        state = full_state(snapshot)
        if edit.new_window:
            rollover(state, session["messages"], edit.content, edit.keep_recent)
        else:
            state["checkpoint"] = {"text": edit.content, "revision": state["checkpoint"]["revision"] + 1,
                                   "stale": False}
        state.pop("edited_sources", None)
        with store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
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
