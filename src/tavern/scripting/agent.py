"""Writer and Tracker tool-mode adapter, governed by Roleplay Runtime."""

import copy
import json
import os

import httpx

from tavern.character.cards import lore_entries, normalize
from tavern.conversation.context import compile_context, get_path, search
from tavern.core.model_view import ModelView, state_for_model
from tavern.core.tokens import counting_info, request_tokens
from tavern.search.retrieval_text import RetrievalParser, split_retrieval


async def make_provider(settings, capture=None, *, request_timeout=120):
    from kohakuterrarium.llm.openai import OpenAIProvider
    from openai import AsyncOpenAI

    key = settings.api_key or os.environ.get("TAVERN_API_KEY", "")
    if not settings.model:
        raise ValueError("请先设置模型名称")
    if not key:
        raise ValueError("请先设置 API Key；本地免密端点可填 local")
    extra = {}
    if settings.reasoning_effort != "default":
        extra = (
            {"reasoning_effort": settings.reasoning_effort}
            if settings.reasoning_format == "reasoning_effort"
            else {"reasoning": {"effort": settings.reasoning_effort}}
        )
    provider = OpenAIProvider(
        api_key=key,
        model=settings.model,
        base_url=settings.base_url,
        temperature=settings.temperature,
        max_tokens=settings.reply_tokens,
        max_retries=0,
        extra_body=extra,
    )
    await provider._client.close()

    async def request_hook(request):
        if request.content:
            try:
                payload = json.loads(request.content)
            except (ValueError, UnicodeError):
                return
            wire = {"messages": payload.get("messages", []), "tools": payload.get("tools", [])}
            input_tokens = request_tokens(wire["messages"], settings, wire["tools"])
            if (
                input_tokens + settings.reply_tokens + 2048
                > settings.context_tokens
            ):
                raise ValueError(f"最终模型请求（含工具与结果）估算 {input_tokens} tokens，超过 token 预算，未发送")
            if capture:
                capture(
                    {
                        "model": payload.get("model"),
                        "messages": payload.get("messages", []),
                        "tools": [x.get("function", {}).get("name") for x in payload.get("tools", [])],
                        "reasoning": payload.get("reasoning", payload.get("reasoning_effort", "default")),
                        "estimated_input_tokens": input_tokens,
                        "token_counting": counting_info(settings),
                    }
                )

    client = httpx.AsyncClient(
        proxy=(settings.proxy or None) if settings.use_proxy else None,
        timeout=request_timeout,
        trust_env=False,
        event_hooks={"request": [request_hook]},
    )
    provider._client = AsyncOpenAI(api_key=key, base_url=settings.base_url, max_retries=0, http_client=client)
    return provider


