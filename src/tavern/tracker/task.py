"""One atomic Tracker draft and native tool loop shared by its memory batches.

The memory engine pauses this loop at accepted write_memory calls. Its next batch
or review is attached to that *unsent* tool result, preserving native tool pairs
and the already-sent prefix without another, competing model conversation.
"""
import asyncio
import copy
import json
import time
from dataclasses import dataclass

import jsonpatch
import jsonpointer

from tavern.character.cards import lore_entries, normalize
from tavern.conversation.context import get_path, search
from tavern.core.message_adapter import adapt_messages, provider_messages, runtime_messages
from tavern.core.model_view import ModelView, state_for_model
from tavern.core.provider import make_provider
from tavern.core.tokens import request_tokens
from tavern.memories.history_recall import HistoryBudget, HistoryRecall, MemoryText
from tavern.memories.memory_engine import apply_operations, consolidate
from tavern.memories.memory_protocol import References
from tavern.memories.memory_records import read_memory, with_source_status
from tavern.scripting.request_context import OperationContexts, configure_operation
from tavern.tracker.context import initial_messages
from tavern.tracker.tools import SPECS, definitions


@dataclass
class TaskBudget:
    limit: int
    calls: int = 0

    def reserve(self):
        if self.calls >= self.limit:
            raise ValueError("Tracker 达到本次任务的模型调用上限；草稿未提交")
        self.calls += 1


