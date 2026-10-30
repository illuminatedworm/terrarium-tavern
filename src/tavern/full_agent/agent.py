"""One model, one bounded native tool loop, model-controlled window rollover."""

import asyncio
import copy
import json
import time

import jsonpatch
import jsonpointer

from tavern.character.cards import card_view, lore_entries, lore_title
from tavern.conversation.cache_breakpoint import model_messages
from tavern.conversation.context import macros, wrap_lorebook
from tavern.conversation.regeneration import reply_for_regeneration
from tavern.conversation.variable_rules import collect_variable_rules
from tavern.core.cache_policy import fingerprint
from tavern.core.context_usage import exceeds
from tavern.core.history_refs import message_ref
from tavern.core.message_adapter import provider_messages
from tavern.core.model_view import ModelView
from tavern.core.provider import make_provider
from tavern.full_agent.content import lore_references
from tavern.full_agent.content_history import CONTENT_TOOLS_VERSION, content_history, retired_content_call
from tavern.full_agent.context import compile_window, load_trace
from tavern.full_agent.lore import inject_lore, retain_lore
from tavern.full_agent.preview import TurnPreview
from tavern.full_agent.protocol import ToolError, TurnError, failed, success
from tavern.full_agent.state import full_state
from tavern.full_agent.story import LEGACY_TURN_TOOLS, story_history
from tavern.full_agent.tools import AgentTools, encoded, tool_definitions
from tavern.full_agent.visibility import context_messages
from tavern.scripting.lore import merge_script_lore, sync_runtime_lore
from tavern.scripting.native_response import completion_message, message_digest, portable
from tavern.scripting.request_context import ProviderRequestContext, response_origin

# Each executed call checkpoints the whole draft, so a runaway relay batch costs
# quadratic I/O. Legitimate story maintenance fits comfortably within this bound.
MAX_BATCH_CALLS = 32


