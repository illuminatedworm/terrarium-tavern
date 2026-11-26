"""Revision-checked, resumable context maintenance without story/tool side effects."""

import asyncio
import copy
import json
import time
from typing import Literal

from pydantic import BaseModel, Field

from tavern.conversation.compaction import compact_once, completed_context, rebuild
from tavern.core.cache_policy import fingerprint
from tavern.core.context_usage import context_overflow
from tavern.core.errors import NotFound
from tavern.core.message_adapter import adapter_snapshot, bind_adapter
from tavern.core.provider import make_provider
from tavern.scripting.request_context import response_origin
from tavern.storage.store import Conflict, dump, uid
from tavern.storage.story_cache import story_cache_window
from tavern.story.compaction import rebuild_local
from tavern.story.context import compile_window, load_trace
from tavern.story.state import story_state
from tavern.story.tools import tool_definitions


class ContextOperationRequest(BaseModel):
    operation_id: str = Field(min_length=1, max_length=100)
    expected_revision: int = Field(ge=0)
    kind: Literal["enable_compact", "compact", "enable_local"]


def _settings_identity(settings):
    return fingerprint([settings.model_dump(), adapter_snapshot(settings), response_origin(settings)])


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
            raise NotFound("上下文操作不存在")
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
                """SELECT o.id,o.session,o.operation,o.kind,o.status,o.expected_revision,o.error,o.created,
                    json_extract(o.payload,'$.progress') AS progress,json_extract(o.payload,'$.usage') AS usage,
                    json_extract(o.payload,'$.compacted') AS compacted,json_extract(o.payload,'$.converted') AS converted
                FROM sessions s LEFT JOIN context_operations o ON o.id=(
                    SELECT id FROM context_operations WHERE session=s.id ORDER BY created DESC,rowid DESC LIMIT 1)
                WHERE s.id=?""",
                (sid,),
            ).fetchone()
        if row is None:
            raise NotFound("会话不存在")
        if row["id"] is None:
            return None
        return {
            **dict(row),
            "progress": json.loads(row["progress"] or "{}"),
            "usage": json.loads(row["usage"] or "[]"),
            "compacted": bool(row["compacted"]),
            "converted": bool(row["converted"]),
        }

    def _settings(self, sid):
        return bind_adapter(self.store.session_settings(sid).for_role("story"), self.store, "story", sid)

    def start(self, sid, request):
        session = self.store.session(sid)
        if session["channel_origin"] != "chatgpt":
            raise ValueError("此操作仅适用于 OpenAI 官方渠道的对话")
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
            # Persist the identity before queueing so a restart before the first
            # request can still check the original settings on resume.
            payload = {"head": session["head"], "source_settings": _settings_identity(self._settings(sid))}
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
                    dump(payload),
                    time.time(),
                ),
            )
            db.execute("UPDATE sessions SET opening_locked=1 WHERE id=?", (sid,))
        self.tasks[oid] = asyncio.create_task(self.run(oid))
        return self.get(oid)

    def save(self, sid, oid, payload, *, status="running", error=None, cache=None):
        with self.store.connection() as db:
            db.execute(
                "UPDATE context_operations SET payload=?,status=?,error=? WHERE id=?",
                (dump(payload), status, error, oid),
            )
            if cache is not None:
                window, usage = cache
                self.store.story_cache.record(db, sid, oid, window, usage)
            if status != "running":
                self.store.story_cache.finish(db, sid, oid, status)

    async def run(self, oid):
        op = self.get(oid, private=True)
        payload = op["payload"]
        provider = None
        sid = op["session"]
        try:
            session = self.store.session(sid)
            if session["revision"] != op["expected_revision"] or session["head"] != payload["head"]:
                raise Conflict("历史已改变，不能继续旧的上下文操作；请重新发起")
            source_settings = payload.get("source_settings")
            if source_settings is None:
                raise Conflict("无法核对旧整理记录的原始设置，请重新发起整理")
            settings = self._settings(sid)
            if _settings_identity(settings) != source_settings:
                raise Conflict("整理设置或官方连接与原记录不匹配，请重新发起整理")
            strategy = "new_context" if op["kind"] == "enable_local" else "openai_compact"
            if op["kind"] != "compact" and session["context_strategy"] == strategy:
                self.save(sid, oid, payload, status="completed")
                return
            snapshot = copy.deepcopy(session["snapshot"])
            state = story_state(snapshot)
            window = story_cache_window(snapshot, settings.context_strategy)
            self.store.story_cache.begin(sid, oid, settings, snapshot, resume=oid)
            settings.context_strategy = strategy
            source = {**session, "context_strategy": strategy}
            if op["kind"] != "compact":
                # Both directions derive fresh state from the same durable
                # originals. Old opaque state and handoff are not inputs.
                previous = state.pop("compaction", None)
                # Only whether server compaction had happened carries over: it
                # is the evidence that the originals need a handoff on return.
                payload.setdefault("source_compacted", bool(previous and previous.get("items")))
                state.pop("compact_replay", None)
            document = (snapshot.get("content") or {}).get(
                "document", self.store.session_card(session["id"])["document"]
            )
            tools = tool_definitions(source, settings.story_config, settings=settings)

            compiled = compile_window(
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
            if strategy == "openai_compact":
                compiled = completed_context(compiled)
            self.save(sid, oid, payload)

            def capture_usage(usage):
                usages = [*payload.get("usage", []), usage]
                self.save(sid, oid, {**payload, "usage": usages}, cache=(window, usage))
                payload["usage"] = usages

            class LazyProvider:
                async def chat_complete(_, *args, **kwargs):
                    nonlocal provider
                    if provider is None:
                        provider = (
                            self.provider_factory(settings)
                            if self.provider_factory
                            else await make_provider(settings)
                        )
                        provider.failed_usage_capture = lambda event: capture_usage(event["usage"])
                    response = await provider.chat_complete(*args, **kwargs)
                    capture_usage(response.usage or {})
                    return response

                def __getattr__(_, name):
                    if provider is None:
                        raise AttributeError(name)
                    return getattr(provider, name)

            lazy_provider = LazyProvider()

            async def save_progress(value):
                payload["replay"] = value
                payload["progress"] = {key: value[key] for key in ("batches", "processed", "total")}
                self.save(sid, oid, payload)

            if op["kind"] == "enable_local":
                replay = await rebuild_local(
                    compiled,
                    settings,
                    lazy_provider,
                    session["messages"],
                    progress=payload.get("replay"),
                    save=save_progress,
                    compacted=payload["source_compacted"],
                )
                state["handoff"] = {**replay["handoff"], "revision": state["handoff"]["revision"] + 1}
                state["window"] = {
                    "id": uid(),
                    "number": state["window"]["number"] + 1,
                    "start": replay["history_start"],
                    "replay_history": True,
                }
                payload["converted"] = bool(replay["batches"])
            elif (
                compiled["compact_requires_rebuild"]
                or op["kind"] == "enable_compact"
                or payload.get("replay")
            ):
                # Manual re-compaction can start from the existing checkpoint,
                # but rebuilding from originals is required on identity changes.
                checkpoint, replay = await rebuild(
                    compiled,
                    settings,
                    lazy_provider,
                    progress=payload.get("replay"),
                    save=save_progress,
                    force=op["kind"] == "compact",
                )
                state["compaction"] = checkpoint
                payload["compacted"] = any(i.get("type") == "compaction" for i in checkpoint["items"])
                payload["converted"] = bool(replay["batches"])
            elif op["kind"] == "compact":
                try:
                    checkpoint, _ = await compact_once(compiled, settings, lazy_provider)
                except Exception as error:
                    if not context_overflow(error):
                        raise
                    # The old checkpoint plus plaintext tail may itself be too
                    # large. Replay originals at whole-exchange boundaries,
                    # keeping successful batches resumable just like rebuilds.
                    checkpoint, _ = await rebuild(
                        compiled, settings, lazy_provider, save=save_progress, force=True
                    )
                if checkpoint:
                    state["compaction"] = checkpoint
                payload["compacted"] = bool(
                    checkpoint and any(item.get("type") == "compaction" for item in checkpoint["items"])
                )
                payload["converted"] = True
            with self.store.connection() as db:
                db.execute("BEGIN IMMEDIATE")
                current_settings = self._settings(sid)
                if _settings_identity(current_settings) != source_settings:
                    raise Conflict("整理期间设置或官方连接已改变，请重新发起整理")
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
                self.store.story_cache.finish(
                    db,
                    sid,
                    oid,
                    "committed",
                    snapshot=snapshot,
                    strategy=strategy,
                    keep_segment=op["kind"] == "compact" and not payload["compacted"],
                )
        except asyncio.CancelledError:
            self.save(sid, oid, payload, status="cancelled", error="整理已取消，原上下文保持不变")
        except Exception as error:
            self.save(sid, oid, payload, status="error", error=str(error))
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
