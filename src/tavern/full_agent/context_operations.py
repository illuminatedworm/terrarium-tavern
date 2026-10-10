"""Revision-checked, resumable context maintenance without story/tool side effects."""

import asyncio
import copy
import json
import time
from typing import Literal

from pydantic import BaseModel, Field

from tavern.core.message_adapter import bind_adapter
from tavern.core.models import TurnRequest
from tavern.core.provider import make_provider
from tavern.full_agent.compaction import compact_once, make_handoff, rebuild
from tavern.full_agent.context import compile_window, load_trace
from tavern.full_agent.state import full_state, rollover
from tavern.full_agent.tools import tool_definitions
from tavern.storage.store import Conflict, dump, uid


class ContextOperationRequest(BaseModel):
    operation_id: str = Field(min_length=1, max_length=100)
    expected_revision: int = Field(ge=0)
    kind: Literal["enable_compact", "compact", "enable_local"]


class ContextOperations:
    def __init__(self, store):
        self.store, self.tasks = store, {}
        self.provider_factory = None
        with store.connection() as db:
            db.execute(
                "UPDATE context_operations SET status='interrupted' WHERE status IN ('queued','running')"
            )

    def get(self, operation_id, *, private=False):
        with self.store.connection() as db:
            row = db.execute("SELECT * FROM context_operations WHERE id=?", (operation_id,)).fetchone()
        if row is None:
            raise KeyError("上下文操作不存在")
        value = dict(row)
        payload = json.loads(value.pop("payload"))
        value.update(
            progress=payload.get("progress", {}),
            usage=payload.get("usage", []),
            compacted=payload.get("compacted", False),
        )
        if private:
            value["payload"] = payload
        return value

    def latest(self, sid):
        with self.store.connection() as db:
            row = db.execute(
                "SELECT id FROM context_operations WHERE session=? ORDER BY created DESC,rowid DESC LIMIT 1",
                (sid,),
            ).fetchone()
        return self.get(row[0]) if row else None

    def start(self, sid, request):
        session = self.store.session(sid)
        if session["channel_origin"] != "chatgpt" or session["agent_mode"] != "full_agent":
            raise ValueError("此操作仅适用于 ChatGPT 官方渠道的 Full Agent 对话")
        if request.kind in ("compact", "enable_local") and session["context_strategy"] != "openai_compact":
            raise ValueError("请先切换为云端 compact 模式")
        if not session["head"]:
            raise ValueError("当前对话没有可整理的历史")
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute(
                "SELECT id,kind,expected_revision FROM context_operations WHERE session=? AND operation=?",
                (sid, request.operation_id),
            ).fetchone()
            if previous:
                if (
                    previous["kind"] != request.kind
                    or previous["expected_revision"] != request.expected_revision
                ):
                    raise Conflict("相同操作 ID 不能用于不同上下文请求")
                return self.get(previous["id"])
            if db.execute(
                "SELECT 1 FROM turns WHERE session=? AND status IN ('queued','running','awaiting_review')",
                (sid,),
            ).fetchone():
                raise Conflict("请先完成或取消当前回合")
            if db.execute(
                "SELECT 1 FROM context_operations WHERE session=? AND status IN ('queued','running')", (sid,)
            ).fetchone():
                raise Conflict("此对话已有上下文操作")
            row = db.execute("SELECT revision FROM sessions WHERE id=?", (sid,)).fetchone()
            if row[0] != request.expected_revision:
                raise Conflict("会话已改变，请刷新后重试")
            oid = uid()
            db.execute(
                "INSERT INTO context_operations(id,session,operation,kind,status,expected_revision,payload,created) VALUES (?,?,?,?,?,?,?,?)",
                (
                    oid,
                    sid,
                    request.operation_id,
                    request.kind,
                    "queued",
                    request.expected_revision,
                    dump({"head": session["head"]}),
                    time.time(),
                ),
            )
        self.tasks[oid] = asyncio.create_task(self.run(oid))
        return self.get(oid)

    def save(self, oid, payload, *, status="running", error=None):
        with self.store.connection() as db:
            db.execute(
                "UPDATE context_operations SET payload=?,status=?,error=? WHERE id=?",
                (dump(payload), status, error, oid),
            )

    async def run(self, oid):
        op = self.get(oid, private=True)
        payload = op["payload"]
        prior_usage = copy.deepcopy(payload.get("usage", []))
        provider = None
        try:
            session = self.store.session(op["session"])
            if session["revision"] != op["expected_revision"] or session["head"] != payload["head"]:
                raise Conflict("历史已改变，不能继续旧的上下文操作；请重新发起")
            snapshot = copy.deepcopy(session["snapshot"])
            state = full_state(snapshot)
            settings = bind_adapter(self.store.session_settings(session), self.store, "story", session["id"])
            settings.context_strategy = "openai_compact"
            source = {**session, "context_strategy": "openai_compact"}
            document = (snapshot.get("content") or {}).get(
                "document", self.store.session_card(session["id"])["document"]
            )
            request = TurnRequest(operation_id=op["operation"], expected_revision=session["revision"])
            tools = tool_definitions(
                request, source, settings.full_agent_config, settings=settings, document=document
            )

            def compile_current():
                return compile_window(
                    document,
                    source,
                    session["messages"],
                    snapshot,
                    "",
                    settings,
                    tools,
                    trace=load_trace(self.store, session["id"], session["messages"]),
                )

            compiled = compile_current()
            provider = (
                self.provider_factory(settings) if self.provider_factory else await make_provider(settings)
            )
            self.save(oid, payload)

            async def save_progress(value):
                payload["replay"] = value
                payload["progress"] = {key: value[key] for key in ("batches", "processed", "total")}
                payload["usage"] = prior_usage + copy.deepcopy(
                    getattr(provider, "usage_history", value["usage"])
                )
                self.save(oid, payload)

            if compiled["compact_requires_rebuild"] or op["kind"] == "enable_compact":
                # Manual re-compaction can start from the existing checkpoint,
                # but rebuilding from originals is required on identity changes.
                checkpoint, replay = await rebuild(
                    compiled, settings, provider, progress=payload.get("replay"), save=save_progress
                )
                state["compaction"] = checkpoint
                payload["compacted"] = any(i.get("type") == "compaction" for i in checkpoint["items"])
                payload["usage"] = replay["usage"]
                compiled = compile_current()
            elif op["kind"] == "compact":
                checkpoint, usage = await compact_once(compiled, settings, provider)
                if checkpoint:
                    state["compaction"] = checkpoint
                payload["compacted"] = checkpoint is not None
                payload["usage"] = [usage]
            if op["kind"] == "enable_local":
                text, usage = await make_handoff(compiled, settings, provider)
                rollover(state, session["messages"], text, 2)
                state.pop("compaction", None)
                payload.setdefault("usage", []).append(usage)
            strategy = "new_context" if op["kind"] == "enable_local" else "openai_compact"
            if hasattr(provider, "usage_history"):
                payload["usage"] = prior_usage + copy.deepcopy(provider.usage_history)
            from tavern.scripting.request_context import response_origin

            if response_origin(self.store.session_settings(session)) != response_origin(settings):
                raise Conflict("整理期间官方连接已改变，未替换当前上下文；请重新整理")
            with self.store.connection() as db:
                db.execute("BEGIN IMMEDIATE")
                updated = db.execute(
                    "UPDATE sessions SET context_strategy=?,revision=revision+1 WHERE id=? AND revision=? AND head=?",
                    (strategy, session["id"], op["expected_revision"], payload["head"]),
                )
                if updated.rowcount != 1:
                    raise Conflict("整理期间对话已改变，未覆盖当前状态")
                db.execute(
                    "UPDATE messages SET snapshot=? WHERE session=? AND id=?",
                    (dump(snapshot), session["id"], payload["head"]),
                )
                payload.pop("replay", None)
                db.execute(
                    "UPDATE context_operations SET status='completed',payload=?,error=NULL WHERE id=?",
                    (dump(payload), oid),
                )
        except asyncio.CancelledError:
            if provider is not None and hasattr(provider, "usage_history"):
                payload["usage"] = prior_usage + copy.deepcopy(provider.usage_history)
            self.save(oid, payload, status="cancelled", error="整理已取消，原上下文保持不变")
        except Exception as error:
            if provider is not None and hasattr(provider, "usage_history"):
                payload["usage"] = prior_usage + copy.deepcopy(provider.usage_history)
            self.save(oid, payload, status="error", error=str(error))
        finally:
            if provider is not None:
                await provider.close()
            self.tasks.pop(oid, None)

    async def cancel(self, oid):
        if oid in self.tasks:
            self.tasks[oid].cancel()
            await asyncio.gather(self.tasks[oid], return_exceptions=True)
        return self.get(oid)

    def resume(self, oid):
        op = self.get(oid)
        if op["status"] not in ("error", "cancelled", "interrupted"):
            raise Conflict("此操作不可恢复")
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute(
                "SELECT 1 FROM turns WHERE session=? AND status IN ('queued','running','awaiting_review')",
                (op["session"],),
            ).fetchone():
                raise Conflict("请先完成或取消当前回合")
            if db.execute(
                "SELECT 1 FROM context_operations WHERE session=? AND status IN ('queued','running')",
                (op["session"],),
            ).fetchone():
                raise Conflict("此对话已有上下文操作")
            db.execute("UPDATE context_operations SET status='queued',error=NULL WHERE id=?", (oid,))
        self.tasks[oid] = asyncio.create_task(self.run(oid))
        return self.get(oid)

    async def close(self):
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
