"""Durable consultation loop; only an explicit preference edit can touch a story."""
import asyncio
import copy
import json
import time
from typing import Literal
from uuid import NAMESPACE_URL, uuid5

from pydantic import BaseModel, ConfigDict, Field

from tavern.advisor.context import (
    compile_context,
    consultation_body,
    context_identity,
    freeze_story,
    runtime_config,
    sources,
)
from tavern.advisor.storage import AdvisorStore
from tavern.advisor.tools import AdvisorTools, definitions
from tavern.core.message_adapter import provider_messages
from tavern.core.model_selection import ModelSelection
from tavern.core.model_view import HANDLE, ModelView
from tavern.core.provider import make_provider, require_model_connection
from tavern.core.tokens import request_tokens
from tavern.scripting.native_response import message_digest
from tavern.scripting.request_context import ProviderRequestContext, configure_operation, response_origin
from tavern.storage.store import Conflict, dump, uid
from tavern.web.client import WebTools
from tavern.web.cloud import WebSession


class ConsultationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = ""
    expected_revision: int = Field(ge=0)
    operation_id: str = Field(min_length=1, max_length=128)
    resume_run_id: str | None = None
    compact: bool = False


class AdvisorRevisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=0)


class OptionsRequest(AdvisorRevisionRequest):
    model_selection: ModelSelection | None = None
    recent_exchanges: int | None = Field(default=None, ge=0, le=1000)
    context_strategy: Literal["local", "openai_compact"] | None = None


