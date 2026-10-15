"""Recoverable native tool loop, isolated from all roleplay sessions."""
import asyncio
import copy
import json
import time

from tavern.character.card_workflow import validate_document, validation_report
from tavern.character.editor_context import EditorContext
from tavern.character.editor_runtime_config import editor_runtime_config
from tavern.character.editor_tools import EditorTools, tool_definitions
from tavern.core.message_adapter import provider_messages
from tavern.scripting.native_response import message_digest
from tavern.scripting.request_context import ProviderRequestContext, configure_operation
from tavern.storage.store import dump
from tavern.web.client import WebTools
from tavern.web.cloud import WebSession


class EditorRun:
    def __init__(self, service, rid):
        self.service, self.store, self.edits, self.rid = service, service.store, service.edits, rid
        run = self.edits.run(rid)
        self.wid, self.request = run["workspace"], run["request"]
        self.cfg = editor_runtime_config(self.store, self.wid, self.request)
        with self.store.connection() as db:
            workspace = self.edits.load(db, self.wid)
        self.environment = workspace.get("environment")
        progress = self.edits.progress(self.wid) if self.request.get("resume_run_id") else None
        self.working = copy.deepcopy(progress["working"] if progress else {"cards": workspace["cards"], "state": workspace["state"]})
        self.base = copy.deepcopy(progress["base"] if progress else self.working)
        self.current_refs = list(progress["current_refs"] if progress else [])
        if self.request.get("_input_ref") and self.request["_input_ref"] not in self.current_refs:
            self.current_refs.append(self.request["_input_ref"])
        self.legacy = self.request.get("_legacy", False)
        self.reads = {}  # Restored with the visible context, not the run boundary.
        self.persona_reads = copy.deepcopy(progress.get("persona_reads", {}) if progress else {})
        self.persona_changes = copy.deepcopy(progress.get("persona_changes", {}) if progress else {})
        self.trace = copy.deepcopy(progress.get("trace", []) if progress else [])
        self.pending_calls = copy.deepcopy(progress.get("pending_calls") if progress else None)
        saved = copy.deepcopy(progress["context"] if progress else self.edits.context(self.wid))
        self.rolled = bool(progress and progress.get("rolled"))
        self.web_session = WebSession(self.cfg.card_editor_config.web.mode, self.cfg, "card_editor", self.event,
                                      failed=bool(progress and progress.get("web_fallback")))
        self.definitions = self.web_session.definitions(tool_definitions())
        self.web = WebTools(self.cfg.web, self.cfg.network, service.web_transport,
                            mode=self.cfg.card_editor_config.web.mode, event=self.event)
        # Steering must close an old batch before a new user message reaches the
        # provider. Never execute a stale proposed mutation after changed intent.
        if progress and self.request.get("text", "").strip() and self.pending_calls:
            returned = {r["tool_call_id"] for r in self.pending_calls.get("results", [])}
            for call in self.pending_calls["assistant"].get("tool_calls", []):
                if call["id"] not in returned:
                    result = {"role": "tool", "tool_call_id": call["id"], "_tool_error": True,
                              "content": dump({"cancelled": True, "message": "用户补充了新要求，此调用尚未执行，请重新判断"})}
                    saved["messages"].append(result)
                    self.trace.append(result)
            self.pending_calls = None
        self.context = EditorContext(saved, self)
        self.tools = EditorTools(self)
        self.calls = 0
        self.request_context = ProviderRequestContext(self.wid, rid, self.context.epoch, self.store.device_id(),
            sent_breakpoints=self.store.cache_breakpoint_history(self.wid, "card_editor"))
        self.request_context.diagnostics = {"rebuild_reason": self.context.reason}
        self.cfg.cache_scope, self.cfg.cache_window = self.wid, self.context.epoch

    def event(self, kind, payload):
        self.edits.event(self.rid, kind, payload)

    @staticmethod
    def usage(provider):
        # last_usage supplies numeric zeros for legacy accounting; the transport
        # history preserves unknown cache counters for truthful UI statistics.
        history = getattr(provider, "usage_history", [])
        return copy.deepcopy(history[-1] if history else getattr(provider, "last_usage", {}) or {})

    def save(self):
        self.edits.save_progress(self.wid, self.rid, {"working": self.working, "base": self.base,
            "current_refs": self.current_refs, "context": self.context.saved(), "trace": self.trace,
            "pending_calls": self.pending_calls, "persona_reads": self.persona_reads,
            "persona_changes": self.persona_changes, "calls": self.calls, "rolled": self.rolled,
            "web_fallback": self.web_session.failed})

    def web_fallback(self):
        self.definitions = self.web_session.definitions(tool_definitions())
        # Switching only the search executor must not discard this unfinished
        # turn's completed reads/results on a later resume.
        self.context.refresh_tool_policy()
        self.save()
        if self.calls >= (12 if self.legacy else self.cfg.card_editor_config.max_steps):
            raise ValueError("本次模型调用额度已用完，可继续使用本地搜索")
        self.calls += 1
        self.event("model_call", {"call": self.calls, "model": self.cfg.model, "search_fallback": True})

    def response(self, provider, text, calls):
        message = {"role": "assistant", "content": text}
        if calls:
            message["tool_calls"] = [{"id": c.id, "type": "function", "function": {"name": c.name, "arguments": c.arguments}} for c in calls]
        message.update(copy.deepcopy(getattr(provider, "last_assistant_extra_fields", {}) or {}))
        native = getattr(provider, "last_native_response", None)
        if native:
            message["_native_response"] = copy.deepcopy(native)
        message["_response_origin"] = self.context.origin
        message["_response_digest"] = message_digest(message)
        return message

    async def execute_pending(self):
        batch = self.pending_calls
        if not batch:
            return
        calls = batch["assistant"]["tool_calls"]
        returned = {r["tool_call_id"] for r in batch.get("results", [])}
        for index, call in enumerate(calls):
            if call["id"] in returned:
                continue
            name, args = call["function"]["name"], call["function"]["arguments"]
            action = f"{self.rid}:{call['id']}"
            started = time.monotonic()
            self.event("tool_started", {"action_id": action, "tool": name, "arguments": args[:4000], "arguments_truncated": len(args) > 4000})
            status = "completed"
            try:
                if (self.context.must_rollover or batch.get("forced")) and (name != "new_context" or len(calls) != 1):
                    raise ValueError("上下文已满，只能单独调用 new_context")
                if len(calls) != 1 and any(c["function"]["name"] == "new_context" for c in calls):
                    raise ValueError("new_context 必须单独调用，当前混合批次不执行")
                result = await self.tools.execute(name, args)
            except asyncio.CancelledError:
                self.event("tool_finished", {"action_id": action, "tool": name, "status": "cancelled", "detail": "工具中断，尚未提交"})
                raise
            except Exception as error:
                status = "error"
                result = {"error": self.context.view.text(str(error))}
            encoded = dump(result)
            result_message = {"role": "tool", "tool_call_id": call["id"], "content": encoded}
            if status == "error":
                result_message["_tool_error"] = True
            batch.setdefault("results", []).append(result_message)
            self.context.messages.append(result_message)
            self.trace.append(result_message)
            # State plus paired result are one persisted fact; resumes skip it.
            self.save()
            self.event("tool_finished", {"action_id": action, "tool": name, "status": status,
                "result": encoded[:4000], "detail": result.get("error", ""), "result_truncated": len(encoded) > 4000,
                "duration_ms": round((time.monotonic() - started) * 1000)})
        self.pending_calls = None
        if self.rolled:
            self.context.rollover()
            self.persona_reads = {}
            self.rolled = False
            self.request_context.window_id = self.context.epoch
            self.cfg.cache_window = self.context.epoch
            self.event("context_rollover", {"window": self.working["state"].get("window", 0)})
        self.save()

    def archived_trace(self):
        result = []
        for message in self.trace:
            item = {k: copy.deepcopy(v) for k, v in message.items() if k in {"role", "content", "tool_calls", "tool_call_id", "_tool_error", "web_sources"}}
            if isinstance(item.get("content"), str):
                item["content"] = self.context.view.restore(item["content"])
            for call in item.get("tool_calls", []):
                call["function"]["arguments"] = self.context.view.restore(call["function"]["arguments"])
            result.append(item)
        return result

    def commit(self, provider, text, assistant):
        for item in self.working["cards"]:
            validate_document(item["document"])
        response = self.context.view.restore(text)
        self.context.messages.append(assistant)
        self.trace.append(assistant)
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            workspace = self.edits.check(db, self.wid, self.request["expected_revision"], idle=False)
            self.tools.commit_personas(db)
            if self.persona_changes:
                self.context.update("persona_changes", [], "待保存的用户角色已全部保存到用户角色库：")
            for item in self.working["cards"]:
                previous = next((c for c in workspace["cards"] if c["ref"] == item["ref"]), None)
                if previous and previous["document"] != item["document"]:
                    self.edits.version(db, self.wid, previous, self.request["text"])
            changed = workspace["cards"] != self.working["cards"] or workspace["state"] != self.working["state"]
            workspace.update(self.working)
            self.edits.write(db, workspace, increment=changed)
            history_ref = self.edits.message(db, self.wid, "assistant", response, self.rid)
            self.context.seen.append(history_ref)
            self.context.update("final_ref", history_ref, "上条最终回复的历史引用：")
            db.execute("INSERT OR REPLACE INTO card_edit_contexts VALUES (?,?)", (self.wid, dump(self.context.saved())))
            db.execute("INSERT OR REPLACE INTO card_editor_traces VALUES (?,?)", (self.rid, dump(self.archived_trace())))
            db.execute("DELETE FROM card_editor_progress WHERE workspace=?", (self.wid,))
            db.execute("UPDATE card_edit_runs SET status='completed',response=? WHERE id=?", (response, self.rid))
        reports = [{"card_ref": c["ref"], **validation_report(c["document"])} for c in self.working["cards"]]
        self.event("completed", {"validation": reports[0] if len(reports) == 1 else reports,
                                 "usage": self.usage(provider), "calls": self.calls})

    async def run(self, provider):
        configure_operation(provider, self.request_context, self.event, "card_editor")
        self.event("started", {})
        if self.context.reason:
            self.event("context_baseline", self.request_context.audit())
        self.save()
        await self.execute_pending()
        maximum = 12 if self.legacy else self.cfg.card_editor_config.max_steps
        for _ in range(maximum):
            if self.calls >= maximum:
                break
            self.calls += 1
            messages = self.context.prepare()
            if self.context.must_rollover:
                self.event("context_warning", {"message": "编辑上下文达到窗口上限，要求换窗；原文仍完整保留"})
            self.save()
            tokens = self.context.tokens()
            self.event("model_call", {"call": self.calls, "model": self.cfg.model})
            self.event("context_stats", {"estimated_tokens": tokens, "context_limit": self.cfg.context_tokens, "model": self.cfg.model})
            text = ""
            options = {"parallel_tool_calls": False}
            if self.context.must_rollover:
                options["tool_choice"] = {"type": "function", "function": {"name": "new_context"}}
            async for chunk in self.web_session.chat(provider, provider_messages(provider, messages, self.cfg), tool_definitions(),
                                                    extra_body=options, on_fallback=self.web_fallback):
                if chunk is None:
                    text = ""
                elif chunk:
                    text += chunk
                    self.event("text", {"text": chunk})
            self.event("context_stats", {"estimated_tokens": tokens, "context_limit": self.cfg.context_tokens,
                                         "model": self.cfg.model, "usage": self.usage(provider)})
            calls = getattr(provider, "last_tool_calls", []) or []
            if len(calls) > 16 or len({c.id for c in calls}) != len(calls) or any(not c.id for c in calls):
                raise ValueError("无效的工具调用序列，本轮进度保留")
            for call in calls:
                try:
                    args = json.loads(call.arguments)
                    if not isinstance(args, dict):
                        raise ValueError()
                except (ValueError, TypeError):
                    raise ValueError("模型工具参数不是合法 JSON 对象；本轮进度保留") from None
            assistant = self.response(provider, text, calls)
            if not calls:
                if self.context.must_rollover:
                    self.context.messages.extend([assistant, {"role": "system", "message_type": "reply_correction", "runtime_phase": "body", "content": "回复未提交，当前必须先单独调用 new_context 换窗。"}])
                    continue
                if not text.strip():
                    raise ValueError("模型没有返回完整说明，本轮进度保留")
                self.commit(provider, text, assistant)
                return
            self.context.messages.append(assistant)
            self.trace.append(assistant)
            self.pending_calls = {"assistant": assistant, "results": [], "forced": self.context.must_rollover}
            self.save()
            await self.execute_pending()
        raise ValueError("本次模型调用额度已用完，进度已保留；可继续或补充要求")
