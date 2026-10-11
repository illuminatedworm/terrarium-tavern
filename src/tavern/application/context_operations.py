"""Revision-checked, resumable context maintenance without story/tool side effects."""

import asyncio
import copy
import json
import time
from typing import Literal

from pydantic import BaseModel, Field

from tavern.conversation.compaction import compact_once, rebuild
from tavern.core.cache_policy import fingerprint
from tavern.core.message_adapter import adapter_snapshot, bind_adapter
from tavern.core.models import TurnRequest
from tavern.core.provider import make_provider
from tavern.full_agent.compaction import rebuild_local
from tavern.full_agent.context import compile_window, load_trace
from tavern.full_agent.state import full_state
from tavern.full_agent.tools import tool_definitions
from tavern.storage.store import Conflict, dump, uid


class ContextOperationRequest(BaseModel):
    operation_id: str = Field(min_length=1, max_length=100)
    expected_revision: int = Field(ge=0)
    kind: Literal["enable_compact", "compact", "enable_local"]


class ContextOperations:
    def __init__(self, store, retrieval=None):
        self.store, self.tasks = store, {}
        self.retrieval = retrieval
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
            converted=payload.get("converted", False),
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
        if session["channel_origin"] != "chatgpt":
            raise ValueError("此操作仅适用于 ChatGPT 官方渠道的对话")
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
            if request.kind == "compact" and session["context_strategy"] != "openai_compact":
                raise ValueError("请先切换为云端 compact 模式")
            if not session["head"]:
                raise ValueError("当前对话没有可整理的历史")
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
            strategy = "new_context" if op["kind"] == "enable_local" else "openai_compact"
            if op["kind"] != "compact" and session["context_strategy"] == strategy:
                self.save(oid, payload, status="completed")
                return
            snapshot = copy.deepcopy(session["snapshot"])
            half = session["agent_mode"] == "half_agent"
            from tavern.conversation.half_compaction import STATE, install
            state = snapshot.setdefault(STATE, {}) if half else full_state(snapshot)
            role = "foreground" if half else "full_agent"
            settings = bind_adapter(self.store.session_settings(session).for_role(role), self.store, "story", session["id"])
            from tavern.scripting.request_context import response_origin
            def identity(config):
                return fingerprint([config.model_dump(), adapter_snapshot(config), response_origin(config),
                                    response_origin(config.for_role("background")) if half else None])
            source_settings = identity(settings)
            if payload.get("source_settings", source_settings) != source_settings:
                raise Conflict("整理设置或官方连接已改变，不能继续旧操作；请重新发起")
            payload["source_settings"] = source_settings
            settings.context_strategy = strategy
            source = {**session, "context_strategy": strategy}
            if op["kind"] != "compact":
                # Both directions derive fresh state from the same durable
                # originals. Old opaque state and handoff are not inputs.
                state.pop("compaction", None)
                state.pop("compact_replay", None)
            document = (snapshot.get("content") or {}).get(
                "document", self.store.session_card(session["id"])["document"]
            )
            request = TurnRequest(operation_id=op["operation"], expected_revision=session["revision"])
            tools = [] if half else tool_definitions(
                request, source, settings.full_agent_config, settings=settings, document=document
            )

            def compile_current():
                if half:
                    from tavern.conversation.half_context_operations import compile_sources
                    return compile_sources(self.store, source, snapshot, document, settings)
                return compile_window(
                    document,
                    source,
                    session["messages"],
                    snapshot,
                    "",
                    settings,
                    tools,
                    trace=load_trace(self.store, session["id"], session["messages"]),
                    replay_all=op["kind"] != "compact",
                )

            compiled = compile_current()
            if half and strategy == "openai_compact":
                from tavern.conversation.half_compaction import completed_context
                compiled = completed_context(compiled)
            self.save(oid, payload)

            class LazyProvider:
                async def chat_complete(_, *args, **kwargs):
                    nonlocal provider
                    if provider is None:
                        provider = (self.provider_factory(settings) if self.provider_factory
                                    else await make_provider(settings))
                    return await provider.chat_complete(*args, **kwargs)

                def __getattr__(_, name):
                    if provider is None:
                        raise AttributeError(name)
                    return getattr(provider, name)

            lazy_provider = LazyProvider()

            async def save_progress(value):
                payload["replay"] = value
                payload["progress"] = {key: value[key] for key in ("batches", "processed", "total")}
                payload["usage"] = prior_usage + copy.deepcopy(
                    getattr(provider, "usage_history", value["usage"])
                )
                self.save(oid, payload)

            if op["kind"] == "enable_local" and half:
                from tavern.conversation.half_context_operations import rebuild_local as rebuild_half_local
                async def save_local(value):
                    payload["replay"] = value
                    payload["progress"] = {key: value[key] for key in ("batches", "processed", "total")}
                    payload["usage"] = copy.deepcopy(value["usage"])
                    self.save(oid, payload)
                snapshot, replay = await rebuild_half_local(
                    self.store, oid, source, snapshot, document, settings,
                    provider_factory=self.provider_factory, progress=payload.get("replay"),
                    save=save_local, retrieval=self.retrieval)
                payload["usage"] = replay["usage"]
                payload["converted"] = bool(replay["batches"])
            elif op["kind"] == "enable_local":
                replay = await rebuild_local(compiled, settings, lazy_provider,
                                             progress=payload.get("replay"), save=save_progress)
                state["handoff"] = {"text": replay["handoff"], "revision": state["handoff"]["revision"] + 1,
                                    "stale": False}
                state["window"] = {"id": uid(), "number": state["window"]["number"] + 1,
                                   "start": replay["history_start"], "replay_history": True}
                payload["usage"] = replay["usage"]
                payload["converted"] = bool(replay["batches"])
            elif (compiled["compact_requires_rebuild"] or op["kind"] == "enable_compact"
                  or half and not compiled["compaction_opaque"]):
                # Manual re-compaction can start from the existing checkpoint,
                # but rebuilding from originals is required on identity changes.
                checkpoint, replay = await rebuild(
                    compiled, settings, lazy_provider, progress=payload.get("replay"), save=save_progress,
                    force=op["kind"] == "compact" or half and compiled["estimated_tokens"] > settings.context_tokens,
                )
                state["compaction"] = checkpoint
                if half:
                    install(snapshot, checkpoint, compiled, session["messages"])
                payload["compacted"] = any(i.get("type") == "compaction" for i in checkpoint["items"])
                payload["usage"] = replay["usage"]
                payload["converted"] = bool(replay["batches"])
                if half and compiled["_replay_complete"] and compiled["estimated_tokens"] > settings.context_tokens and not payload["compacted"]:
                    raise ValueError("历史超过窗口上限，但服务端未生成压缩状态；原上下文保持不变")
            elif op["kind"] == "compact":
                checkpoint, usage = (await compact_once(compiled, settings, lazy_provider)
                                     if not half or compiled["_replay_complete"] else (None, {}))
                if checkpoint:
                    state["compaction"] = checkpoint
                    if half:
                        install(snapshot, checkpoint, compiled, session["messages"])
                payload["compacted"] = checkpoint is not None
                payload["usage"] = [usage]
                payload["converted"] = True
            if hasattr(provider, "usage_history"):
                payload["usage"] = prior_usage + copy.deepcopy(provider.usage_history)
            if response_origin(self.store.session_settings(session).for_role(role)) != response_origin(settings):
                raise Conflict("整理期间官方连接已改变，未替换当前上下文；请重新整理")
            current_settings = bind_adapter(self.store.session_settings(session).for_role(role), self.store, "story", session["id"])
            if identity(current_settings) != source_settings:
                raise Conflict("整理期间上下文设置已改变，未替换当前上下文；请重新整理")
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