class AdvisorService:
    def __init__(self, store, retrieval):
        self.store, self.data, self.retrieval = store, AdvisorStore(store), retrieval
        self.tasks, self.provider_factory = {}, None
        self.web_transport = None

    def public(self, sid):
        state = self.data.get(sid)
        session = self.store.session(sid)
        config = self.store.settings_document().shared.advisor
        profile = config.official_model if session["channel_origin"] == "chatgpt" else config.model
        current_sources = sources(freeze_story(self.store, session))
        state["effective"] = {"recent_exchanges": state["options"].get("recent_exchanges") if state["options"].get("recent_exchanges") is not None else config.recent_exchanges,
                              "range_source": "story" if state["options"].get("recent_exchanges") is not None else "global",
                              "model_selection": state["options"].get("model_selection") or ({"provider_id": profile.provider_id, "model": profile.model} if profile else None),
                              "context_strategy": (state["options"].get("context_strategy") or config.context_strategy) if session["channel_origin"] == "chatgpt" else "local",
                              "channel": session["channel_origin"]}
        for exchange in state["exchanges"]:
            exchange["stale"] = any(current_sources.get(mid) != value for mid, value in exchange["story_sources"].items())
            exchange.pop("story_sources")
            exchange["trace"] = [{k: v for k, v in row.items() if k in {"role", "content", "tool_calls", "tool_call_id", "web_sources", "web_activity"}}
                                 for row in exchange["trace"]]
        state["context"] = {key: state["context"].get(key) for key in ("summary", "through", "created", "strategy")}
        return state

    def preview(self, sid):
        state, session = self.data.get(sid), self.store.session(sid)
        cfg, config = runtime_config(self.store, session, state["options"])
        web = WebSession(config.web.mode, cfg, "advisor")
        return compile_context(freeze_story(self.store, session), config, cfg, state["exchanges"], state["context"], [], web.definitions(definitions(session["agent_mode"])))

    def options(self, sid, request):
        state, session = self.data.get(sid), self.store.session(sid)
        update = request.model_dump(exclude_unset=True, exclude={"expected_revision"})
        if request.model_selection:
            config = self.store.settings_document().shared.advisor
            profile = config.official_model if session["channel_origin"] == "chatgpt" else config.model
            if profile is not None:
                update["model_selection"] = request.model_selection.remember(
                    state["options"].get("model_selection"), profile).model_dump(exclude_defaults=True)
        if update.get("context_strategy") == "openai_compact" and session["channel_origin"] != "chatgpt":
            raise ValueError("只有官方渠道可使用云端 compact")
        runtime_config(self.store, session, {**state["options"], **update})
        self.data.update_options(sid, request.expected_revision, update)
        return self.public(sid)

    def submit(self, sid, request):
        state, session = self.data.get(sid), self.store.session(sid)
        cfg, config = runtime_config(self.store, session, state["options"])
        if not self.provider_factory:
            require_model_connection(cfg)
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute("SELECT id FROM advisor_runs WHERE session=? AND operation=?", (sid, request.operation_id)).fetchone()
            if prior:
                return self.data.run(prior[0])
            self.data.ensure(db, sid, request.expected_revision)
            if db.execute("SELECT 1 FROM advisor_runs WHERE session=? AND status IN ('queued','running')", (sid,)).fetchone():
                raise Conflict("参谋 agent 正在回答")
            pending = db.execute("SELECT id,progress,request FROM advisor_runs WHERE session=? AND progress IS NOT NULL", (sid,)).fetchone()
            progress = None
            payload = request.model_dump()
            if request.resume_run_id:
                if not pending or pending[0] != request.resume_run_id or request.text.strip():
                    raise Conflict("只能继续最新进度；修改问题请先丢弃旧进度")
                progress = json.loads(pending[1])
                if progress["identity"] != context_identity(cfg, config):
                    raise Conflict("参谋 agent 配置已改变，请丢弃旧进度后重新提问")
                if progress.get("preference") is not None and progress["story"]["revision"] != session["revision"]:
                    raise Conflict("故事已改变，旧偏好修改不能继续；请丢弃后重新提问")
                payload["text"] = json.loads(pending[2])["text"]
                db.execute("UPDATE advisor_runs SET progress=NULL WHERE id=?", (pending[0],))
            elif pending:
                raise Conflict("请先继续或丢弃上次未完成的咨询")
            elif not request.text.strip() and not request.compact:
                raise ValueError("请输入咨询内容")
            rid = uid()
            if progress is None:
                progress = {"story": freeze_story(self.store, session), "context": state["context"],
                            "identity": context_identity(cfg, config), "trace": [], "preference": None, "pending": None}
            db.execute("INSERT INTO advisor_runs(id,session,operation,request,status,created,progress) VALUES (?,?,?,?,'queued',?,?)",
                       (rid, sid, request.operation_id, dump(payload), time.time(), dump(progress)))
        try:
            owner = AdvisorRun(self, rid, cfg, config)
        except Exception as error:
            self.fail(rid, "error", str(error))
            raise
        task = asyncio.create_task(self.run(owner))
        self.tasks[rid] = task
        task.add_done_callback(lambda done: self.tasks.pop(rid, None))
        return self.data.run(rid)

    async def run(self, owner):
        provider = None
        try:
            with self.store.connection() as db:
                db.execute("UPDATE advisor_runs SET status='running' WHERE id=?", (owner.rid,))
            provider = self.provider_factory() if self.provider_factory else await make_provider(owner.cfg)
            await owner.run(provider)
        except asyncio.CancelledError:
            self.fail(owner.rid, "cancelled", "已停止，可继续或丢弃本次咨询")
        except Exception as error:
            self.fail(owner.rid, "error", str(error))
        finally:
            if provider:
                await provider.close()

    def fail(self, rid, status, message):
        with self.store.connection() as db:
            db.execute("UPDATE advisor_runs SET status=?,error=? WHERE id=?", (status, message, rid))
        self.data.event(rid, status, {"message": message})

    async def cancel(self, rid):
        self.data.run(rid)
        task = self.tasks.get(rid)
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if self.data.run(rid)["status"] in ("queued", "running"):
            self.fail(rid, "cancelled", "已停止，可继续或丢弃本次咨询")
        return self.data.run(rid)

    async def cancel_session(self, sid):
        await asyncio.gather(*(self.cancel(rid) for rid in list(self.tasks) if self.data.run(rid)["session"] == sid))

    async def close(self):
        await asyncio.gather(*(self.cancel(rid) for rid in list(self.tasks)), return_exceptions=True)


