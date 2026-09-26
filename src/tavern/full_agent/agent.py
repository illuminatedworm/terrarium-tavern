"""One model, one bounded native tool loop, model-controlled window rollover."""
import copy
import time

import jsonpatch
import jsonpointer

from tavern.character.cards import lore_entries, normalize
from tavern.conversation.context import macros
from tavern.core.model_view import ModelView
from tavern.core.provider import make_provider
from tavern.core.tokens import count_text
from tavern.full_agent.context import compile_window, load_trace
from tavern.full_agent.lore import inject_lore, retain_lore
from tavern.full_agent.state import full_state
from tavern.full_agent.tools import AgentTools, encoded, tool_definitions


class FullAgent:
    def __init__(self, store, tid, document, session, history, snapshot, request, script, settings,
                 user_message, retrieval):
        self.store, self.tid, self.document = store, tid, document
        self.session = {**session, "messages": history}
        self.history, self.snapshot, self.request, self.script = history, snapshot, request, script
        self.settings, self.user_message, self.lane = settings, user_message, retrieval
        self.source_history = history + [user_message]
        self.state = full_state(snapshot)
        self.trace = load_trace(store, session["id"], history)
        self.continuation, self.new_trace = [], []
        self.definitions = tool_definitions(request, session)
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
        self.exiting = set()

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

    async def retrieve(self, query, *, source):
        visible = self.retain()
        settings = self.settings.model_copy(deep=True)
        if source == "agent":
            settings.retrieval.query_lookback = 0
            settings.retrieval.input_chars = max(settings.retrieval.input_chars, len(query))
        result = await self.lane.prepare(self.document, visible, query, self.snapshot, settings,
                                         self.session, self.tid, source=source)
        added, notes = inject_lore(self.document, self.session, visible, self.snapshot, query, settings,
                                   self.tid, self.user_message["id"], result, self.exiting)
        self.event("retrieval", {**{k: v for k, v in result.items() if k != "entries"}, "source": source,
                                 "injected": [r["id"] for r in added], "injection_diagnostics": notes})
        refs = {eid: ref for ref, eid in self.lore_refs().items()}
        return {"injected": [{"ref": refs[r["id"]], "title": r["title"]} for r in added],
                "already_in_context": [{"ref": refs[r["id"]], "title": r["title"]}
                                       for r in self.snapshot["lore_injections"] if r not in added
                                       and not r.get("manually_removed")][:20],
                "notice": "新条目已作为中途系统消息注入；已保留的条目不重复注入。"}

    def compile(self):
        self.retain()
        permissions = (f"本轮允许更新变量：{self.request.update_variables}；"
                       f"允许修改设定：{self.request.allow_content_edit or self.session['policy_effective'] != 'directed'}。")
        result = compile_window(self.document, self.session, self.history, self.snapshot, self.request.text,
            self.settings, self.definitions, trace=self.trace, continuation=self.continuation,
            input_id=self.user_message["id"], instruction=permissions + "\n" + self.request.regenerate_note,
            calls=self.calls)
        self.store.set_turn(self.tid, preview=encoded(result))
        if result["estimated_tokens"] > result["available_tokens"]:
            raise ValueError("Full Agent 工作窗口超过输入预算，未裁剪或发送。请在场景面板填写 checkpoint 并换新窗口，或调整模型预算。")
        return result

    def append(self, message, window_id):
        self.continuation.append(copy.deepcopy(message))
        self.new_trace.append({"window_id": window_id, "message": copy.deepcopy(message)})

    async def run(self, provider=None):
        provider = provider or await make_provider(self.settings,
            lambda payload: self.event("provider_request", payload))
        # Pinned Terrarium transport has an emergency tool-drop fallback even
        # with retries=0. This mode must never silently invoke that fallback.
        async def no_overflow_recovery(current, state):
            return None
        if hasattr(provider, "_recover_from_overflow"):
            provider._recover_from_overflow = no_overflow_recovery
        if hasattr(provider, "echo_reasoning"):
            provider.echo_reasoning = True
        try:
            if self.settings.full_agent_config.auto_retrieval:
                await self.retrieve(self.request.text, source="auto")
            self.store.event(self.tid, "input_ready", {"message": self.user_message,
                "lore_injections": self.snapshot.get("lore_injections", [])})
            for _ in range(self.settings.full_agent_config.max_steps):
                compiled = self.compile()
                self.calls += 1
                started = time.perf_counter()
                response = await provider.chat_complete(compiled["messages"], extra_body={
                    "tools": self.definitions, "tool_choice": "auto", "parallel_tool_calls": False})
                self.event("usage", {"usage": response.usage or {}, "calls": 1,
                                      "duration_s": time.perf_counter() - started})
                calls = getattr(provider, "last_tool_calls", []) or []
                self.event("full_agent_response", {"content": response.content or "",
                    "finish_reason": getattr(response, "finish_reason", None),
                    "tool_calls": [{"id": c.id, "name": c.name, "arguments": c.arguments} for c in calls]})
                if getattr(response, "finish_reason", None) in ("length", "content_filter"):
                    raise ValueError("Full Agent 回复未完整结束，本回合未提交；请检查输出上限或服务商响应")
                if not calls:
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
                        if call.name == "new_context" and len(calls) != 1:
                            raise ValueError("new_context 必须单独调用；当前批次没有换窗")
                        result = await self.tools.execute(call.name, call.arguments)
                    except (ValueError, KeyError, TypeError, jsonpatch.JsonPatchException, jsonpointer.JsonPointerException) as error:
                        result = {"error": self.model_view.text(str(error))[:2000]}
                    raw_result = encoded(result)
                    message = {"role": "tool", "tool_call_id": call.id, "content": raw_result}
                    if count_text(raw_result, self.settings) > self.settings.full_agent_config.tool_result_tokens:
                        ref = f"T{len(self.history)+2}.{len(self.new_trace)+1}"
                        message["content"] = encoded({"truncated": True, "ref": ref,
                            "notice": "结果已归档，请用 read_history 分页读取。"})
                        message["_archive_content"] = raw_result
                    self.append(message, window_id)
                    self.event("full_agent_tool", {**activity, "result": result,
                                                  "duration_s": time.perf_counter() - tool_started,
                                                  "message": call.name + (" · 参数/权限错误" if "error" in result else " · 完成")})
                if self.rolled:
                    self.continuation = []
                    self.rolled = False
                    self.retain()
                    self.event("context_rollover", {"window": self.state["window"]["number"],
                                                   "message": "Agent 已保存 checkpoint 并换新窗口"})
            raise ValueError("Full Agent 达到本回合模型调用上限，草稿未提交；换窗不会重置调用额度")
        finally:
            await provider.close()