class TrackerTask:
    def __init__(self, store, tid, settings, session, document, history, snapshot, *, phase,
                 user_input="", narrative="", memory=False, variables=False, content=False,
                 budget=None, history_budget=None, committed_history=None, contexts=None, provider=None):
        self.store, self.tid = store, tid
        self.settings = settings.for_role("background")
        if memory:
            self.settings = self.settings.model_copy(update={"reply_tokens": settings.memory_output_tokens})
        self.session, self.persona = session, session["persona"]
        self.document, self.snapshot = copy.deepcopy(document), copy.deepcopy(snapshot)
        self.history = copy.deepcopy(history)
        self.committed_history = copy.deepcopy(history if committed_history is None else committed_history)
        self.phase, self.purpose = phase, "tracker_" + phase
        self.user_input, self.narrative = user_input, narrative
        self.memory_enabled, self.variables_enabled, self.content_enabled = memory, variables, content
        self.memory = with_source_status(read_memory(snapshot), history)
        self.references = References()
        self.view = ModelView({"document": document, "history": history, "input": user_input})
        self.budget = budget or TaskBudget(settings.tracker_max_steps)
        self.history_budget = history_budget or HistoryBudget()
        self.definitions = definitions(memory=memory, variables=variables, content=content)
        self.allowed = {t["function"]["name"] for t in self.definitions}
        self.contexts = contexts or OperationContexts(store, session["id"], tid)
        self.provider = provider
        self.owns_provider = provider is None
        self.thread = []
        self.batch = None
        self.recall = None
        self.patch = None
        self.patch_reason = ""
        self.accepted = None
        self.write_result = None
        self.summary = ""
        self.variables = copy.deepcopy(state_for_model(snapshot.get("state", {})).get("stat_data", {}))
        self.raw_variables = copy.deepcopy(snapshot.get("state", {}).get("stat_data", {}))
        self.completed = False
        self.total_calls = 0
        self.manual_batches = False
        self.current_ready = True
        self.sent_count = 0
        self.output_tokens = self.settings.reply_tokens
        self.retrieval = None

    def event(self, kind, payload):
        self.store.event(self.tid, kind, {**payload, "purpose": self.purpose})

    def encoded(self, value):
        return json.dumps(self.view.expose(value), ensure_ascii=False)

    def runtime_update(self, fields):
        """Fill an unsent tool result, or append a frame after a failed review.

        A failed review may already have transmitted the preceding write result;
        never rewrite that prefix when returning to the other task duties.
        """
        position = next((i for i, message in enumerate(self.thread) if message is self.write_result), -1)
        if position >= self.sent_count:
            result = json.loads(self.write_result["content"])
            result.update(self.view.expose(fields))
            self.write_result["content"] = json.dumps(result, ensure_ascii=False)
        else:
            self.thread.append({"role": "system", "message_type": "tracker_task_update", "runtime_phase": "body", "content": self.encoded({"tracker_task_update": fields})})

    async def start(self):
        if self.provider is None:
            self.provider = await make_provider(self.settings,
                lambda value: self.event("provider_request", value), request_timeout=self.settings.memory_timeout)
        configure_operation(self.provider, self.contexts.new(self.purpose), self.event, self.purpose)
        if hasattr(self.provider, "echo_reasoning"):
            self.provider.echo_reasoning = True
        self.event("tracker_started", {"phase": self.phase, "memory": self.memory_enabled,
                                       "variables": self.variables_enabled, "content": self.content_enabled})

    async def close(self):
        if self.owns_provider and self.provider is not None:
            await self.provider.close()

    def _new_recall(self):
        history = self.committed_history
        if self.batch is not None:
            fresh = self.batch.context["fresh_ids"]
            cutoff = max((i + 1 for i, m in enumerate(self.history) if m["id"] in fresh), default=0)
            history = history[:cutoff]
        self.recall = HistoryRecall(history, self.settings, self.history_budget, document=self.document,
                                    persona=self.persona, event=self.event, references=self.references)

    async def generate_memory(self, request, *, phase="memory", output_tokens=None):
        """Adapter for the existing source-aware engine; all I/O is tool-driven."""
        self.batch, self.references = request, request.references
        self.output_tokens = output_tokens or self.settings.reply_tokens
        self.current_ready = not self.history or self.history[-1]["id"] in request.context["fresh_ids"]
        if self.manual_batches and phase == "memory":
            self.budget.calls = 0
            if self.thread:
                # Manual rebuilds can span an entire novel. Carry structured
                # memory into a fresh batch context, not every prior raw page.
                # Reviews stay in the same batch; history quota remains shared.
                self.thread, self.write_result, self.sent_count = [], None, 0
                configure_operation(self.provider, self.contexts.new(self.purpose), self.event, self.purpose)
        self.memory = copy.deepcopy(request.context["memory"])
        self.accepted = None
        self._new_recall()
        payload = json.loads(request[-1]["content"])
        payload["mode"] = "review" if phase == "memory_review" else "consolidate"
        payload["current_turn_available"] = self.current_ready
        if self.current_ready:
            payload["current_turn"] = {"user": self.user_input, "assistant": self.narrative}
            payload["current_variables"] = self.view.expose(self.variables)
        payload["notice"] = ("本批编号取代此前批次的编号。复核仅允许修订指定待确认条目，不能再次提出问题。"
                             if phase == "memory_review" else "本批编号取代此前批次的编号；完成后调用 write_memory。")
        if not self.thread:
            self.thread = initial_messages(self)
            # Merge batch data into the first task input instead of installing
            # memory_engine's legacy JSON-output system contract.
            initial = json.loads(self.thread[-1]["content"])
            initial.pop("previous_result", None)
            initial["memory_batch"] = self.view.expose(payload)
            self.thread[-1]["content"] = json.dumps(initial, ensure_ascii=False)
            self.thread[-1]["runtime_content"] = self.thread[-1]["content"]
        elif self.write_result is not None:
            self.runtime_update({"next_memory_task": payload})
        else:
            raise ValueError("Tracker 记忆批次缺少配对的工具结果")
        await self.loop(stop_on_memory=True)
        return self.accepted

    async def run(self, *, memory_start=None, memory_budget=None):
        self.manual_batches = bool(memory_budget and memory_budget.manual)
        try:
            await self.start()
            if memory_start is not None:
                result = await consolidate(self.history, self.settings, self.generate_memory,
                    memory=self.memory, start=memory_start, document=self.document, persona=self.persona,
                    budget=memory_budget, event=self.event)
                self.memory = result
                self.snapshot["memory_document"] = copy.deepcopy(result)
                self.batch = None
                self.output_tokens = self.settings.reply_tokens
                if self.write_result is not None:
                    self.runtime_update({"memory_complete": True,
                        "remaining_tasks": ["patch_state"] if self.variables_enabled and self.patch is None else []})
            if not self.thread:
                self.thread = initial_messages(self)
            if (self.budget.calls >= self.budget.limit and self.phase != "pre" and memory_start is not None
                    and (not self.variables_enabled or self.patch is not None)):
                self.summary, self.completed = "必要工具写入已完成；模型调用额度已用尽。", True
            else:
                await self.loop()
            self.event("tracker_completed", {"phase": self.phase, "record": self.summary,
                                              "calls": self.total_calls})
            return self
        finally:
            await self.close()

    async def loop(self, *, stop_on_memory=False):
        while True:
            self.budget.reserve()
            self.total_calls += 1
            typed = runtime_messages(self.thread, "background")
            tokens = request_tokens(adapt_messages(typed, self.settings)[0], self.settings, self.definitions)
            if tokens + self.output_tokens > self.settings.context_tokens:
                self.event("context_warning", {"message": "Tracker 请求估算超出上下文，仍完整发送"})
            begun = time.perf_counter()
            self.sent_count = len(self.thread)
            response = await asyncio.wait_for(self.provider.chat_complete(provider_messages(self.provider, typed, self.settings), max_tokens=self.output_tokens, extra_body={
                "tools": self.definitions, "tool_choice": "auto", "parallel_tool_calls": False}),
                self.settings.memory_timeout)
            self.event("usage", {"calls": 1, "usage": response.usage or {},
                                  "duration_s": time.perf_counter() - begun})
            if getattr(response, "finish_reason", None) in ("length", "content_filter"):
                raise ValueError("Tracker 回复未完整结束，草稿未提交")
            calls = getattr(self.provider, "last_tool_calls", []) or []
            if len(calls) > 8 or any(not c.id for c in calls) or len({c.id for c in calls}) != len(calls):
                raise ValueError("Tracker 工具调用序列无效")
            assistant = {"role": "assistant", "content": response.content or ""}
            if calls:
                assistant["tool_calls"] = [{"id": c.id, "type": "function",
                    "function": {"name": c.name, "arguments": c.arguments}} for c in calls]
            assistant.update({k: copy.deepcopy(v) for k, v in
                getattr(self.provider, "last_assistant_extra_fields", {}).items() if k in (
                    "reasoning_content", "reasoning_details", "reasoning", "_native_response",
                    "_response_origin", "_response_digest")})
            self.thread.append(assistant)
            if not calls:
                missing = (["write_memory"] if stop_on_memory and self.accepted is None else [])
                if self.variables_enabled and self.patch is None:
                    missing.append("patch_state")
                if missing:
                    self.thread.append({"role": "system", "message_type": "required_tool_notice", "runtime_phase": "body", "content": "必要任务尚未完成，请调用：" + ", ".join(missing)})
                    continue
                self.summary, self.completed = response.content or "", True
                return
            for call in calls:
                tool_error = False
                try:
                    result = await self.execute_tool(call.name, json.loads(call.arguments))
                    # Recall returns its own validation/quota failures. Other
                    # read tools may legitimately return a user field named error.
                    tool_error = call.name == "tavern_recall_history" and "error" in result
                except (ValueError, TypeError, KeyError, jsonpatch.JsonPatchException,
                        jsonpointer.JsonPointerException) as error:
                    result = {"error": self.view.text(str(error))}
                    tool_error = True
                message = {"role": "tool", "tool_call_id": call.id, "content": self.encoded(result)}
                if tool_error:
                    message["_tool_error"] = True
                self.thread.append(message)
                self.event("tracker_tool", {"name": call.name, "arguments": call.arguments, "result": result})
                if call.name == "write_memory" and "error" not in result:
                    self.write_result = message
            if stop_on_memory and self.accepted is not None:
                return
            if (not stop_on_memory and self.phase != "pre" and self.patch is not None
                    and self.budget.calls >= self.budget.limit):
                self.summary, self.completed = "变量补丁已暂存；模型调用额度已用尽。", True
                return

    async def execute_tool(self, name, arguments):
        if name == "search_lore" and self.retrieval is not None:
            query = self.view.restore(SPECS[name][0].model_validate(arguments).query)
            if query != "*":
                selected = await self.retrieval.prepare(self.document, self.committed_history, query,
                    self.search_snapshot(), self.settings, self.session, self.tid)
                ids = set(selected["ids"])
                return [{"name": e.get("comment", ""), "content": e["content"]}
                        for e in selected["entries"] if e["enabled"] and e["id"] in ids]
        return self.execute(name, arguments)

    def search_snapshot(self):
        snapshot = copy.deepcopy(self.snapshot)
        if self.patch is not None:
            snapshot.setdefault("state", {})["stat_data"] = copy.deepcopy(self.raw_variables)
        return snapshot

    def execute(self, name, arguments):
        if name not in self.allowed:
            raise ValueError("本次 Tracker 任务不允许调用该工具")
        if name == "tavern_recall_history":
            if self.recall is None:
                self._new_recall()
            return self.recall.execute(self.view.restore(arguments))
        model = SPECS[name][0].model_validate(arguments)
        if name == "read_character":
            return self.view.expose(get_path(normalize(self.document), self.view.restore(model.field)))
        if name == "read_state":
            return self.view.expose(get_path(self.variables, self.view.restore(model.path)))
        if name == "search_lore":
            entries = [e for e in lore_entries(self.document) if e["enabled"]]
            if model.query == "*":
                matches = entries
            else:
                from tavern.search.retrieval import fuse, lexical_recall
                result = lexical_recall(self.document, self.committed_history, self.view.restore(model.query),
                                        self.search_snapshot(), self.settings, self.session, self.tid)
                ids = set(fuse(result["rankings"], result["entries"], self.settings.retrieval))
                matches = [e for e in result["entries"] if e["enabled"] and e["id"] in ids]
            return [{"name": e.get("comment", ""), "content": e["content"]} for e in matches]
        if name == "search_memory":
            records = self.references.previous(self.memory)["records"]
            records = [r for r in records if r["status"] not in ("deleted", "superseded")]
            return records if model.query == "*" else search(records, self.view.restore(model.query))
        if name == "revise_content":
            if not self.current_ready:
                raise ValueError("请先完成历史记忆批次，等待本轮资料后再修改设定")
            data = self.view.patch(normalize(self.document), model.operations)
            if not isinstance(data, dict) or not isinstance(data.get("name"), str) or not data["name"].strip():
                raise ValueError("设定必须保留有效角色名称")
            if self.document.get("spec"):
                self.document["data"] = data
            else:
                self.document = data
            self.snapshot["content"] = {"document": copy.deepcopy(self.document),
                "reason": self.view.restore(model.comment), "turn": self.tid}
            self.event("content_proposal", {"operations": model.operations, "reason": model.comment})
            return {"saved_to_draft": True, "notice": "对话级角色卡草稿已更新，回合提交后生效。"}
        if name == "patch_state":
            if not self.current_ready:
                raise ValueError("请先完成历史记忆批次，等待本轮正文后再更新变量")
            if self.patch is not None:
                raise ValueError("本轮变量补丁已接受，不能重复提交")
            from tavern.tracker.state import stage
            operations, candidate = stage(self.raw_variables, self.view.operations(model.operations))
            self.patch, self.patch_reason = operations, self.view.restore(model.comment)
            self.raw_variables = candidate
            self.variables = state_for_model({"stat_data": candidate})["stat_data"]
            self.event("state_proposal", {"operations": operations, "reason": self.patch_reason, "staged": True})
            return {"saved_to_draft": True, "notice": "变量补丁已暂存，待最终 MVU 与脚本校验。"}
        if name == "write_memory":
            return self.write_memory(self.view.restore(model.model_dump(exclude_unset=True, exclude_none=True)))
        raise ValueError("未知 Tracker 工具")

    def write_memory(self, arguments):
        if self.batch is None or self.accepted is not None:
            raise ValueError("当前没有待提交的记忆批次；已处理批次不能重复提交")
        arguments.setdefault("questions", [])
        context = self.batch.context
        parsed = self.references.resolve(arguments,
            on_invalid=lambda p: self.event("memory_records_discarded", p))
        sources = self.recall.sources if self.recall else {}
        expanded = copy.deepcopy(context["expanded"])
        from tavern.memories.memory_engine import _merge_history_sources
        _merge_history_sources(expanded, sources)
        suppressed = set()
        preview = apply_operations(context["memory"], parsed, context["originals"], expanded,
            context["evidence"], fresh_ids=context["fresh_ids"], settings=self.settings,
            review_ids=context["review_ids"], suppressed_ids=suppressed, defer_retention=True)
        questions = parsed["questions"]
        if context["review_ids"] is not None and questions:
            raise ValueError("复核不能再次请求核验")
        deleted = {r["id"] for r in preview["records"] if r["status"] == "deleted"}
        questions = [q for q in questions if not set(q["record_ids"]) & (suppressed | deleted)]
        active = {r["id"] for r in preview["records"] if not r.get("manual") and r["status"] == "active"}
        for question in questions:
            if not set(question["record_ids"]) <= active:
                raise ValueError("疑难问题必须引用实际存在的非手工有效记忆")
            for record in preview["records"]:
                if record["id"] in question["record_ids"]:
                    record["category"] = "uncertain"
        parsed["questions"] = questions
        accepted = MemoryText(json.dumps(arguments, ensure_ascii=False), sources)
        accepted.resolved = parsed
        self.accepted, self.memory = accepted, preview
        self.event("memory_proposal", {"operations": len(parsed["operations"]), "questions": len(questions)})
        return {"saved_to_draft": True, "operations": len(parsed["operations"]),
                "notice": "本批记忆已接受；程序将提供下一批或复核，全部成功后才推进覆盖。"}