class AdvisorRun:
    def __init__(self, service, rid, cfg, config):
        self.service, self.store, self.data, self.rid = service, service.store, service.data, rid
        self.cfg, self.config = cfg, config
        run = self.data.run(rid, private=True)
        self.sid, self.request = run["session"], run["request"]
        progress = run["progress"]
        self.story, self.context = progress["story"], progress["context"]
        self.trace, self.preference, self.pending = progress["trace"], progress["preference"], progress["pending"]
        self.web_events = progress.get("web_events", [])
        self.web_sources = progress.get("web_sources", [])
        self.exchanges = self.data.get(self.sid)["exchanges"]
        self.view = ModelView()
        self.view.reserved = set(progress.get("reserved", self.context.get("reserved", []))) | set(HANDLE.findall(dump([self.story, self.exchanges, self.request["text"]])))
        for raw in progress.get("view", self.context.get("view", [])):
            self.view.bind(raw)
        self.web_session = WebSession(config.web.mode, cfg, "advisor", self.event, failed=progress.get("web_fallback", False))
        self.base_definitions = definitions(self.story["agent_mode"])
        self.definitions = self.web_session.definitions(self.base_definitions)
        self.tools = AdvisorTools(self)
        self.web = WebTools(cfg.web, cfg.network, service.web_transport, mode=config.web.mode, event=self.event)
        self.context_prepared = progress.get("context_prepared", False)
        self.calls = 0
        self.request_context = ProviderRequestContext(str(uuid5(NAMESPACE_URL, "advisor:" + self.sid)), rid, "consultation", self.store.device_id())

    def event(self, kind, payload):
        if kind == "web_activity":
            self.web_events.append(copy.deepcopy(payload))
        if kind == "web_sources":
            sources = {row["url"]: row for row in self.web_sources}
            sources.update({row["url"]: row for row in payload["sources"]})
            self.web_sources = list(sources.values())
        if kind not in {"provider_request", "provider_response"}:
            self.data.event(self.rid, kind, payload)

    def save(self):
        progress = {"story": self.story, "context": self.context, "trace": self.trace, "preference": self.preference,
                    "pending": self.pending, "view": list(self.view.forward), "identity": context_identity(self.cfg, self.config),
                    "context_prepared": self.context_prepared, "reserved": sorted(self.view.reserved),
                    "web_fallback": self.web_session.failed, "web_events": self.web_events, "web_sources": self.web_sources}
        with self.store.connection() as db:
            db.execute("UPDATE advisor_runs SET progress=? WHERE id=?", (dump(progress), self.rid))

    def compiled(self):
        pending = [{"role": "system", "message_type": "advisor_notice", "runtime_phase": "body", "content": "以下当前用户咨询的引用为 Q1。只有其中的明确修改要求可以授权更新本轮偏好。"},
                   {"role": "user", "content": self.request["text"]}, *self.trace]
        return compile_context(self.story, self.config, self.cfg, self.exchanges, self.context, pending, self.definitions, self.view)

    def web_fallback(self):
        self.definitions = self.web_session.definitions(self.base_definitions)
        self.request_slot()
        self.save()

    def check_preference_write(self, db=None):
        if db is None:
            with self.store.connection() as connection:
                return self.check_preference_write(connection)
        current = db.execute("SELECT revision,turn_preference FROM sessions WHERE id=?", (self.sid,)).fetchone()
        if not current or current["revision"] != self.story["revision"] or current["turn_preference"] != self.story["turn_preference"]:
            raise Conflict("故事或本轮偏好已改变，请重新咨询后再修改")
        if db.execute("SELECT 1 FROM turns WHERE session=? AND status IN ('queued','running','awaiting_review')", (self.sid,)).fetchone():
            raise Conflict("正文正在运行或待审核，本次只能只读咨询")
        if db.execute("SELECT 1 FROM context_operations WHERE session=? AND status IN ('queued','running')", (self.sid,)).fetchone():
            raise Conflict("故事正在整理上下文，请完成后再修改偏好")

    def request_slot(self):
        if self.calls >= self.config.max_steps:
            raise ValueError("本次调用额度已用完，可继续咨询；待保存偏好尚未生效")
        self.calls += 1

    async def prepare_context(self, provider):
        if self.context_prepared:
            return
        compiled = self.compiled()
        force = self.request.get("compact", False)
        if not force and (not self.config.auto_compact or compiled["estimated_tokens"] <= self.cfg.context_tokens):
            self.context_prepared = True
            return
        old = self.exchanges[:-self.config.keep_recent]
        if not old:
            self.event("context_warning", {"message": "没有可整理的旧咨询，选中的资料将完整发送。"})
            self.context_prepared = True
            return
        self.event("compacting", {"message": "正在整理旧咨询；故事记忆保持不变。"})
        through = self.context.get("through", 0) if compiled["context_valid"] else 0
        old = [e for e in old if e["seq"] > through]
        if not old:
            self.context_prepared = True
            return
        # Process whole exchanges in bounded groups. No individual source block
        # or tool result is clipped to fit a budget.
        summary = self.context.get("summary", "") if compiled["context_valid"] else ""
        checkpoint = self.context.get("checkpoint", []) if compiled["context_valid"] else []
        base = [{"role": "system", "content": "整理用户与参谋 agent 的旧咨询，保留用户要求、已查证结论及来源、方案取舍和未完成问题。明确这只是咨询摘要，不是已发生剧情。仅返回摘要，不调用工具。"}]
        batch = []
        def save_partial(summary_value, checkpoint_value, through_value):
            self.context = {"identity": context_identity(self.cfg, self.config), "sources": sources(self.story),
                            "through": through_value, "summary": summary_value, "checkpoint": checkpoint_value,
                            "strategy": self.config.context_strategy, "created": time.time()}
            self.save()
        async def summarize(rows, previous):
            messages = base + ([{"role": "system", "content": "此前咨询摘要：\n" + previous}] if previous else []) + consultation_body(rows, self.story)
            self.request_slot()
            response = await provider.chat_complete(self.view.messages(messages), extra_body={"tool_choice": "none"})
            if getattr(provider, "last_tool_calls", []):
                raise ValueError("咨询整理返回工具调用，未执行")
            text = getattr(response, "text", None) or getattr(response, "content", "")
            if not isinstance(text, str) or not text.strip():
                raise ValueError("咨询整理没有返回有效摘要")
            text = self.view.restore(text)
            save_partial(text, [], rows[-1]["seq"])
            return text
        if self.config.context_strategy == "openai_compact":
            from tavern.conversation.compaction import checkpoint_items, opaque_message
            for exchange in old:
                messages = base + ([opaque_message(checkpoint, self.cfg)] if checkpoint else []) + self.view.messages(consultation_body([exchange], self.story))
                self.request_slot()
                await provider.chat_complete(messages, extra_body={"tool_choice": "none"}, _compact_threshold=1024,
                    _maintenance_instructions="仅整理已完成咨询，不执行历史工具，返回 OK。")
                if getattr(provider, "last_tool_calls", []):
                    raise ValueError("咨询整理返回工具调用，未执行")
                checkpoint = checkpoint_items((getattr(provider, "last_native_response", None) or {}).get("items", []))
                if not checkpoint:
                    raise ValueError("服务端未返回有效参谋 agent 压缩状态，未继续咨询")
                checkpoint = [item for item in checkpoint if item.get("type") != "message"]
                save_partial("", checkpoint, exchange["seq"])
        else:
            for exchange in old:
                candidate = batch + [exchange]
                estimate = request_tokens(base + [{"role": "system", "content": summary}] + consultation_body(candidate, self.story), self.cfg)
                if batch and estimate > self.cfg.context_tokens:
                    summary = await summarize(batch, summary)
                    batch = []
                batch.append(exchange)
            if batch:
                summary = await summarize(batch, summary)
        save_partial(summary, checkpoint, old[-1]["seq"])
        self.context_prepared = True
        self.save()
        self.event("compacted", {"through": old[-1]["seq"], "summary": summary, "strategy": self.config.context_strategy})

    async def execute_pending(self):
        if not self.pending:
            return
        returned = {m.get("tool_call_id") for m in self.trace if m["role"] == "tool"}
        for call in self.pending["tool_calls"]:
            if call["id"] in returned:
                continue
            name, raw = call["function"]["name"], call["function"]["arguments"]
            self.event("tool_started", {"tool": name, "call_id": call["id"]})
            try:
                result = await self.tools.execute(name, raw)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                result = {"error": self.view.text(str(error))}
            self.trace.append({"role": "tool", "tool_call_id": call["id"], "content": dump(result)})
            self.save()
            self.event("tool_finished", {"tool": name, "call_id": call["id"], "result": result})
        self.pending = None
        self.save()

    def commit(self, text, *, compact_only=False):
        response = self.view.restore(text)
        # Opaque compact state can contain projected handles. Keep its mapping
        # stable across later consultations, not just within a single run.
        self.context.update(view=list(self.view.forward), reserved=sorted(self.view.reserved))
        # Portable complete tool history is independent of the model origin.
        trace = []
        for message in self.trace:
            row = {k: copy.deepcopy(v) for k, v in message.items() if k in {"role", "content", "tool_calls", "tool_call_id",
                "web_sources", "_native_response", "_response_origin", "_response_digest", "reasoning"}}
            if isinstance(row.get("content"), str):
                row["content"] = self.view.restore(row["content"])
            for call in row.get("tool_calls", []):
                call["function"]["arguments"] = self.view.restore(call["function"]["arguments"])
            trace.append(row)
        if trace and self.web_events:
            trace[-1]["web_activity"] = copy.deepcopy(self.web_events)
        if trace and self.web_sources:
            trace[-1]["web_sources"] = copy.deepcopy(self.web_sources)
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self.data.ensure(db, self.sid)
            if self.preference is not None:
                self.check_preference_write(db)
                db.execute("UPDATE sessions SET turn_preference=?,revision=revision+1 WHERE id=?", (self.preference, self.sid))
            if not compact_only:
                db.execute("""INSERT INTO advisor_exchanges(session,run,user_text,response,trace,story_head,story_sources,created)
                              VALUES (?,?,?,?,?,?,?,?)""", (self.sid, self.rid, self.request["text"], response, dump(trace), self.story["head"], dump(sources(self.story)), time.time()))
            db.execute("UPDATE advisor_conversations SET context=?,revision=revision+1 WHERE session=?", (dump(self.context), self.sid))
            db.execute("UPDATE advisor_runs SET status='completed',response=?,progress=NULL WHERE id=?", (response, self.rid))
        self.event("completed", {"preference_updated": self.preference is not None, "response": response})

    async def run(self, provider):
        configure_operation(provider, self.request_context, self.event, "advisor")
        self.event("started", {})
        previous_through = self.context.get("through")
        await self.prepare_context(provider)
        if self.request.get("compact"):
            self.commit("咨询上下文整理完成。" if self.context.get("through") != previous_through else "当前没有可退出的旧咨询，原文保持完整。", compact_only=True)
            return
        await self.execute_pending()
        for _ in range(max(0, self.config.max_steps - self.calls)):
            compiled = self.compiled()
            self.event("context_stats", {k: compiled[k] for k in ("estimated_tokens", "context_limit", "recent_refs", "token_counting", "opaque")})
            if compiled["estimated_tokens"] > self.cfg.context_tokens:
                self.event("context_warning", {"message": "请求估算超出模型容量，仍完整发送已选资料；可调整近期剧情范围或上下文容量。"})
            self.save()
            self.event("response_started", {})
            text = ""
            self.request_slot()
            async for chunk in self.web_session.chat(provider, provider_messages(provider, compiled["runtime_messages"], self.cfg), self.base_definitions,
                                             extra_body={"parallel_tool_calls": True}, on_fallback=self.web_fallback):
                if chunk is None:
                    text = ""
                elif chunk:
                    text += chunk
                    self.event("text", {"text": chunk})
            calls = getattr(provider, "last_tool_calls", []) or []
            used_calls = {c["id"] for m in self.trace for c in m.get("tool_calls", [])}
            if len(calls) > 32 or len({c.id for c in calls}) != len(calls) or any(not c.id or c.id in used_calls for c in calls):
                raise ValueError("模型返回无效工具调用，本次进度保留")
            assistant = {"role": "assistant", "content": text}
            if calls:
                assistant["tool_calls"] = [{"id": c.id, "type": "function", "function": {"name": c.name, "arguments": c.arguments}} for c in calls]
            assistant.update(copy.deepcopy(getattr(provider, "last_assistant_extra_fields", {}) or {}))
            native = getattr(provider, "last_native_response", None)
            if native:
                assistant["_native_response"] = copy.deepcopy(native)
            assistant["_response_origin"], assistant["_response_digest"] = response_origin(self.cfg), message_digest(assistant)
            self.event("usage", getattr(provider, "last_usage", {}) or {})
            if not calls:
                if not text.strip():
                    raise ValueError("模型未返回完整回答，本次进度保留")
                self.trace.append(assistant)
                self.commit(text)
                return
            self.trace.append(assistant)
            self.pending = assistant
            self.save()
            await self.execute_pending()
        raise ValueError("本次调用额度已用完，可继续咨询；待保存偏好尚未生效")
