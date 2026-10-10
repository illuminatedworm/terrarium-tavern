"""One model, one bounded native tool loop, model-controlled window rollover."""
import copy
import json
import time

import jsonpatch
import jsonpointer

from tavern.character.cards import normalize
from tavern.conversation.cache_breakpoint import model_messages
from tavern.conversation.context import macros, wrap_lorebook
from tavern.conversation.regeneration import reply_for_regeneration
from tavern.conversation.variable_rules import collect_variable_rules
from tavern.core.message_adapter import provider_messages
from tavern.core.model_view import ModelView
from tavern.core.provider import make_provider
from tavern.full_agent.content import lore_references
from tavern.full_agent.content_history import CONTENT_TOOLS_VERSION, content_history, retired_content_call
from tavern.full_agent.context import compile_window, load_trace
from tavern.full_agent.lore import inject_lore, retain_lore
from tavern.full_agent.protocol import ToolError, TurnError, failed
from tavern.full_agent.state import full_state
from tavern.full_agent.tools import AgentTools, encoded, tool_definitions
from tavern.scripting.native_response import message_digest
from tavern.scripting.request_context import ProviderRequestContext, response_origin


class FullAgent:
    def __init__(self, store, tid, document, session, history, snapshot, request, script, settings,
                 user_message, retrieval, progress=None):
        self.store, self.tid, self.document = store, tid, document
        self.session = {**session, "messages": history, "edit_notification_history": session["messages"]}
        self.history, self.snapshot, self.request, self.script = history, snapshot, request, script
        self.regenerate_reply = reply_for_regeneration(request, session)
        self.settings, self.user_message, self.lane = settings, user_message, retrieval
        self.source_history = history + [user_message]
        self.state = full_state(snapshot)
        self.trace = load_trace(store, session["id"], history)
        self.continuation, self.new_trace = [], []
        self.definitions = tool_definitions(request, session, settings.full_agent_config, settings=settings, document=document)
        self.tools = AgentTools(self)
        # Tool exchanges survive turns/windows. Their short document handles
        # must keep the same meaning after document reorder or a new provider.
        initial_view = {"document": document, "state": snapshot["state"]}
        self.model_view = ModelView(initial_view)
        refs = self.state.setdefault("model_references", {})
        self.model_view.reverse = refs
        self.model_view.forward = {raw: ref for ref, raw in refs.items()}
        self.model_view.reserved.difference_update(refs)
        self.model_view.seed(initial_view)
        self.rolled, self.retrieval_query = False, ""
        self.must_rollover = False
        self.exiting = set()
        self.patch_state_done = False
        # Keep macro values and rule edits stable across tools, rollovers and
        # failed-request resumes. The next turn takes a fresh rule snapshot.
        self.variable_rules = (copy.deepcopy(progress["variable_rules"])
            if progress is not None and "variable_rules" in progress else
            collect_variable_rules(document, session["persona"], snapshot["state"])
            if request.update_variables else None)
        self.resumed = progress is not None
        self.final_response = None
        self.context_epoch = ""
        self.native_epoch = ""
        saved_context = (progress or {}).get("request_context", {})
        self.request_context = ProviderRequestContext(
            session["id"], saved_context.get("turn_id", request.resume_turn_id or tid),
            self.state["window"]["id"], store.device_id(), routing=copy.deepcopy(saved_context.get("routing", {})),
            sent_breakpoints=store.cache_breakpoint_history(session["id"]))
        if progress:
            self.continuation, retired = content_history(progress["continuation"])
            if retired:
                self.request_context.routing.clear()
            self.new_trace = copy.deepcopy(progress["new_trace"])
            self.retrieval_query = progress["retrieval_query"]
            self.must_rollover = progress["must_rollover"]
            if progress.get("provider_origin") and progress["provider_origin"] != response_origin(settings):
                # A different model may have a different context limit. Reassess
                # it from portable history instead of inheriting the old limit.
                self.must_rollover = False
            self.exiting = set(progress["exiting"])
            self.tools.reads.restore(progress.get("tool_reads", {}))
            self.patch_state_done = progress["patch_state_done"]

    async def save_progress(self):
        script_mirror = await self.script.call("snapshot")
        self.store.save_full_agent_progress(self.tid, {
            "user": self.user_message, "snapshot": self.snapshot, "document": self.document,
            "script_mirror": script_mirror,
            "continuation": self.continuation, "new_trace": self.new_trace,
            "retrieval_query": self.retrieval_query, "must_rollover": self.must_rollover,
            "patch_state_done": self.patch_state_done,
            "variable_rules": self.variable_rules,
            "exiting": sorted(self.exiting), "tool_reads": copy.deepcopy(self.tools.reads.records),
            "request_context": self.request_context.saved(),
            "provider_origin": response_origin(self.settings),
        })

    def event(self, kind, payload):
        self.store.event(self.tid, kind, {"purpose": "full_agent", **payload})

    def expand(self, text):
        return macros(text, normalize(self.document), self.session["persona"], self.snapshot["state"])

    def lore_refs(self):
        return lore_references(self.document, self.state)

    def retain(self):
        visible = self.history if self.settings.context_strategy == "openai_compact" else self.history[self.state["window"]["start"]:]
        self.exiting |= retain_lore(self.document, self.snapshot, visible, self.user_message["id"],
                                  sum(m["role"] == "user" for m in self.history) + 1)
        return visible

    async def retrieve(self, query, *, source, include_ref=False, include_title=False):
        visible = self.retain()
        settings = self.settings.model_copy(deep=True)
        if source == "agent":
            settings.retrieval.query_lookback = 0
        result = await self.lane.prepare(self.document, visible, query, self.snapshot, settings,
                                         self.session, self.tid, source=source)
        added, notes = inject_lore(self.document, self.session, visible, self.snapshot, query, settings,
                                   self.tid, self.user_message["id"], result, self.exiting, persist=source == "auto")
        self.event("retrieval", {**{k: v for k, v in result.items() if k != "entries"}, "source": source,
                                 "injected": [r["id"] for r in added] if source == "auto" else [],
                                 "tool_results": [r["id"] for r in added] if source == "agent" else [],
                                 "injection_diagnostics": notes})
        refs = {eid: ref for ref, eid in self.lore_refs().items()}
        matches = [{**({"ref": refs[r["id"]]} if include_ref else {}),
                    **({"title": r["title"]} if include_title else {}),
                    "text": wrap_lorebook(r["content"])} for r in added]
        result = {"matches": matches}
        if include_ref:
            result["already_in_context"] = [
                {"ref": refs[r["id"]], **({"title": r["title"]} if include_title else {})}
                for r in self.snapshot.get("lore_injections", []) if r not in added
                and not r.get("manually_removed")][:20]
        return result

    def compile(self):
        self.retain()
        result = compile_window(self.document, self.session, self.history, self.snapshot, self.request.text,
            self.settings, self.definitions, trace=self.trace, continuation=self.continuation,
            input_id=self.user_message["id"], instruction=(
                self.settings.regenerate_prompt + "\n" + self.request.regenerate_note
                if self.request.regenerate_note else ""),
            force_rollover=self.must_rollover,
            require_state_patch=self.request.update_variables, variable_rules=self.variable_rules,
            allow_content_edit=self.request.allow_content_edit, regenerate_reply=self.regenerate_reply)
        self.must_rollover = result["rollover_required"]
        self.context_epoch = result["context_ledger"]["epoch"]
        self.native_epoch = result["context_ledger"]["native_epoch"]
        self.request_context.window_id = self.state["window"]["id"]
        self.request_context.diagnostics = copy.deepcopy(result["context_ledger"])
        for message in result["context_additions"]:
            self.append(message, self.state["window"]["id"])
        if result["context_ledger"]["rebuild_reason"]:
            self.event("context_baseline", result["context_ledger"])
        if result["estimated_tokens"] > self.settings.context_tokens:
            self.event("context_warning", {"message": "Full Agent 请求估算超出模型上下文，仍保留完整文本"})
        self.store.set_turn(self.tid, preview=encoded(result))
        return result

    def append(self, message, window_id):
        message = {**message, "_context_epoch": self.context_epoch}
        self.continuation.append(copy.deepcopy(message))
        self.new_trace.append({"window_id": window_id, "message": copy.deepcopy(message)})

    async def run(self, provider=None):
        provider = provider or await make_provider(self.settings,
            lambda payload: self.event("provider_request", payload), request_context=self.request_context)
        if hasattr(provider, "request_context"):
            provider.request_context = self.request_context
            self.request_context.bind(response_origin(provider.settings, provider.key))
        if hasattr(provider, "echo_reasoning"):
            provider.echo_reasoning = True
        try:
            if self.settings.full_agent_config.auto_retrieval and not self.resumed:
                await self.retrieve(self.request.text, source="auto")
            self.store.event(self.tid, "input_ready", {"message": self.user_message,
                "lore_injections": self.snapshot.get("lore_injections", [])})
            if self.resumed:
                await self.resume_tools()
            for _ in range(self.settings.full_agent_config.max_steps):
                compiled = self.compile()
                if compiled.get("compact_requires_rebuild"):
                    from tavern.full_agent.compaction import rebuild
                    async def save_replay(value):
                        self.state["compact_replay"] = value
                        self.event("context_replay", {key: value[key] for key in ("batches", "processed", "total")})
                        self.event("usage", {"usage": value["usage"][-1], "calls": 1, "context_maintenance": True})
                        await self.save_progress()
                    checkpoint, _ = await rebuild(compiled, self.settings, provider,
                        progress=self.state.get("compact_replay"), save=save_replay)
                    self.state["compaction"] = checkpoint
                    self.state.pop("compact_replay", None)
                    await self.save_progress()
                    compiled = self.compile()
                if self.must_rollover and not any(t["function"]["name"] == "new_context" for t in self.definitions):
                    raise ValueError("上下文已满，但 new_context 已关闭。请在工具页启用它，或在场景页手动保存并换新窗口。")
                started = time.perf_counter()
                tool_options = {
                    "tools": self.definitions,
                    "tool_choice": {"type": "function", "function": {"name": "new_context"}}
                    if self.must_rollover else "auto",
                    "parallel_tool_calls": self.settings.full_agent_config.parallel_tool_calls
                    and not self.must_rollover} if self.definitions else {}
                # Persist the actual runtime frames even when the very first
                # model request fails. Resume then reuses them instead of guessing.
                await self.save_progress()
                try:
                    response = await provider.chat_complete(provider_messages(provider, model_messages(compiled), self.settings), extra_body=tool_options)
                except Exception as error:
                    record = getattr(provider, "last_response_record", {})
                    if record.get("error"):
                        self.event("provider_response", record)
                    raise TurnError("PROVIDER_ERROR", str(error) or type(error).__name__) from error
                self.event("usage", {"usage": response.usage or {}, "calls": 1,
                                      "duration_s": time.perf_counter() - started})
                calls = getattr(provider, "last_tool_calls", []) or []
                if self.settings.context_strategy == "openai_compact":
                    from tavern.full_agent.compaction import capture_checkpoint
                    if capture_checkpoint(self.state, compiled, self.settings, provider,
                                          self.response_message(response, provider, calls)):
                        self.event("context_compacted", {"message": "云端已压缩上下文", "window": self.state["window"]["number"]})
                self.event("full_agent_response", {"content": response.content or "",
                    "finish_reason": getattr(response, "finish_reason", None),
                    "tool_calls": [{"id": c.id, "name": c.name, "arguments": c.arguments} for c in calls],
                    "provider_response": copy.deepcopy(getattr(provider, "last_response_record", {}))})
                if getattr(response, "finish_reason", None) in ("length", "content_filter"):
                    raise TurnError("PROVIDER_INCOMPLETE", "Full Agent 回复未完整结束，本回合未提交；请检查输出上限或服务商响应")
                if not calls:
                    if self.must_rollover:
                        self.event("full_agent_reply_rejected", {"message": "强制换窗期间的正文回复已否决"})
                        self.reject_reply(response, provider, "刚才的正文回复已被运行时否决，未提交。必须单独调用 new_context 换窗后再继续。")
                        await self.save_progress()
                        continue
                    if self.request.update_variables and not self.patch_state_done:
                        self.event("full_agent_reply_rejected", {"message": "本轮尚未调用 patch_state，正文回复已否决"})
                        self.reject_reply(response, provider, "刚才的正文回复未提交。本轮必须先成功调用一次 patch_state 更新 MVU 变量。")
                        await self.save_progress()
                        continue
                    text = response.content or ""
                    if not text.strip():
                        raise ValueError("Full Agent 没有返回故事正文，本回合未提交")
                    self.store.set_turn(self.tid, draft=text)
                    self.store.event(self.tid, "text", {"text": text})
                    self.final_response = self.response_message(response, provider, calls)
                    return text
                if len(calls) > 8 or any(not c.id for c in calls) or len({c.id for c in calls}) != len(calls):
                    raise TurnError("TOOL_PROTOCOL_INVALID", "模型返回了无效的工具调用序列")
                # A malformed protocol payload (observed from relays as
                # '{}{"content":...}') cannot be repaired by executing a tool.
                # Do not echo it through up to max_steps paid requests. Usage and
                # the raw response are already recorded; prior progress survives.
                for call in calls:
                    try:
                        json.loads(call.arguments)
                    except (ValueError, TypeError):
                        raise TurnError("TOOL_PROTOCOL_INVALID",
                            f"服务商返回的 {call.name} 工具参数不是合法 JSON；已停止本回合，避免连续付费重试。"
                            "请检查提供商的工具调用协议；Claude 可尝试 Anthropic Messages 接口。") from None
                window_id = self.state["window"]["id"]
                assistant_seq = len(self.new_trace) + 1
                assistant = self.response_message(response, provider, calls)
                self.append(assistant, window_id)
                if assistant["content"]:
                    self.event("full_agent_assistant", {"activity_id": f"text-{assistant_seq}",
                        "order": assistant_seq * 10, "content": assistant["content"]})
                # Save the response before executing a batch, then checkpoint each
                # result. Interrupted batches resume only their unpaired calls.
                await self.save_progress()
                await self.execute_tools(assistant, window_id, assistant_seq)
            raise TurnError("BUDGET_EXHAUSTED", "Full Agent 达到本回合模型调用上限，草稿未提交；换窗不会重置调用额度")
        finally:
            await provider.close()

    def reject_reply(self, response, provider, correction):
        message = self.response_message(response, provider, [])
        message["_full_agent_rejected"] = True
        self.append(message, self.state["window"]["id"])
        self.append({"role": "system", "content": correction, "message_type": "reply_correction", "runtime_phase": "body"}, self.state["window"]["id"])

    async def resume_tools(self):
        records = list(self.new_trace)
        for seq, item in enumerate(records, 1):
            message = copy.deepcopy(item["message"])
            if message.get("tool_calls"):
                message["tool_calls"] = [call for call in message["tool_calls"]
                                         if not retired_content_call(message, call)]
            if item["window_id"] != self.state["window"]["id"] or not message.get("tool_calls"):
                continue
            returned = set()
            for following in records[seq:]:
                part = following["message"]
                if part["role"] == "assistant":
                    break
                if part["role"] == "tool":
                    returned.add(part["tool_call_id"])
            if any(call["id"] not in returned for call in message["tool_calls"]):
                self.context_epoch = message.get("_context_epoch", "")
                await self.execute_tools(message, item["window_id"], seq, returned)

    async def execute_tools(self, assistant, window_id, assistant_seq, returned=()):
        calls = assistant["tool_calls"]
        mixed_rollover = len(calls) > 1 and any(call["function"]["name"] == "new_context" for call in calls)
        disabled_batch = len(calls) > 1 and not self.settings.full_agent_config.parallel_tool_calls
        rejected_batch = mixed_rollover or disabled_batch
        for index, call in enumerate(calls):
            if call["id"] in returned:
                continue
            name, arguments = call["function"]["name"], call["function"]["arguments"]
            activity = {"activity_id": f"call-{assistant_seq}-{index}",
                        "order": assistant_seq * 10 + index + 1, "name": name, "arguments": arguments}
            self.event("full_agent_tool_started", activity)
            tool_started = time.perf_counter()
            try:
                if mixed_rollover:
                    raise ToolError("MIXED_CONTEXT_CALLS", "new_context 必须单独调用；当前批次未执行任何工具")
                if self.must_rollover and (name != "new_context" or len(calls) != 1):
                    raise ToolError("CONTEXT_ROLLOVER_REQUIRED", "当前必须换窗；仅接受单独的 new_context，其他调用已否决且未执行")
                if disabled_batch:
                    raise ToolError("PARALLEL_TOOL_CALLS_DISABLED", "已关闭单次多工具调用；本次不执行此批次中尚未执行的工具")
                result = await self.tools.execute(name, arguments)
            except TurnError:
                raise
            except ToolError as error:
                result = error.result
                if error.__cause__ is not None:
                    self.event("full_agent_tool_diagnostic", {"name": name,
                        "error_type": type(error.__cause__).__name__, "message": str(error.__cause__)})
            except jsonpointer.JsonPointerException as error:
                result = ToolError("INVALID_PATH", str(error)).result
            except jsonpatch.JsonPatchException as error:
                result = ToolError("PATCH_VALIDATION_FAILED", str(error)).result
            except (ValueError, KeyError, TypeError) as error:
                result = ToolError("INVALID_ARGUMENTS", str(error)).result
            if failed(result) and name == "patch_state":
                result["error"]["patch_state_completed"] = self.patch_state_done
            result = self.model_view.expose(result)
            self.append({"role": "tool", "tool_call_id": call["id"], "content": encoded(result)}, window_id)
            self.event("full_agent_tool", {**activity, "result": result,
                "duration_s": time.perf_counter() - tool_started,
                "message": name + (" · 调用错误" if failed(result) else " · 完成")})
            if self.rolled:
                self.continuation = []
                self.rolled = False
                self.must_rollover = False
                self.retain()
                self.event("context_rollover", {"window": self.state["window"]["number"],
                                               "message": "Agent 已保存 handoff 并换新窗口"})
            if not rejected_batch:
                await self.save_progress()
        if rejected_batch:
            # A rejected batch must never persist just its first error. Recovery
            # removes failed calls; a partial checkpoint could otherwise turn the
            # remaining call into a valid standalone call.
            await self.save_progress()

    def response_message(self, response, provider, calls):
        message = {"role": "assistant", "_content_tools_version": CONTENT_TOOLS_VERSION,
                   "content": response.content or "", "tool_calls": [
            {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": c.arguments}}
            for c in calls]}
        message.update({k: copy.deepcopy(v) for k, v in getattr(provider, "last_assistant_extra_fields", {}).items()
                        if k in ("reasoning_content", "reasoning_details", "reasoning")})
        message["_response_origin"] = response_origin(self.settings)
        message["_response_digest"] = message_digest(message)
        message["_context_epoch"] = self.context_epoch
        message["_native_epoch"] = self.native_epoch
        native = getattr(provider, "last_native_response", None)
        if native:
            message["_native_response"] = copy.deepcopy(native)
            message["_response_origin"] = copy.deepcopy(native["origin"])
        return message