class TurnAgent:
    """Shared turn draft and Terrarium transport; Roleplay Runtime owns orchestration."""

    def __init__(self, store, tid, document, session, history, snapshot, request, runtime, settings=None):
        self.store, self.tid = store, tid
        self.document, self.session, self.history = document, session, history
        self.snapshot, self.request, self.runtime = snapshot, request, runtime
        self.requested = set()
        self.state_patches = []
        self.agent = None
        self.compiled = None
        self.calls = 0
        from tavern.memories.history_recall import HistoryBudget
        self.history_recall_budget = HistoryBudget()
        self.settings = (settings or store.settings()).for_role("foreground")
        self.retrieval = None
        self.retrieval_query = ""
        if request.reasoning_effort is not None:
            self.settings.reasoning_effort = request.reasoning_effort
        self.maintenance = False
        self.input_message_id = ""
        # One mapping belongs to this maintenance operation, including all tool
        # continuations. Its short handles never replace identities in storage.
        self.model_view = ModelView({"document": document, "input": request.text,
                                     "history": [m.get("model", m["raw"]) for m in history]})
        self.lore_handles = {}
        self.history_references = None

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
            input_message_id=self.input_message_id,
            retrieval=self.retrieval,
            retrieval_protocol=not self.maintenance,
        )
        if self.maintenance:
            self.compiled["messages"][0:0] = [
                {
                    "role": "system",
                    "content": self.settings.maintenance_system_prompt,
                }
            ]
        else:
            self.compiled["messages"].append({
                "role": "system",
                "content": "本次仅生成故事正文与检索线索。MVU 变量由独立后台任务更新；不要输出变量更新块或变量操作指令。",
            })
        self.store.set_turn(self.tid, preview=json.dumps(self.compiled, ensure_ascii=False))
        return self.compiled

    def tools(self):
        from kohakuterrarium import tool

        @tool
        async def read_character(field: str = "") -> str:
            """Read a character field, or all character text fields."""
            data = normalize(self.document)
            if not field:
                data = {k: v for k, v in data.items() if k not in ("extensions", "assets", "character_book")}
            else:
                data = get_path(data, self.model_view.restore(field))
            # Do not expose hidden fingerprints even for an explicitly selected
            # technical field. Parent reads use a reversible, compact JSON view.
            from tavern.core.model_view import hidden_field
            if field and hidden_field(field.rsplit(".", 1)[-1]):
                return "该字段由程序维护，无需模型读取或回写。"
            return json.dumps(self.model_view.expose(data), ensure_ascii=False)

        def lore_view(entry, *, preview=False):
            handle = next((h for h, eid in self.lore_handles.items() if eid == entry["id"]), None)
            if handle is None:
                handle = f"L{len(self.lore_handles) + 1}"
                self.lore_handles[handle] = entry["id"]
            # Admission controls, cooldowns, hashes, source IDs and cached
            # scoring are Runtime bookkeeping. This tool only needs text.
            text = self.model_view.text(entry.get("content", ""))
            return {"ref": handle, "name": entry.get("comment") or entry.get("name", ""),
                    "preview" if preview else "content": text[:240] if preview else text}

        @tool
        async def search_lore(query: str) -> str:
            """Search worldbook entries using Chinese-aware text matching. Read an entry to include it."""
            query = self.model_view.restore(query)
            if getattr(self, "retrieval_engine", None):
                result = await self.retrieval_engine.prepare(self.document, self.history, query, self.snapshot,
                                                            self.settings, self.session, self.tid)
                by_id = {entry["id"]: entry for entry in result["entries"]}
                rows = [by_id[eid] for eid in result["ids"]]
            else:
                rows = search(lore_entries(self.document, "writer"), query)
            return json.dumps(self.model_view.expose([lore_view(e, preview=True) for e in rows]), ensure_ascii=False)

        @tool
        async def read_lore(entry_ref: str) -> str:
            """Read and activate a worldbook entry by the L1/L2 ref returned by search_lore."""
            entry_id = self.lore_handles.get(entry_ref)
            for entry in lore_entries(self.document, "writer"):
                if entry["id"] == entry_id and entry["enabled"]:
                    self.requested.add(entry_id)
                    return json.dumps(self.model_view.expose(lore_view(entry)), ensure_ascii=False)
            raise ValueError("世界书短编号不存在或未启用；先搜索并选择返回的 L 编号")

        @tool
        async def read_state(path: str = "") -> str:
            """Read current draft state using a dot-separated path."""
            return self.model_view.text(json.dumps(
                get_path(state_for_model(self.snapshot["state"]), self.model_view.restore(path)), ensure_ascii=False))

        @tool
        async def patch_state(operations: list[dict], reason: str) -> str:
            """Apply JSON Patch operations to stat_data through MVU; do not repeat them in narrative output."""
            if not self.request.update_variables:
                raise ValueError("本轮未勾选更新变量")
            operations = self.model_view.operations(operations)
            result = await self.runtime.call("process", "", operations)
            self.snapshot["state"] = result["state"]
            self.state_patches.extend(operations)
            self.store.event(self.tid, "state_proposal", {"operations": operations, "reason": reason})
            return json.dumps(result["state"], ensure_ascii=False)

        @tool
        async def search_memory(query: str) -> str:
            """Search memories visible on the current story branch."""
            from tavern.memories.memory_records import read_memory
            visible = [{k: r[k] for k in ("content", "category", "kind", "status", "manual") if k in r}
                       for r in read_memory(self.snapshot)["records"] if r.get("status") not in ("deleted", "superseded")]
            return self.model_view.text(json.dumps(search(visible, self.model_view.restore(query)), ensure_ascii=False))

        def recall_history(args):
            from tavern.memories.history_recall import HistoryRecall
            from tavern.memories.memory_protocol import References
            if self.history_references is None:
                self.history_references = References()
            recall = HistoryRecall(self.history, self.settings, self.history_recall_budget,
                document=self.document, persona=self.session["persona"],
                event=lambda kind, payload: self.store.event(self.tid, kind, {**payload, "purpose": "maintenance"}),
                references=self.history_references)
            return self.model_view.text(json.dumps(recall.execute(self.model_view.restore(args)), ensure_ascii=False))

        @tool
        async def tavern_recall_history(query: str = "", turn: int = 0, radius: int = 1, limit: int = 5) -> str:
            """只读历史补查：query搜索关键词或turn读取附近原文，二选一；整次维护最多3次，搜索和读取共用额度。"""
            args = {}
            if query:
                args.update(query=query, limit=limit)
            if turn:
                args.update(turn=turn, radius=radius)
            return recall_history(args)

        @tool
        async def write_memory(content: str, source_ref: str, kind: str = "fact") -> str:
            """Save branch memory using an H passage ref returned by history recall. Never copy IDs or quotes."""
            source = (self.history_references.sources if self.history_references else {}).get(source_ref)
            if source is None:
                raise ValueError("请引用本次历史补查返回的 H 片段编号")
            source_message_id = source["message_id"]
            if source_message_id not in {m["id"] for m in self.history}:
                raise ValueError("记忆必须引用当前分支已有消息")
            if kind not in ("fact", "inference"):
                raise ValueError("记忆类型必须为 fact 或 inference")
            from tavern.memories.memory_records import Record, install_memory, read_memory, source_hash
            message = next(m for m in self.history if m["id"] == source_message_id)
            record = Record(category="episode", content=self.model_view.restore(content), kind=kind,
                            sources=[{"message_id": source_message_id, "excerpt": source["excerpt"],
                                      "fingerprint": source_hash(message)}]).model_dump()
            memory = read_memory(self.snapshot)
            memory["records"].append(record)
            install_memory(self.snapshot, memory)
            self.store.event(self.tid, "memory_proposal", {"record": record})
            return "已记录该内容及程序回填的原文来源。"

        @tool
        async def revise_content(operations: list[dict], reason: str) -> str:
            """Propose JSON Patch edits to the character document. Obeys the session editing policy."""
            if self.session["policy_effective"] == "directed" and not self.request.allow_content_edit:
                raise ValueError("用户未授权本轮修改原始设定；只能维护剧情状态")
            revised = self.model_view.patch(self.document, operations)
            if not normalize(revised).get("name"):
                raise ValueError("设定必须保留角色名称")
            self.document = revised
            self.snapshot["content"] = {"document": revised, "reason": self.model_view.restore(reason), "turn": self.tid}
            self.store.event(self.tid, "content_proposal", {"operations": operations, "reason": reason})
            return "已创建当前分支的设定草稿；回合提交后生效。"

        return [
            read_character,
            search_lore,
            read_lore,
            read_state,
            patch_state,
            search_memory,
            tavern_recall_history,
            write_memory,
            revise_content,
        ]

    async def _run(self, provider=None, *, maintenance=False):
        from kohakuterrarium import Activity, Terrarium, TextChunk, TurnEnded
        from kohakuterrarium.core.config_types import AgentConfig
        from kohakuterrarium.modules.plugin import BasePlugin

        owner = self
        self.maintenance = maintenance
        original_settings = self.settings
        if maintenance:
            self.settings = self.settings.for_role("background")
        self.calls = 0
        allowed = (
            {
                "read_character",
                "search_lore",
                "read_lore",
                "read_state",
                "search_memory",
                "tavern_recall_history",
                "write_memory",
                "revise_content",
            }
            if maintenance
            else set()
        )

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
                if owner.calls > (4 if maintenance else 1):
                    from kohakuterrarium.modules.plugin import PluginBlockError

                    raise PluginBlockError("本轮 Agent 调用次数超限")
                # A fresh creature owns only this turn. Keep native assistant/tool blocks after its input.
                marker = next((i for i, m in enumerate(messages) if m.get("role") == "user"), len(messages))
                continuation = copy.deepcopy(messages[marker + 1 :])
                try:
                    compiled = copy.deepcopy(owner.compile()["messages"]) + continuation
                    return owner.model_view.messages(compiled) if maintenance else compiled
                except Exception as error:
                    from kohakuterrarium.modules.plugin import PluginBlockError

                    raise PluginBlockError(str(error)) from error

        owned_provider = provider is None
        if provider is None:
            provider = await make_provider(
                self.settings.for_role("background") if maintenance else self.settings,
                lambda payload: self.store.event(
                    self.tid,
                    "provider_request",
                    {**payload, "purpose": "maintenance" if maintenance else "narrative"},
                ),
            )
        config = AgentConfig(
            name="terrarium-tavern-tracker" if maintenance else "terrarium-tavern-writer",
            system_prompt=self.settings.maintenance_system_prompt if maintenance else self.settings.system_prompt,
            tools=[],
            subagents=[],
            include_tools_in_prompt=False,
            include_hints_in_prompt=False,
            max_iterations=4 if maintenance else 1,
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
                tools=self.tools() if maintenance else [],
                plugins=[ContextPlugin()],
                io="headless",
                session=self.store.root
                / "runs"
                / f"{self.tid}{'-maintenance' if maintenance else ''}.kohakutr",
            )
            self.agent = creature.agent
            async for event in creature.run_stream(self.request.text or "继续剧情"):
                if isinstance(event, TextChunk):
                    visible = event.text if maintenance else parser.feed(event.text)
                    text += visible
                    if not maintenance:
                        self.store.set_turn(self.tid, draft=text)
                        self.store.event(self.tid, "text", {"text": visible})
                elif isinstance(event, TurnEnded):
                    result = event.result
                elif isinstance(event, Activity):
                    self.store.event(
                        self.tid,
                        "activity",
                        {"kind": event.kind, "message": event.detail, "metadata": event.metadata,
                         "purpose": "maintenance" if maintenance else "narrative"},
                    )
            if result is None or result.status != "ok":
                raise ValueError(
                    f"Agent 回合未完成：{getattr(result, 'error', None) or getattr(result, 'status', 'unknown')}"
                )
            self.store.event(
                self.tid,
                "usage",
                {
                    "purpose": "maintenance" if maintenance else "narrative",
                    "calls": self.calls,
                    "usage": result.usage or {},
                    "duration_s": result.duration_s,
                },
            )
            if not maintenance and not result.text.strip():
                raise ValueError("模型没有返回正文。请降低思考强度或增大单次输出上限；本轮未提交。")
            if maintenance:
                self.store.event(self.tid, "maintenance_completed", {"record": result.text})
            if not maintenance:
                clean, self.retrieval_query, diagnostics = split_retrieval(result.text, self.settings.retrieval.query_chars)
                tail = parser.feed("", final=True)
                if tail:
                    self.store.event(self.tid, "text", {"text": tail})
                self.store.event(self.tid, "retrieval_text", {"diagnostics": diagnostics})
                self.store.set_turn(self.tid, draft=clean)
                return clean
            return result.text
        finally:
            await engine.shutdown()
            if owned_provider:
                await provider.close()
            self.settings = original_settings

    async def cancel(self):
        if self.agent:
            self.agent.interrupt()


class Writer(TurnAgent):
    """前台 Writer: one tool-free narrative call, plus retrieval text protocol.

    Consumes Runtime-compiled context; never schedules Tracker or commits state.
    The shared adapter's tool mode is entered only by Tracker.maintain().
    """

    async def run(self, provider=None):
        return await self._run(provider)