class FullAgent:
    def __init__(
        self,
        store,
        tid,
        document,
        session,
        history,
        snapshot,
        request,
        script,
        settings,
        user_message,
        retrieval,
        progress=None,
        *,
        output_id: str,
        script_mirror=None,
        novel=None,
    ):
        self.store, self.tid, self.document = store, tid, document
        self.novel, self.novel_operation = novel, ""
        self.session = {**session, "messages": history}
        self.history, self.snapshot, self.request, self.script = history, snapshot, request, script
        self.regenerate_reply = reply_for_regeneration(request, session)
        self.settings, self.user_message, self.lane = settings, user_message, retrieval
        self.output_id = output_id
        self.source_history = history + [user_message]
        self.state = full_state(snapshot)
        self.exiting = set()
        if not progress and script_mirror and script_mirror.get("loreModified"):
            # Initialization callbacks precede the model. Merge their authored
            # changes before freezing rules and constructing the model view.
            self.apply_script_lore(script_mirror["lore"], script_mirror, include_existing=True)
            document = self.document
        self.trace = load_trace(store, session["id"], history)
        self.continuation, self.new_trace = [], []
        self.definitions = tool_definitions(
            request, session, settings.full_agent_config, settings=settings, document=document
        )
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
        self.recall_ids = []
        self.patch_state_done = False
        # Keep macro values and rule edits stable across tools, rollovers and
        # failed-request resumes. The next turn takes a fresh rule snapshot.
        self.variable_rules = (
            copy.deepcopy(progress["variable_rules"])
            if progress is not None and "variable_rules" in progress
            else collect_variable_rules(
                document, session["persona"], snapshot["state"], snapshot.get("scopes")
            )
            if request.update_variables
            else None
        )
        self.resumed = progress is not None
        self.story = copy.deepcopy((progress or {}).get("story"))
        if self.story is not None:
            self.source_history.append(
                {
                    "id": self.output_id,
                    "role": "assistant",
                    "raw": self.story["content"],
                }
            )
        self.model_completed = (progress or {}).get("model_completed", False)
        self.resume_reason = (progress or {}).get("resume_reason", "")
        self.legacy_protocol = (progress or {}).get("legacy_protocol", False)
        self.preview = TurnPreview(store, tid)
        self.context_epoch = ""
        self.native_epoch = ""
        saved_context = (progress or {}).get("request_context", {})
        self.request_context = ProviderRequestContext(
            session["id"],
            saved_context.get("turn_id", request.resume_turn_id or tid),
            self.state["window"]["id"],
            store.device_id(),
            routing=copy.deepcopy(saved_context.get("routing", {})),
            sent_breakpoints=store.cache_breakpoint_history(session["id"]),
        )
        if progress:
            self.continuation, retired = content_history(progress["continuation"])
            _, retired_story = story_history(self.continuation)
            if retired or retired_story:
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

    async def save_progress(self, *, script_mirror=None):
        if script_mirror is None:
            script_mirror = await self.script.call("snapshot")
        self.store.save_full_agent_progress(
            self.tid,
            {
                "version": 4,
                "user": self.user_message,
                "output_id": self.output_id,
                "snapshot": self.snapshot,
                "document": self.document,
                "script_mirror": script_mirror,
                "continuation": self.continuation,
                "new_trace": self.new_trace,
                "retrieval_query": self.retrieval_query,
                "must_rollover": self.must_rollover,
                "patch_state_done": self.patch_state_done,
                "story": self.story,
                "model_completed": self.model_completed,
                "resume_reason": self.resume_reason,
                "legacy_protocol": self.legacy_protocol,
                "variable_rules": self.variable_rules,
                "exiting": sorted(self.exiting),
                "tool_reads": copy.deepcopy(self.tools.reads.records),
                "request_context": self.request_context.saved(),
                "provider_origin": response_origin(self.settings),
            },
        )

    async def stop_for_user(self, code, reason):
        """A terminal response never becomes an automatic paid correction loop."""
        self.model_completed = False
        self.resume_reason = reason
        self.preview.replace(self.story["content"] if self.story else "")
        await self.save_progress()
        raise TurnError(code, reason + "；草稿未提交，继续时保留已完成的工作")

    async def finished_text(self, *, batch_failed=False):
        missing = []
        if self.story is None:
            missing.append(
                "模型已结束，但本轮尚未提交非空白正文；请调用 submit_story"
                if not self.settings.kemini.enabled
                else "模型已结束，但本轮尚未输出非空白正文"
            )
        if self.must_rollover:
            missing.append("当前上下文须先完成 new_context 换窗")
        if self.request.update_variables and not self.patch_state_done:
            missing.append("本轮要求的 patch_state 尚未成功")
        if self.session.get("plot_enabled", False) and not self.state["plot"]["text"].strip():
            missing.append("剧情规划已开启，但尚未建立初始大纲")
        if batch_failed:
            missing.append("本次结束响应包含失败的工具调用")
        if missing:
            await self.stop_for_user("TURN_NOT_READY", "；".join(missing))
        return self.story["content"]

    def story_candidate(self, text, calls):
        """The visible Kemini experiment is the only alternate text transport."""
        auxiliary, transport_id = [], None
        if self.settings.kemini.enabled:
            from tavern.scripting.kemini import AuxiliaryParser

            transports = [call for call in calls if call.name == "kemini_emit_story"]
            if transports:
                if len(transports) != 1:
                    raise TurnError("STORY_ALREADY_RECEIVED", "同一响应包含多次实验正文输出")
                call = transports[0]
                args = json.loads(call.arguments)
                if (
                    not isinstance(args, dict)
                    or set(args) != {"content"}
                    or not isinstance(args["content"], str)
                ):
                    raise TurnError("TOOL_PROTOCOL_INVALID", "实验正文传输需要且仅接受字符串 content")
                if text.strip():
                    auxiliary.append({"kind": "extra", "text": text})
                text, transport_id = args["content"], call.id
            parser = AuxiliaryParser(self.settings.kemini)
            text = parser.feed(text, final=True)
            auxiliary.extend(parser.parts)
            if transport_id and not text.strip():
                raise TurnError("EMPTY_STORY", "实验输出未包含故事正文")
        return text, auxiliary, transport_id

    async def receive_story(self, text, auxiliary):
        if self.story is not None:
            if not self.settings.kemini.enabled:
                raise ToolError("STORY_ALREADY_RECEIVED", "本轮正文已接收，不允许再次提交")
            await self.stop_for_user("STORY_ALREADY_RECEIVED", "本轮已输出正文，第二次正文未接收")
        if self.must_rollover:
            await self.stop_for_user("CONTEXT_ROLLOVER_REQUIRED", "当前须先换窗，本次正文未接收")
        mirror = await self.script.call("snapshot")
        mirror["messages"][-1]["message"] = text
        await self.script.call("update", mirror)
        self.story = {"content": text, "auxiliary": auxiliary}
        self.source_history.append({"id": self.output_id, "role": "assistant", "raw": text})
        self.preview.replace(text)
        if auxiliary:
            self.event("kemini_auxiliary", {"items": auxiliary})
        return {"input_id": self.user_message["id"], "content_hash": fingerprint(text)}

    def event(self, kind, payload):
        self.store.event(self.tid, kind, {"purpose": "full_agent", **payload})

    def apply_script_lore(self, initial_lore, processed, *, include_existing=False):
        if not self.request.allow_content_edit:
            raise TurnError("CONTENT_EDIT_NOT_ALLOWED", "卡片脚本尝试修改设定，但本轮未授权；草稿未提交")
        try:
            revised = merge_script_lore(
                self.document, initial_lore, processed, include_existing=include_existing
            )
            if revised == self.document:
                return
            old = {entry["id"]: entry for entry in lore_entries(self.document)}
            current = {entry["id"]: entry for entry in lore_entries(revised)}
        except (ValueError, KeyError, TypeError) as error:
            # A successful callback has already changed its realms. Do not let
            # the tool loop continue with a worldbook that failed to merge.
            raise TurnError("SCRIPT_LORE_INVALID", "角色脚本修改世界书失败，停止回合；草稿未提交") from error
        changed = {key for key in old if old[key] != current.get(key)}
        self.snapshot["lore_injections"] = [
            row for row in self.snapshot.get("lore_injections", []) if row["id"] not in changed
        ]
        for key in changed:
            self.snapshot.get("lore", {}).pop(key, None)
        self.exiting.difference_update(changed)
        self.document = revised
        self.snapshot["content"] = {"document": revised, "reason": "角色脚本修改世界书", "turn": self.tid}

    def expand(self, text):
        return macros(
            text,
            card_view(self.document),
            self.session["persona"],
            self.snapshot["state"],
            self.snapshot.get("scopes"),
        )

    def lore_refs(self):
        return lore_references(self.document, self.state)

    def retain(self):
        window = self.state["window"]
        compact = self.settings.context_strategy == "openai_compact"
        visible = self.history if compact else self.history[window["start"] :]
        owners = {m["id"] for m in visible}
        records = [
            item["message"]
            for item in self.trace
            if item["message_id"] in owners
            and (compact or window.get("replay_history") or item["window_id"] == window["id"])
        ]
        recalled = {
            eid
            for message in [*records, *self.continuation]
            if message["role"] == "tool"
            for eid in message.get("lore_ids", [])
        }
        self.exiting |= retain_lore(
            self.document,
            self.snapshot,
            visible,
            self.user_message["id"],
            sum(m["role"] == "user" for m in self.history) + 1,
            recalled=recalled,
        )
        return visible

    async def retrieve(self, query, *, source, include_ref=False, include_title=False):
        visible = self.retain()
        settings = self.settings.model_copy(deep=True)
        if source == "agent":
            settings.retrieval.query_lookback = 0
        result = await self.lane.prepare(
            self.document, visible, query, self.snapshot, settings, self.session, self.tid, source=source
        )
        added, notes = inject_lore(
            self.document,
            self.session,
            visible,
            self.snapshot,
            query,
            settings,
            self.tid,
            self.user_message["id"],
            result,
            self.exiting,
            persist=source == "auto",
        )
        self.event(
            "retrieval",
            {
                **{k: v for k, v in result.items() if k != "entries"},
                "source": source,
                "injected": [r["id"] for r in added] if source == "auto" else [],
                "tool_results": [r["id"] for r in added] if source == "agent" else [],
                "injection_diagnostics": notes,
            },
        )
        refs = {eid: ref for ref, eid in self.lore_refs().items()}
        matches = [
            {
                **({"ref": refs[r["id"]]} if include_ref else {}),
                **({"title": r["title"]} if include_title else {}),
                "text": wrap_lorebook(r["content"]),
            }
            for r in added
        ]
        result = {"matches": matches}
        if include_ref:
            entries = {e["id"]: e for e in lore_entries(self.document, "writer")}
            omitted = {r["id"] for r in added} | {
                r["id"] for r in self.snapshot.get("lore_injections", []) if r.get("manually_removed")
            }
            result["already_in_context"] = [
                {
                    "ref": refs[eid],
                    **({"title": lore_title(entries[eid]) or "世界书"} if include_title else {}),
                }
                for eid, old in self.snapshot.get("lore", {}).items()
                if old.get("present") and eid not in omitted
            ][:20]
        if source == "agent":
            self.recall_ids = [r["id"] for r in added]
            for eid in self.recall_ids:
                self.snapshot.setdefault("lore", {}).setdefault(eid, {}).update(present=True)
        return result

    def compile(self):
        self.retain()
        result = compile_window(
            self.document,
            self.session,
            self.history,
            self.snapshot,
            self.request.text,
            self.settings,
            self.definitions,
            trace=self.trace,
            continuation=self.continuation,
            input_id=self.user_message["id"],
            instruction=(
                self.settings.regenerate_prompt + "\n" + self.request.regenerate_note
                if self.request.regenerate_note
                else ""
            ),
            force_rollover=self.must_rollover,
            require_state_patch=self.request.update_variables,
            variable_rules=self.variable_rules,
            allow_content_edit=self.request.allow_content_edit,
            regenerate_reply=self.regenerate_reply,
            story=self.story,
        )
        self.must_rollover = result["rollover_required"]
        self.context_epoch = result["context_ledger"]["epoch"]
        self.native_epoch = result["context_ledger"]["native_epoch"]
        self.request_context.window_id = self.state["window"]["id"]
        self.request_context.diagnostics = copy.deepcopy(result["context_ledger"])
        self.tools.reads.observe_context(context_messages(result))
        for message in result["context_additions"]:
            self.append(message, self.state["window"]["id"])
        if result["context_ledger"]["rebuild_reason"]:
            self.event("context_baseline", result["context_ledger"])
        if exceeds(result["context_usage"], self.settings.context_tokens):
            self.event(
                "context_warning",
                {"message": "Full Agent 上次实测输入超过窗口上限；新增内容尚未计量，仍保留完整文本"},
            )
        self.store.set_turn(self.tid, preview=encoded(result))
        return result

    def append(self, message, window_id):
        message = {**message, "_context_epoch": self.context_epoch}
        self.continuation.append(copy.deepcopy(message))
        self.new_trace.append({"window_id": window_id, "message": copy.deepcopy(message)})

    async def run(self, provider=None, *, provider_factory=None, request_observer=None):
        try:
            if self.settings.full_agent_config.auto_retrieval and not self.resumed:
                await self.retrieve(self.request.text, source="auto")
            self.store.event(
                self.tid,
                "input_ready",
                {"message": self.user_message, "lore_injections": self.snapshot.get("lore_injections", [])},
            )
            if self.resumed:
                if self.story is not None:
                    self.preview.replace(self.story["content"])
                await self.resume_tools()
                if self.model_completed:
                    return await self.finished_text()
                if self.resume_reason:
                    self.append(
                        {
                            "role": "system",
                            "content": "上次回合未提交：" + self.resume_reason,
                            "message_type": "reply_correction",
                            "runtime_phase": "body",
                        },
                        self.state["window"]["id"],
                    )
                    self.resume_reason = ""
            # A saved natural end needs only local finalization. Do not
            # refresh credentials or create a network client to replay it.
            provider = provider or (
                provider_factory()
                if provider_factory
                else await make_provider(
                    self.settings,
                    lambda payload: self.event("provider_request", payload),
                    request_context=self.request_context,
                )
            )
            provider.request_observer = request_observer
            provider.operation_purpose = "narrative"
            provider.auxiliary_capture = lambda items: self.event("kemini_auxiliary", {"items": items})
            if hasattr(provider, "request_context"):
                provider.request_context = self.request_context
                self.request_context.bind(response_origin(provider.settings, provider.key))
            if hasattr(provider, "echo_reasoning"):
                provider.echo_reasoning = True
            for step in range(self.settings.full_agent_config.max_steps):
                if self.novel and self.session.get("novel_enabled"):
                    await self.novel.deliver(self)
                compiled = self.compile()
                if (
                    self.settings.context_strategy == "openai_compact"
                    and self.settings.compact_auto
                    and (
                        compiled["_compact_stale"]
                        or step == 0
                        and exceeds(compiled["context_usage"], self.settings.context_tokens)
                    )
                ):
                    # Tools may change fixed sources mid-turn. Repair stale
                    # checkpoints before the next actual request, while keeping
                    # pressure-based compaction at the existing turn boundary.
                    from tavern.conversation.compaction import prepare_turn

                    async def save_replay(value):
                        self.state["compact_replay"] = value
                        self.event(
                            "context_replay", {key: value[key] for key in ("batches", "processed", "total")}
                        )
                        self.event(
                            "usage", {"usage": value["usage"][-1], "calls": 1, "context_maintenance": True}
                        )
                        await self.save_progress()

                    checkpoint = await prepare_turn(
                        compiled,
                        self.settings,
                        provider,
                        progress=self.state.get("compact_replay"),
                        save=save_replay,
                    )
                    self.state["compaction"] = checkpoint
                    self.state.pop("compact_replay", None)
                    await self.save_progress()
                    compiled = self.compile()
                if self.must_rollover and not any(
                    t["function"]["name"] == "new_context" for t in self.definitions
                ):
                    raise ValueError(
                        "上下文已满，但 new_context 已关闭。请在工具页启用它；已有交接覆盖退出范围时，也可在场景页手动换新窗口。"
                    )
                started = time.perf_counter()
                tool_options = (
                    {
                        "tools": self.definitions,
                        "tool_choice": {"type": "function", "function": {"name": "new_context"}}
                        if self.must_rollover
                        else "auto",
                        "parallel_tool_calls": self.settings.full_agent_config.parallel_tool_calls
                        and not self.must_rollover,
                    }
                    if self.definitions
                    else {}
                )
                # Persist the actual runtime frames even when the very first
                # model request fails. Resume then reuses them instead of guessing.
                await self.save_progress()
                if self.story is None:
                    self.preview.replace("")
                try:
                    messages = provider_messages(provider, model_messages(compiled), self.settings)
                    complete = getattr(provider, "chat_complete_with_preview", None)
                    if complete:
                        response = await complete(
                            messages,
                            self.preview.append if self.story is None else None,
                            _full_agent_story=True,
                            _submit_story=not self.settings.kemini.enabled,
                            extra_body=tool_options,
                        )
                    else:
                        response = await provider.chat_complete(
                            messages,
                            extra_body=tool_options,
                            _full_agent_story=True,
                            _submit_story=not self.settings.kemini.enabled,
                        )
                except Exception as error:
                    record = getattr(provider, "last_response_record", {})
                    if record.get("error"):
                        self.event("provider_response", record)
                    raise TurnError("PROVIDER_ERROR", str(error) or type(error).__name__) from error
                self.event(
                    "usage",
                    {"usage": response.usage or {}, "calls": 1, "duration_s": time.perf_counter() - started},
                )
                calls = getattr(provider, "last_tool_calls", []) or []
                completion = provider.last_completion
                self.event(
                    "full_agent_response",
                    {
                        "content": response.content or "",
                        "finish_reason": getattr(response, "finish_reason", None),
                        "completion": {"action": completion.action, "reason": completion.reason},
                        "tool_calls": [{"id": c.id, "name": c.name, "arguments": c.arguments} for c in calls],
                        "provider_response": copy.deepcopy(getattr(provider, "last_response_record", {})),
                    },
                )
                if completion.action == "incomplete":
                    raise TurnError(
                        "PROVIDER_INCOMPLETE",
                        completion_message("Full Agent 回复未完整结束，本回合未提交", completion),
                    )
                if any(not c.id for c in calls) or len({c.id for c in calls}) != len(calls):
                    raise TurnError("TOOL_PROTOCOL_INVALID", "模型返回了无效的工具调用序列")
                if len(calls) > MAX_BATCH_CALLS:
                    raise TurnError(
                        "TOOL_PROTOCOL_INVALID",
                        f"模型单次返回 {len(calls)} 个工具调用，超过 {MAX_BATCH_CALLS} 个上限；本次响应未执行任何工具",
                    )
                # A malformed protocol payload (observed from relays as
                # '{}{"content":...}') cannot be repaired by executing a tool.
                # Do not echo it through up to max_steps paid requests. Usage and
                # the raw response are already recorded; prior progress survives.
                for call in calls:
                    try:
                        json.loads(call.arguments)
                    except (ValueError, TypeError):
                        raise TurnError(
                            "TOOL_PROTOCOL_INVALID",
                            f"服务商返回的 {call.name} 工具参数不是合法 JSON；已停止本回合，避免连续付费重试。"
                            "请检查提供商的工具调用协议；Claude 可尝试 Anthropic Messages 接口。",
                        ) from None
                assistant_seq = len(self.new_trace) + 1
                assistant = self.response_message(response, provider, calls)
                text = response.content or ""
                auxiliary, transport_id = [], None
                if self.settings.kemini.enabled:
                    text, auxiliary, transport_id = self.story_candidate(text, calls)
                if text.strip() and self.settings.kemini.enabled:
                    receipt = await self.receive_story(text, auxiliary)
                    assistant["_story_received"] = receipt
                    if transport_id:
                        assistant["_story_call_id"] = transport_id
                    elif assistant["content"] != text:
                        assistant = {**portable(assistant), "content": text}
                self.model_completed = completion.action == "end"
                if self.settings.context_strategy == "openai_compact":
                    from tavern.full_agent.compaction import capture_checkpoint

                    # Invalid or duplicate output must not advance a window or
                    # install a checkpoint containing an unaccepted story.
                    if capture_checkpoint(self.state, compiled, self.settings, provider, assistant):
                        self.event(
                            "context_compacted",
                            {"message": "云端已压缩上下文", "window": self.state["window"]["number"]},
                        )
                window_id = self.state["window"]["id"]
                self.append(assistant, window_id)
                if text.strip() and not transport_id:
                    self.event(
                        "full_agent_assistant",
                        {
                            "activity_id": f"text-{assistant_seq}",
                            "order": assistant_seq * 10,
                            "content": text,
                            "is_story": self.settings.kemini.enabled,
                        },
                    )
                # Save the response before executing a batch, then checkpoint each
                # result. Interrupted batches resume only their unpaired calls.
                await self.save_progress()
                batch_failed = await self.execute_tools(assistant, window_id, assistant_seq)
                if self.model_completed:
                    return await self.finished_text(batch_failed=batch_failed)
                if self.story is None:
                    self.preview.replace("")
            raise TurnError(
                "BUDGET_EXHAUSTED", "Full Agent 达到本回合模型调用上限，草稿未提交；换窗不会重置调用额度"
            )
        finally:
            if provider is not None:
                try:
                    await asyncio.wait_for(provider.close(), timeout=10)
                except TimeoutError:
                    # A stuck HTTP connection can hang client shutdown. Closing
                    # only releases it; it must not replace the story already
                    # returned or the real error that ended the loop.
                    self.event("runtime_warning", {"message": "模型连接关闭超时"})

    async def resume_tools(self):
        records = list(self.new_trace)
        for seq, item in enumerate(records, 1):
            message = copy.deepcopy(item["message"])
            if message.get("tool_calls"):
                message["tool_calls"] = [
                    call for call in message["tool_calls"] if not retired_content_call(message, call)
                ]
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
                batch_failed = await self.execute_tools(
                    message, item["window_id"], seq, returned, legacy=self.legacy_protocol
                )
                if self.model_completed and batch_failed:
                    await self.finished_text(batch_failed=True)
        self.legacy_protocol = False
        await self.save_progress()

    async def execute_tools(self, assistant, window_id, assistant_seq, returned=(), *, legacy=False):
        calls = assistant["tool_calls"]
        business_calls = [
            call
            for call in calls
            if not (legacy and call["function"]["name"] in LEGACY_TURN_TOOLS)
            and not (self.settings.kemini.enabled and call["function"]["name"] == "kemini_emit_story")
        ]
        mixed_rollover = len(business_calls) > 1 and any(
            call["function"]["name"] == "new_context" for call in business_calls
        )
        disabled_batch = len(business_calls) > 1 and not self.settings.full_agent_config.parallel_tool_calls
        rejected_batch = mixed_rollover or disabled_batch
        batch_failed = False
        for index, call in enumerate(calls):
            if call["id"] in returned:
                continue
            name, arguments = call["function"]["name"], call["function"]["arguments"]
            self.recall_ids = []
            activity = {
                "activity_id": f"call-{assistant_seq}-{index}",
                "order": assistant_seq * 10 + index + 1,
                "name": name,
                "arguments": arguments,
            }
            self.event("full_agent_tool_started", activity)
            tool_started = time.perf_counter()
            previous_document = self.document
            legacy_receipt = None
            try:
                if mixed_rollover:
                    raise ToolError("MIXED_CONTEXT_CALLS", "new_context 必须单独调用；当前批次未执行任何工具")
                if self.must_rollover and (name != "new_context" or len(business_calls) != 1):
                    raise ToolError(
                        "CONTEXT_ROLLOVER_REQUIRED",
                        "当前必须换窗；仅接受单独的 new_context，其他调用已否决且未执行",
                    )
                if disabled_batch:
                    raise ToolError(
                        "PARALLEL_TOOL_CALLS_DISABLED",
                        "已关闭单次多工具调用；本次不执行此批次中尚未执行的工具",
                    )
                if legacy and name in LEGACY_TURN_TOOLS:
                    # Only migration of a previously saved response reaches
                    # this branch. These names never enter the new tool catalog.
                    args = json.loads(arguments)
                    if name == "finish_turn":
                        if args != {}:
                            raise ToolError("INVALID_ARGUMENTS", "旧收尾调用不接受参数")
                        self.model_completed = True
                        result = success({"finalization_requested": True})
                    elif self.story is not None:
                        result = {
                            "ok": False,
                            "error": {"code": "STORY_ALREADY_RECEIVED", "message": "本轮已输出正文"},
                        }
                    else:
                        if (
                            not isinstance(args, dict)
                            or set(args) != {"content"}
                            or not isinstance(args["content"], str)
                        ):
                            raise ToolError("INVALID_ARGUMENTS", "旧正文调用需要字符串 content")
                        text, auxiliary, _ = self.story_candidate(args["content"], [])
                        if not text.strip():
                            raise ToolError("INVALID_ARGUMENTS", "旧正文调用没有非空白正文")
                        legacy_receipt = await self.receive_story(text, auxiliary)
                        result = success(
                            {"saved_to_draft": True, "ref": message_ref(len(self.source_history))}
                        )
                elif name == "kemini_emit_story" and self.settings.kemini.enabled:
                    result = success({"story_received": True, "ref": message_ref(len(self.source_history))})
                else:
                    self.novel_operation = self.request_context.turn_id + ":" + call["id"]
                    result = await self.tools.execute(name, arguments)
                    if name == "submit_story":
                        # Attach the receipt to both saved copies of the calling response.
                        # Its ordinary text remains commentary; only the argument is prose.
                        receipt = {
                            "input_id": self.user_message["id"],
                            "content_hash": fingerprint(self.story["content"]),
                        }
                        records = [assistant, self.new_trace[assistant_seq - 1]["message"]]
                        # Call IDs may be reused by later responses from a relay.
                        records.append(
                            next(
                                m
                                for m in reversed(self.continuation)
                                if m.get("role") == "assistant"
                                and any(c["id"] == call["id"] for c in m.get("tool_calls", []))
                            )
                        )
                        for record in records:
                            record["_story_received"] = receipt
                            record["_story_call_id"] = call["id"]
            except TurnError:
                raise
            except ToolError as error:
                result = error.result
                if error.__cause__ is not None:
                    self.event(
                        "full_agent_tool_diagnostic",
                        {
                            "name": name,
                            "error_type": type(error.__cause__).__name__,
                            "message": str(error.__cause__),
                        },
                    )
            except jsonpointer.JsonPointerException as error:
                result = ToolError("INVALID_PATH", str(error)).result
            except jsonpatch.JsonPatchException as error:
                result = ToolError("PATCH_VALIDATION_FAILED", str(error)).result
            except (ValueError, KeyError, TypeError) as error:
                result = ToolError("INVALID_ARGUMENTS", str(error)).result
            if failed(result) and name == "patch_state":
                result["error"]["patch_state_completed"] = self.patch_state_done
            tool_error = failed(result)
            batch_failed |= tool_error
            if name == "revise_content" and not tool_error:
                # Later callbacks must read the same authored worldbook the
                # model just saved, without reinstalling scripts or rules.
                mirror = await self.script.call("snapshot")
                sync_runtime_lore(mirror, self.document, previous_document)
                await self.script.call("update", mirror)
            result = self.model_view.expose(result)
            message = {"role": "tool", "tool_call_id": call["id"], "content": encoded(result)}
            if legacy_receipt:
                message["_roleplay_received"] = legacy_receipt
            if self.recall_ids and not tool_error:
                # Existing trace metadata keeps runtime identities out of the
                # model result while surviving commits, retries and story forks.
                message["lore_ids"] = self.recall_ids
            if tool_error:
                message["_tool_error"] = True
            self.append(message, window_id)
            self.event(
                "full_agent_tool",
                {
                    **activity,
                    "result": result,
                    "duration_s": time.perf_counter() - tool_started,
                    "message": name + (" · 调用错误" if failed(result) else " · 完成"),
                },
            )
            if self.rolled:
                self.continuation = []
                self.rolled = False
                self.must_rollover = False
                self.retain()
                self.event(
                    "context_rollover",
                    {"window": self.state["window"]["number"], "message": "Agent 已保存 handoff 并换新窗口"},
                )
            if not rejected_batch:
                await self.save_progress()
        if rejected_batch:
            # A rejected batch must never persist just its first error. Recovery
            # removes failed calls; a partial checkpoint could otherwise turn the
            # remaining call into a valid standalone call.
            await self.save_progress()
        if legacy and batch_failed:
            self.model_completed = False
            await self.save_progress()
        return batch_failed

    def response_message(self, response, provider, calls):
        message = {
            "role": "assistant",
            "_content_tools_version": CONTENT_TOOLS_VERSION,
            "content": response.content or "",
            "_model_completion": {
                "action": provider.last_completion.action,
                "reason": provider.last_completion.reason,
            },
            "tool_calls": [
                {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": c.arguments}}
                for c in calls
            ],
        }
        message.update(
            {
                k: copy.deepcopy(v)
                for k, v in getattr(provider, "last_assistant_extra_fields", {}).items()
                if k in ("reasoning_content", "reasoning_details", "reasoning")
            }
        )
        message["_response_origin"] = response_origin(self.settings)
        message["_response_digest"] = message_digest(message)
        message["_context_epoch"] = self.context_epoch
        message["_native_epoch"] = self.native_epoch
        native = getattr(provider, "last_native_response", None)
        if native:
            message["_native_response"] = copy.deepcopy(native)
            message["_response_origin"] = copy.deepcopy(native["origin"])
        return message
