"""Writer's tool-free adapter; legacy maintenance calls delegate to TrackerTask."""

import copy
import json

from tavern.conversation.cache_breakpoint import model_messages, prepare_context
from tavern.conversation.context import compile_context
from tavern.conversation.half_context import load_trace
from tavern.conversation.regeneration import reply_for_regeneration
from tavern.core.message_adapter import provider_messages
from tavern.core.provider import make_provider
from tavern.scripting.request_context import OperationContexts, configure_operation
from tavern.search.retrieval_text import RetrievalParser, split_retrieval


class TurnAgent:
    """Shared turn draft and Terrarium transport; Roleplay Runtime owns orchestration."""

    def __init__(self, store, tid, document, session, history, snapshot, request, runtime, settings=None):
        self.store, self.tid = store, tid
        self.document, self.session, self.history = document, session, history
        self.snapshot, self.request, self.runtime = snapshot, request, runtime
        self.regenerate_reply = reply_for_regeneration(request, session)
        self.requested = set()
        self.state_patches = []
        self.agent = None
        self.compiled = None
        self.calls = 0
        from tavern.memories.history_recall import HistoryBudget
        self.history_recall_budget = HistoryBudget()
        self.settings = (settings or store.settings()).for_role("foreground")
        self.settings.cache_scope = session["id"]
        self.retrieval = None
        self.retrieval_query = ""
        if request.reasoning_effort is not None:
            self.settings.reasoning_effort = request.reasoning_effort
        self.maintenance = False
        self.input_message_id = ""
        self.operation_contexts = OperationContexts(store, session["id"], tid)
        self.native_response = None
        self.clean_response = ""
        self.native_responses = {}
        self.compact_checked = False
        self.compact_calls = 0

    def compile(self):
        self.compiled = compile_context(
            self.document,
            self.session,
            self.history,
            self.snapshot,
            self.request.text,
            self.settings,
            self.tid,
            self.requested,
            self.request.generate,
            turn_instruction=self.request.regenerate_note,
            regenerate_reply=self.regenerate_reply,
            input_message_id=self.input_message_id,
            retrieval=self.retrieval,
            replay_records=load_trace(self.store, self.session["id"], self.history),
        )
        prepare_context(self.compiled, self.session, self.history, self.snapshot, self.settings)
        if self.compiled["estimated_tokens"] > self.settings.context_tokens - self.settings.reply_tokens - 2048:
            self.store.event(self.tid, "context_warning", {"purpose": "writer",
                                                       "message": "正文请求估算超出模型上下文，仍完整发送"})
        self.store.set_turn(self.tid, preview=json.dumps(self.compiled, ensure_ascii=False))
        return self.compiled

    async def _run(self, provider=None, *, maintenance=False):
        from kohakuterrarium import Activity, Terrarium, TextChunk, TurnEnded
        from kohakuterrarium.core.config_types import AgentConfig
        from kohakuterrarium.modules.plugin import BasePlugin

        if maintenance:
            # Compatibility entry delegates to the dedicated compiler; never
            # install maintenance instructions into Writer's context.
            from tavern.memories.memory import Tracker
            return await Tracker(self.store).maintain(self, provider)
        owner = self
        self.calls = 0
        allowed = set()

        class ContextPlugin(BasePlugin):
            name = "tavern-context"

            def get_tool_visibility(self, context):
                from kohakuterrarium.modules.plugin.tool_visibility import ToolVisibility

                return ToolVisibility(allowed_tools=frozenset(allowed), allowed_subagents=frozenset())

            async def pre_tool_execute(self, args, **kwargs):
                if kwargs.get("tool_name") not in allowed:
                    from kohakuterrarium.modules.plugin import PluginBlockError

                    raise PluginBlockError("Roleplay Runtime 禁止当前角色执行该工具")
                return None

            async def pre_llm_call(self, messages, **kwargs):
                owner.calls += 1
                if owner.calls > 1:
                    from kohakuterrarium.modules.plugin import PluginBlockError

                    raise PluginBlockError("本轮 Agent 调用次数超限")
                try:
                    context = owner.compile()
                    from tavern.conversation.half_compaction import enabled, install
                    if enabled(owner.settings) and not owner.compact_checked:
                        owner.compact_checked = True
                        if (owner.settings.compact_auto and context["estimated_tokens"] > owner.settings.context_tokens
                                and context["_replay_complete"]):
                            from tavern.conversation.compaction import prepare_turn
                            narrative_context = provider.request_context
                            capture = getattr(provider, "capture", None)
                            def maintenance_event(kind, payload):
                                owner.store.event(owner.tid, kind, {**payload, "purpose": "context_maintenance"})
                            async def progress(value):
                                maintenance_event("context_replay", {k: value[k] for k in ("batches", "processed", "total")})
                                maintenance_event("usage", {"usage": value["usage"][-1], "calls": 1})
                                owner.compact_calls += 1
                            configure_operation(provider, owner.operation_contexts.new("context_maintenance"),
                                                maintenance_event, "context_maintenance")
                            if hasattr(provider, "capture"):
                                provider.capture = lambda payload: maintenance_event("provider_request", payload)
                            try:
                                checkpoint = await prepare_turn(context, owner.settings, provider, save=progress)
                                install(owner.snapshot, checkpoint, context, owner.history)
                            finally:
                                configure_operation(provider, narrative_context,
                                    lambda kind, payload: owner.store.event(owner.tid, kind, payload), "narrative")
                                if hasattr(provider, "capture"):
                                    provider.capture = capture
                            context = owner.compile()
                            maintenance_event("context_compacted", {"message": "云端已压缩已完成的交流"})
                    compiled = model_messages(context)
                    provider.request_context.diagnostics = copy.deepcopy(context.get("context_ledger", {}))
                    provider.request_context.window_id = context.get("half_agent_record", {}).get("window", "")
                    owner.store.set_turn(owner.tid, preview=json.dumps(context, ensure_ascii=False))
                    if context.get("context_ledger", {}).get("rebuild_reason"):
                        owner.store.event(owner.tid, "context_baseline", {
                            "purpose": "narrative", **context["context_ledger"]})
                    return provider_messages(provider, compiled, owner.settings)
                except Exception as error:
                    from kohakuterrarium.modules.plugin import PluginBlockError

                    raise PluginBlockError(str(error)) from error

            async def post_llm_call(self, messages, response, usage, **kwargs):
                native = getattr(provider, "last_native_response", None)
                if native:
                    owner.native_responses[native["message_sha256"]] = copy.deepcopy(native)
                owner.native_response = copy.deepcopy(native)

        owned_provider = provider is None
        if provider is None:
            provider = await make_provider(
                self.settings,
                lambda payload: self.store.event(
                    self.tid,
                    "provider_request",
                    {**payload, "purpose": "narrative"},
                ),
            )
        purpose = "narrative"
        configure_operation(provider, self.operation_contexts.new(purpose),
                            lambda kind, payload: self.store.event(self.tid, kind, payload), purpose)
        config = AgentConfig(
            name="terrarium-tavern-writer",
            system_prompt=self.settings.system_prompt,
            tools=[],
            subagents=[],
            include_tools_in_prompt=False,
            include_hints_in_prompt=False,
            max_iterations=1,
            compact={"enabled": False},
            default_plugins=[],
            agent_path=self.store.root.resolve(),
        )
        engine = Terrarium()
        text, result = "", None
        parser = RetrievalParser(self.settings.retrieval.query_chars)
        try:
            creature = await engine.add_creature(
                config,
                llm=provider,
                tools=[],
                plugins=[ContextPlugin()],
                io="headless",
                session=self.store.root
                / "runs"
                / f"{self.tid}.kohakutr",
            )
            self.agent = creature.agent
            async for event in creature.run_stream(self.request.text or "继续剧情"):
                if isinstance(event, TextChunk):
                    visible = parser.feed(event.text)
                    text += visible
                    self.store.set_turn(self.tid, draft=text)
                    self.store.event(self.tid, "text", {"text": visible})
                elif isinstance(event, TurnEnded):
                    result = event.result
                elif isinstance(event, Activity):
                    self.store.event(
                        self.tid,
                        "activity",
                        {"kind": event.kind, "message": event.detail, "metadata": event.metadata,
                         "purpose": "narrative"},
                    )
            if result is None or result.status != "ok":
                raise ValueError(
                    f"Agent 回合未完成：{getattr(result, 'error', None) or getattr(result, 'status', 'unknown')}"
                )
            self.store.event(
                self.tid,
                "usage",
                {
                    "purpose": "narrative",
                    "calls": self.calls,
                    "usage": (provider.usage_history[-1] if self.compact_calls and getattr(provider, "usage_history", [])
                              else getattr(provider, "reported_usage", None) or result.usage or {}),
                    "duration_s": result.duration_s,
                },
            )
            if not result.text.strip():
                raise ValueError("模型没有返回正文。请降低思考强度或增大单次输出上限；本轮未提交。")
            clean, self.retrieval_query, diagnostics = split_retrieval(result.text, self.settings.retrieval.query_chars)
            self.clean_response = clean
            self.native_response = copy.deepcopy(getattr(provider, "last_native_response", None))
            tail = parser.feed("", final=True)
            if tail:
                self.store.event(self.tid, "text", {"text": tail})
            self.store.event(self.tid, "retrieval_text", {"diagnostics": diagnostics})
            self.store.set_turn(self.tid, draft=clean)
            return clean
        finally:
            await engine.shutdown()
            if owned_provider:
                await provider.close()

    async def cancel(self):
        if self.agent:
            self.agent.interrupt()


class Writer(TurnAgent):
    """前台 Writer: one tool-free narrative call, plus retrieval text protocol.

    Consumes Runtime-compiled context; never schedules Tracker or commits state.
    Tracker uses a separate compiler and native tool loop.
    """

    async def run(self, provider=None):
        return await self._run(provider)
