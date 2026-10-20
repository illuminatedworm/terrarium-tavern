"""Persist composer choices under the conversation's immutable channel."""

import json

from pydantic import BaseModel, ConfigDict, Field

from tavern.core.channels import Channel
from tavern.core.errors import NotFound
from tavern.core.model_selection import ModelSelection, select_runtime
from tavern.storage.store import Conflict, dump


class SelectionEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=0)
    selection: ModelSelection


class ChannelEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=0)
    channel_origin: Channel


def register_model_selection_routes(app, store, editor):
    @app.put("/api/sessions/{sid}/model")
    def select_story(sid: str, edit: SelectionEdit):
        with store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
            if row is None:
                raise NotFound("对话不存在")
            if row["revision"] != edit.expected_revision:
                raise Conflict("对话已改变，请重新载入")
            if db.execute(
                "SELECT 1 FROM turns WHERE session=? AND status IN ('queued','running','awaiting_review')",
                (sid,),
            ).fetchone():
                raise Conflict("请先完成或取消当前回合")
            if db.execute(
                "SELECT 1 FROM context_operations WHERE session=? AND status IN ('queued','running')", (sid,)
            ).fetchone():
                raise Conflict("请先完成或取消上下文整理")
            role = "full_agent" if row["agent_mode"] == "full_agent" else "foreground"
            settings = store.settings(row["agent_mode"], row["channel_origin"])
            selection = edit.selection.remember(json.loads(row["model_selection"]), getattr(settings, role))
            select_runtime(settings, role, selection.model_dump(), row["channel_origin"])
            # Connection preferences do not change story revision: resumable work
            # still references the same story, with its complete original context.
            db.execute(
                "UPDATE sessions SET model_selection=? WHERE id=?",
                (selection.model_dump_json(exclude_defaults=True), sid),
            )
        return store.session(sid)

    @app.put("/api/card-author/conversations/{wid}/model")
    def select_editor(wid: str, edit: SelectionEdit):
        with store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            workspace = editor.edits.check(db, wid, edit.expected_revision, idle=False)
            if db.execute(
                "SELECT 1 FROM card_edit_runs WHERE workspace=? AND status IN ('queued','running')", (wid,)
            ).fetchone():
                raise Conflict("请先等待或停止当前编辑任务")
            settings = store.settings()
            selection = edit.selection.remember(workspace["model_selection"], settings.card_author)
            select_runtime(settings, "card_author", selection.model_dump(), workspace["channel_origin"])
            db.execute(
                "UPDATE card_author_conversations SET model_selection=? WHERE id=?",
                (selection.model_dump_json(exclude_defaults=True), wid),
            )
        return editor.edits.conversation(wid)

    @app.put("/api/card-author/conversations/{wid}/channel-origin")
    def classify_editor(wid: str, edit: ChannelEdit):
        with store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            workspace = editor.edits.check(db, wid, edit.expected_revision, idle=False)
            if workspace["channel_origin"] is not None:
                raise Conflict("渠道归属已经确定，不能跨渠道修改")
            if db.execute(
                "SELECT 1 FROM card_edit_runs WHERE workspace=? AND status IN ('queued','running')", (wid,)
            ).fetchone():
                raise Conflict("请先等待或停止当前编辑任务")
            db.execute(
                "UPDATE card_author_conversations SET channel_origin=?,model_selection=? WHERE id=?",
                (edit.channel_origin, dump({}), wid),
            )
        return editor.edits.conversation(wid)
