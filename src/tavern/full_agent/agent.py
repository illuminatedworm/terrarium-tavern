"""One model, one bounded native tool loop, model-controlled window rollover."""
import copy
import json
import time

import jsonpatch
import jsonpointer

from tavern.character.cards import lore_entries, normalize
from tavern.conversation.context import macros, wrap_lorebook
from tavern.core.model_view import ModelView
from tavern.core.provider import make_provider
from tavern.full_agent.context import compile_window, load_trace
from tavern.full_agent.lore import inject_lore, retain_lore
from tavern.full_agent.state import full_state
from tavern.full_agent.tools import AgentTools, encoded, tool_definitions


class FullAgent:
    def __init__(self, store, tid, document, session, history, snapshot, request, script, settings,
                 user_message, retrieval, progress=None):
        self.store, self.tid, self.document = store, tid, document
        self.session = {**session, "messages": history}
        self.history, self.snapshot, self.request, self.script = history, snapshot, request, script
        self.settings, self.user_message, self.lane = settings, user_message, retrieval
        self.source_history = history + [user_message]
        self.state = full_state(snapshot)
        self.trace = load_trace(store, session["id"], history)
        self.continuation, self.new_trace = [], []
        self.definitions = tool_definitions(request, session, settings.full_agent_config, settings=settings)
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
        self.calls, self.rolled, self.retrieval_query = 0, False, ""
        self.must_rollover = False
        self.exiting = set()
        self.patch_state_done = False
        self.resumed = progress is not None
        if progress:
            self.continuation = copy.deepcopy(progress["continuation"])
            self.new_trace = copy.deepcopy(progress["new_trace"])
            self.retrieval_query = progress["retrieval_query"]
            self.must_rollover = progress["must_rollover"]
            self.exiting = set(progress["exiting"])
            self.tools.read_sources = copy.deepcopy(progress["read_sources"])
            self.tools.note_reads = {tuple(key): True for key in progress["note_reads"]}
            if "patch_state_done" in progress:
                self.patch_state_done = progress["patch_state_done"]
            else:
                patch_ids = {call["id"] for item in self.new_trace if item["message"]["role"] == "assistant"
                             for call in item["message"].get("tool_calls", [])
                             if call["function"]["name"] == "patch_state"}
                self.patch_state_done = any(
                    item["message"].get("tool_call_id") in patch_ids
                    and json.loads(item["message"].get("content", "{}")).get("saved_to_draft")
                    for item in self.new_trace if item["message"]["role"] == "tool")

    async def save_progress(self):
        script_mirror = await self.script.call("snapshot")
        self.store.save_full_agent_progress(self.tid, {
            "user": self.user_message, "snapshot": self.snapshot, "document": self.document,
            "script_mirror": script_mirror,
            "continuation": self.continuation, "new_trace": self.new_trace,
            "retrieval_query": self.retrieval_query, "must_rollover": self.must_rollover,
            "patch_state_done": self.patch_state_done,
            "exiting": sorted(self.exiting), "read_sources": self.tools.read_sources,
            "note_reads": [list(key) for key in self.tools.note_reads],
        })

    def event(self, kind, payload):
        self.store.event(self.tid, kind, {"purpose": "full_agent", **payload})

    def expand(self, text):
        return macros(text, normalize(self.document), self.session["persona"], self.snapshot["state"])

    def lore_refs(self):
        refs = self.state.setdefault("lore_references", {})
        for entry in lore_entries(self.document, "writer"):
            if entry["id"] not in refs.values():
                refs[f"L{len(refs)+1}"] = entry["id"]
        return refs

    def retain(self):
        visible = self.history[self.state["window"]["start"]:]
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
        permissions = f"允许修改设定：{self.request.allow_content_edit or self.session['policy_effective'] != 'directed'}。"
        result = compile_window(self.document, self.session, self.history, self.snapshot, self.request.text,
            self.settings, self.definitions, trace=self.trace, continuation=self.continuation,
            input_id=self.user_message["id"], instruction=permissions + (
                "\n" + self.settings.regenerate_prompt + "\n" + self.request.regenerate_note
                if self.request.regenerate_note else ""),
            calls=self.calls, force_rollover=self.must_rollover,
            require_state_patch=self.request.update_variables)
        self.must_rollover = result["rollover_required"]
        if result["estimated_tokens"] > self.settings.context_tokens:
            self.event("context_warning", {"message": "Full Agent 请求估算超出模型上下文，仍保留完整文本"})
        self.store.set_turn(self.tid, preview=encoded(result))
        return result

    def append(self, message, window_id):
        self.continuation.append(copy.deepcopy(message))
        self.new_trace.append({"window_id": window_id, "message": copy.deepcopy(message)})

    async def run(self, provider=None):
        provider = provider or await make_provider(self.settings,
            lambda payload: self.event("provider_request", payload))
        if hasattr(provider, "echo_reasoning"):
            provider.echo_reasoning = True
        try:
            if self.settings.full_agent_config.auto_retrieval and not self.resumed:
                await self.retrieve(self.request.text, source="auto")
            self.store.event(self.tid, "input_ready", {"message": self.user_message,
                "lore_injections": self.snapshot.get("lore_injections", [])})
            for _ in range(self.settings.full_agent_config.max_steps):
                compiled = self.compile()
                if self.must_rollover and not any(t["function"]["name"] == "new_context" for t in self.definitions):
                    raise ValueError("上下文已满，但 new_context 已关闭。请在工具页启用它，或在场景页手动保存并换新窗口。")
                self.calls += 1
                started = time.perf_counter()
                tool_options = {
                    "tools": self.definitions,
                    "tool_choice": {"type": "function", "function": {"name": "new_context"}}
                    if self.must_rollover else "auto", "parallel_tool_calls": False} if self.definitions else {}
                response = await provider.chat_complete(compiled["messages"], extra_body=tool_options)
                self.event("usage", {"usage": response.usage or {}, "calls": 1,
                                      "duration_s": time.perf_counter() - started})
                calls = getattr(provider, "last_tool_calls", []) or []
                self.event("full_agent_response", {"content": response.content or "",
                    "finish_reason": getattr(response, "finish_reason", None),
                    "tool_calls": [{"id": c.id, "name": c.name, "arguments": c.arguments} for c in calls]})
                if getattr(response, "finish_reason", None) in ("length", "content_filter"):
                    raise ValueError("Full Agent 回复未完整结束，本回合未提交；请检查输出上限或服务商响应")
                if not calls:
                    if self.must_rollover:
                        self.event("full_agent_reply_rejected", {"message": "强制换窗期间的正文回复已否决"})
                        self.continuation.append({"role": "system", "content":
                            "刚才的正文回复已被运行时否决，未提交。必须单独调用 new_context 换窗后再继续。"})
                        continue
                    if self.request.update_variables and not self.patch_state_done:
                        self.event("full_agent_reply_rejected", {"message": "本轮尚未调用 patch_state，正文回复已否决"})
                        self.continuation.append({"role": "system", "content":
                            "刚才的正文回复未提交。本轮必须先成功调用一次 patch_state 更新 MVU 变量。"})
                        continue
                    text = response.content or ""
                    if not text.strip():
                        raise ValueError("Full Agent 没有返回故事正文，本回合未提交")
                    self.store.set_turn(self.tid, draft=text)
                    self.store.event(self.tid, "text", {"text": text})
                    return text
                if len(calls) > 8 or any(not c.id for c in calls) or len({c.id for c in calls}) != len(calls):
                    raise ValueError("模型返回了无效的工具调用序列")
                window_id = self.state["window"]["id"]
                assistant_seq = len(self.new_trace) + 1
                assistant = {"role": "assistant", "content": response.content or "", "tool_calls": [
                    {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": c.arguments}}
                    for c in calls]}
                assistant.update({k: v for k, v in getattr(provider, "last_assistant_extra_fields", {}).items()
                                  if k in ("reasoning_content", "reasoning_details", "reasoning")})
                self.append(assistant, window_id)
                if assistant["content"]:
                    self.event("full_agent_assistant", {"activity_id": f"text-{assistant_seq}",
                        "order": assistant_seq * 10, "content": assistant["content"]})
                for index, call in enumerate(calls):
                    activity = {"activity_id": f"call-{assistant_seq}-{index}",
                                "order": assistant_seq * 10 + index + 1,
                                "name": call.name, "arguments": call.arguments}
                    self.event("full_agent_tool_started", activity)
                    tool_started = time.perf_counter()
                    try:
                        if self.must_rollover and (call.name != "new_context" or len(calls) != 1):
                            raise ValueError("当前必须换窗；仅接受单独的 new_context，其他调用已否决且未执行")
                        if call.name == "new_context" and len(calls) != 1:
                            raise ValueError("new_context 必须单独调用；当前批次没有换窗")
                        result = await self.tools.execute(call.name, call.arguments)
                    except (ValueError, KeyError, TypeError, jsonpatch.JsonPatchException, jsonpointer.JsonPointerException) as error:
                        result = {"error": self.model_view.text(str(error))}
                    raw_result = encoded(result)
                    message = {"role": "tool", "tool_call_id": call.id, "content": raw_result}
                    self.append(message, window_id)
                    self.event("full_agent_tool", {**activity, "result": result,
                                                  "duration_s": time.perf_counter() - tool_started,
                                                  "message": call.name + (" · 参数/权限错误" if "error" in result else " · 完成")})
                if self.rolled:
                    self.continuation = []
                    self.rolled = False
                    self.must_rollover = False
                    self.retain()
                    self.event("context_rollover", {"window": self.state["window"]["number"],
                                                   "message": "Agent 已保存 checkpoint 并换新窗口"})
                await self.save_progress()
            raise ValueError("Full Agent 达到本回合模型调用上限，草稿未提交；换窗不会重置调用额度")
        finally:
            await provider.close()
