"""Roleplay Runtime: schedule Writer/Tracker and own the atomic turn lifecycle."""

import asyncio
import copy
import shutil

from tavern.character.cards import regex_scripts
from tavern.conversation.message_editing import editable_message_ids
from tavern.core.models import TurnRequest
from tavern.memories.memory import Tracker, completed_rounds
from tavern.scripting.agent import Writer
from tavern.scripting.dependencies import Dependencies
from tavern.scripting.request_context import OperationContexts, configure_operation
from tavern.scripting.runtime import ScriptRuntime
from tavern.storage.store import Conflict, dump, uid


class RoleplayRuntime:
    """程序 Roleplay Runtime: context, permissions, scripts, branches and commits.

    Writer and Tracker exchange drafts through this orchestrator, never delegation.
    ScriptRuntime below is only the isolated card-script executor.
    """

    def __init__(self, store):
        self.store = store
        self.runtime = ScriptRuntime()
        self.dependencies = Dependencies(store)
        self.tasks = {}
        self.agents = {}
        self.provider_factory = None  # dependency injection for integration tests
        self.memory_provider_factory = None
        self.variable_provider_factory = None
        self.tracker_provider_factory = None
        self.tracker = Tracker(store)
        from tavern.search.local_models import LocalModelManager
        from tavern.search.retrieval import RetrievalEngine
        self.local_models = LocalModelManager(store)
        self.retrieval = RetrievalEngine(store, self.local_models)

    def prepare_indexes(self, sid):
        """Opportunistic preparation after creating/opening a story; never changes story state."""
        async def prepare(record):
            session = self.store.session(sid)
            document = (session["snapshot"].get("content") or {}).get(
                "document", self.store.session_card(sid)["document"])
            from tavern.memories.memory import window_history
            visible, _ = window_history(session["messages"], session["snapshot"])
            await self.retrieval.prepare(document, visible, "", session["snapshot"],
                                         self.store.settings(), session, "prepare", index_only=True)
        try:
            return self.local_models.job("prepare:" + sid, prepare)
        except ValueError:
            return None  # A full preparation queue must not prevent opening a story.

    async def close(self):
        for task in self.tasks.values():
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)
        await self.local_models.close()
        await self.runtime.close()

    def submit(self, sid, request):
        if self.store.session(sid).get("agent_mode", "half_agent") != "half_agent":
            raise ValueError("Full Agent 会话须由独立引擎执行")
        if (self.tracker_provider_factory or self.memory_provider_factory if request.scene_refresh else self.provider_factory) is None:
            import os

            settings = self.store.settings().for_role("background" if request.scene_refresh else "foreground")
            if not settings.model:
                raise ValueError("请先在“模型与偏好”中设置模型名称")
            if not (settings.api_key or os.environ.get("TAVERN_API_KEY", "")):
                raise ValueError("请先在“模型与偏好”中设置 API Key；本地免密端点可填 local")
        tid, created = self.store.create_turn(sid, request)
        if created:
            self.tasks[tid] = asyncio.create_task(self.run(tid))
            def completed(task):
                finished = [key for key, value in self.tasks.items() if value.done()]
                for key in finished[:-30]:
                    self.tasks.pop(key, None)
            self.tasks[tid].add_done_callback(completed)
        return self.store.turn(tid)

    async def cancel(self, tid):
        task = self.tasks.get(tid)
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        elif self.store.turn(tid)["status"] == "awaiting_review":
            self.store.set_turn(tid, status="cancelled", pending=None)
            self.store.event(tid, "cancelled", {})

    async def delete_session(self, sid):
        turn_ids = self.store.session_turn_ids(sid)
        for tid in turn_ids:
            await self.cancel(tid)
            self.tasks.pop(tid, None)
            self.agents.pop(tid, None)
        runs = (self.store.root / "runs").resolve()
        if runs.exists():
            for tid in turn_ids:
                for path in runs.glob(f"{tid}*"):
                    resolved = path.resolve()
                    if resolved.parent != runs:
                        raise ValueError("会话运行文件路径无效")
                    if resolved.is_dir():
                        shutil.rmtree(resolved)
                    else:
                        resolved.unlink(missing_ok=True)
        async with self.dependencies.lock:
            self.store.delete_session(sid)

    async def delete_card(self, cid):
        async with self.dependencies.lock:
            self.store.delete_card(cid)

    def review(self, tid, accept):
        turn = self.store.turn(tid)
        if turn["status"] != "awaiting_review":
            raise Conflict("此回合没有待审核变更")
        if accept:
            self.store.commit_turn(tid, turn["pending"])
        else:
            self.store.reject_review(tid)
        return self.store.turn(tid)

    async def refresh_scene(self, tid, req, settings, session):
        from tavern.memories.memory_engine import CallBudget, estimate_batches
        from tavern.memories.memory_records import install_memory, read_memory

        kind = req.scene_refresh
        self.store.set_turn(tid, status="running")
        self.store.event(tid, "scene_refresh_started", {"kind": kind})
        before = copy.deepcopy(session["snapshot"])
        snapshot = copy.deepcopy(before)
        cfg = settings.for_role("background")
        memory_calls = CallBudget(manual=True)
        document = (snapshot.get("content") or {}).get("document", self.store.session_card(session["id"])["document"])
        contexts = OperationContexts(self.store, session["id"], tid)

        # A rebuild replaces automatic records only. User edits and deletions
        # remain explicit constraints, rather than being erased by re-extraction.
        base = {"version": 1, "records": [r for r in read_memory(snapshot)["records"] if r.get("manual")]}
        estimate = await asyncio.to_thread(estimate_batches, session["messages"], base, document, session["persona"], cfg)
        self.store.event(tid, "memory_rebuild_estimate", {"purpose": "memory", "estimated_batches": estimate,
            "message": f"预计至少 {estimate} 批；记忆增长可能增加批次，可随时取消，完成后统一替换。"})
        snapshot["memory_document"] = base
        task = await self.run_tracker_task(tid, settings, session, document, session["messages"], snapshot,
            phase="rebuild", contexts=contexts, memory_start=0, memory_budget=memory_calls)
        rebuilt = task.snapshot["memory_document"]
        self.store.event(tid, "scene_refresh_ready", {"purpose": kind, "result": rebuilt})
        rounds = completed_rounds(session["messages"])
        from tavern.memories.memory import fingerprint

        install_memory(snapshot, rebuilt)
        snapshot.pop("memory_warning", None)
        snapshot["memory_archive"] = {"covered_count": len(session["messages"]),
                                      "fingerprint": fingerprint(session["messages"]), "last_round": rounds}
        pending = {"scene": {"message_id": session["head"], "snapshot": snapshot},
                   "before": before, "reason": "手动重新生成记忆"}
        if session["policy_effective"] == "preview" and snapshot != before:
            self.store.set_turn(tid, status="awaiting_review", pending=dump(pending))
            self.store.event(tid, "awaiting_review", {"before": before, "after": snapshot})
        else:
            self.store.commit_turn(tid, pending)

    async def initialize_session(self, sid):
        session = self.store.session(sid)
        card = self.store.session_card(session["id"])
        if card["permission"]:
            # Resolves imports/assets before creating the story; source card is unchanged.
            await self.dependencies.resolve(card)
        logs = []

        async def host(method, args):
            if method == "log":
                logs.append(args)
                return None
            raise ValueError("开场初始化时不允许发起模型请求")

        runtime = await self.runtime.open(
            card["document"],
            session["messages"],
            session["snapshot"],
            bool(card["permission"]),
            host,
            self.dependencies.scripts(card),
            lambda html: self.dependencies.markup(card, html),
            persona=session["persona"],
            opening=True,
            greeting_id=session.get("greeting_id"),
        )
        try:
            mirror = await runtime.call("snapshot")
            snapshot = session["snapshot"]
            snapshot["state"], snapshot["scopes"] = mirror["state"], mirror["scopes"]
            greeting = session["messages"][-1]
            rules = regex_scripts(card["document"])
            display = await runtime.call("transform", greeting["raw"], rules, 2, "display", 0)
            if card["permission"] and "<" in display:
                display = self.dependencies.markup(card, display)
            model = await runtime.call("transform", greeting["raw"], rules, 2, "model", 0)
            with self.store.connection() as db:
                db.execute(
                    "UPDATE messages SET snapshot=?,model=?,display=? WHERE id=?",
                    (dump(snapshot), model, display, greeting["id"]),
                )
        finally:
            await runtime.close()
        return self.store.session(sid)

    async def edit_message(self, sid, mid, text, expected_revision, *, notify_ai=False):
        session = self.store.session(sid)
        if mid not in editable_message_ids(session["messages"], session["head"]):
            raise ValueError("只能编辑当前分支最后一轮的用户消息或 AI 回复")
        message = next((m for m in session["messages"] if m["id"] == mid), None)
        if message is None:
            raise ValueError("只能编辑当前会话分支中的消息")
        card = self.store.session_card(sid)
        snapshot = message["snapshot"]
        document = (snapshot.get("content") or {}).get("document", card["document"])

        async def host(method, args):
            raise ValueError("消息编辑格式化不允许执行卡片交互")

        # Only run the formatting rules; editing must not replay state updates or scripts.
        runtime = await self.runtime.open(
            document, session["messages"], snapshot, False, host, scripts_override=[],
            persona=session["persona"],
        )
        try:
            rules = regex_scripts(document)
            placement = 2 if message["role"] == "assistant" else 1
            model = await runtime.call("transform", text, rules, placement, "model", 0)
            display = await runtime.call("transform", text, rules, placement, "display", 0)
            if card["permission"] and "<" in display:
                await self.dependencies.resolve(card)
                display = self.dependencies.markup(card, display)
            return self.store.edit_message(
                sid, mid, text, expected_revision, model=model, display=display, notify_ai=notify_ai,
            )
        finally:
            await runtime.close()

    async def interaction(self, sid, payload):
        req = TurnRequest(
            text="卡片交互",
            operation_id=payload["operation_id"],
            expected_revision=payload["expected_revision"],
            allow_content_edit=bool(payload.get("allow_content_edit")),
        )
        tid, created = self.store.create_turn(sid, req)
        if not created:
            return self.store.session(sid)
        runtime = None
        try:
            self.store.set_turn(tid, status="running")
            session = self.store.session(sid)
            card = self.store.session_card(session["id"])
            if not card["permission"]:
                raise ValueError("此卡未启用脚本")
            before = copy.deepcopy(session["snapshot"])
            snapshot = copy.deepcopy(before)
            document = (snapshot.get("content") or {}).get("document", card["document"])

            async def host(method, args):
                if method == "log":
                    self.store.event(tid, "script_log", args)
                    return None
                raise ValueError("交互事务中不允许嵌套生成")

            runtime = await self.runtime.open(
                document,
                session["messages"],
                snapshot,
                True,
                host,
                self.dependencies.scripts({**card, "document": document}),
                lambda html: self.dependencies.markup(card, html),
            )
            mirror = await runtime.call("snapshot")
            method, args = payload["method"], payload.get("args", {})
            raw = session["messages"][-1]["raw"]
            if method == "variables":
                option = args.get("option") or {"type": "chat"}
                variables = args["variables"]
                if not isinstance(variables, dict):
                    raise ValueError("变量必须为对象")
                scope = option.get("type", "chat")
                if scope == "message":
                    index = option.get("message_id", "latest")
                    if index == "latest":
                        index = len(mirror["messages"]) - 1
                    if index < 0:
                        index += len(mirror["messages"])
                    if index != len(mirror["messages"]) - 1:
                        raise ValueError("历史楼层变量修改请先切换到该剧情分支")
                    mirror["state"] = variables
                elif scope == "chat":
                    mirror["state"] = variables
                elif scope in ("character", "global", "preset", "script", "extension"):
                    key = (
                        f"script:{option.get('script_id', 'card')}"
                        if scope == "script"
                        else f"extension:{option.get('extension_id', '')}"
                        if scope == "extension"
                        else scope
                    )
                    mirror["scopes"][key] = variables
                else:
                    raise ValueError("无效变量范围")
            elif method == "messages":
                messages = args.get("messages", [])
                if len(messages) != 1 or messages[0].get("message_id") not in (
                    -1,
                    len(mirror["messages"]) - 1,
                ):
                    raise ValueError("仅允许当前楼层编辑；历史编辑请使用剧情分支")
                raw = str(messages[0].get("message", raw))
            elif method == "lore":
                if session["policy_effective"] == "directed" and not req.allow_content_edit:
                    raise ValueError("请启用本轮设定修改权限")
                revised = copy.deepcopy(document)
                revised.get("data", revised).setdefault("character_book", {})["entries"] = args["entries"]
                snapshot["content"] = {"document": revised, "reason": "卡片界面修改世界书", "turn": tid}
            else:
                raise ValueError("不支持的界面操作")
            await runtime.call("update", mirror)
            result = await runtime.call("process", "")
            snapshot["state"], snapshot["scopes"] = result["state"], result["scopes"]
            old = session["messages"][-1]
            rules = regex_scripts(document)
            m = {
                "id": uid(),
                "parent": old["parent"],
                "role": old["role"],
                "raw": raw,
                "model": await runtime.call(
                    "transform", raw, rules, 2 if old["role"] == "assistant" else 1, "model", 0
                ),
                "display": await runtime.call(
                    "transform", raw, rules, 2 if old["role"] == "assistant" else 1, "display", 0
                ),
                "snapshot": snapshot,
            }
            pending = {"messages": [m], "before": before, "reason": "卡片界面交互"}
            if session["policy_effective"] == "preview":
                self.store.set_turn(tid, status="awaiting_review", pending=dump(pending), draft=raw)
                self.store.event(tid, "awaiting_review", {"before": before, "after": snapshot})
            else:
                self.store.commit_turn(tid, pending)
            return self.store.session(sid)
        except BaseException as e:
            self.store.set_turn(tid, status="error", error=str(e))
            raise
        finally:
            if runtime:
                await runtime.close()

    async def run_tracker_task(self, tid, settings, session, document, history, snapshot, *, phase,
                               memory_start=None, memory_budget=None, **options):
        from tavern.tracker.task import TrackerTask
        factory = self.tracker_provider_factory
        if factory is None:
            factory = (self.memory_provider_factory if memory_start is not None else
                       self.variable_provider_factory if options.get("variables") else self.provider_factory)
        provider = factory() if factory else None
        task = TrackerTask(self.store, tid, settings, session, document, history, snapshot,
                           phase=phase, memory=memory_start is not None, provider=provider, **options)
        task.retrieval = self.retrieval
        try:
            return await task.run(memory_start=memory_start, memory_budget=memory_budget)
        finally:
            if provider is not None:
                await provider.close()

    async def run(self, tid):
        runtime = None
        try:
            turn = self.store.turn(tid)
            req = TurnRequest.model_validate(turn["request"])
            from tavern.core.message_adapter import bind_adapter
            settings = bind_adapter(self.store.settings().for_role("foreground").model_copy(deep=True),
                                    self.store, "story", turn["session"])
            session = self.store.session(turn["session"])
            contexts = OperationContexts(self.store, session["id"], tid)
            if req.scene_refresh:
                await self.refresh_scene(tid, req, settings, session)
                return
            card = self.store.session_card(session["id"])
            history = session["messages"]
            reused_user = None
            if req.mode == "regenerate":
                target = req.message_id or session["head"]
                found = next((i for i, m in enumerate(history) if m["id"] == target), None)
                if found is None:
                    raise ValueError("目标消息不在当前分支")
                if history[found]["role"] != "assistant" or not found:
                    raise ValueError("请选择可重生成的角色回复")
                history = history[:found]
                if history[-1]["role"] == "user":
                    reused_user = copy.deepcopy(history[-1])
                    req.text = history[-1]["raw"]
                    history = history[:-1]
            if not req.text.strip():
                raise ValueError("请输入消息")
            before = copy.deepcopy(
                reused_user["snapshot"]
                if reused_user
                else history[-1]["snapshot"]
                if history
                else {"state": {}, "memory_document": {"version": 1, "records": []}, "lore": {}, "scopes": {}, "content": None}
            )
            snapshot = copy.deepcopy(before)
            from tavern.conversation.message_editing import (
                NOTIFICATIONS,
                inherit_edit_notifications,
                review_snapshot,
            )
            inherit_edit_notifications(snapshot, session["snapshot"])
            document = copy.deepcopy(
                snapshot.get("content", {}).get("document", card["document"])
                if snapshot.get("content")
                else card["document"]
            )
            self.store.set_turn(tid, status="running")
            self.store.event(tid, "started", {})
            if card["permission"]:
                await self.dependencies.resolve({**card, "document": document})

            async def host(method, args):
                if method == "log":
                    self.store.event(tid, "script_log", args)
                    return None
                if method == "generate":
                    # A nested request gets a read-only copy and no mutation tools.
                    from tavern.conversation.context import compile_context
                    from tavern.scripting.agent import make_provider

                    provider = await make_provider(settings, lambda payload: self.store.event(
                        tid, "provider_request", {**payload, "purpose": "script"}))
                    configure_operation(provider, contexts.new("script"),
                                        lambda kind, payload: self.store.event(tid, kind, payload), "script")
                    try:
                        from tavern.memories.memory import window_history
                        nested_document = (snapshot.get("content") or {}).get("document", document)
                        visible, _ = window_history(history, snapshot, enabled=settings.chunk_mode != "off")
                        prepared = await self.retrieval.prepare(
                            nested_document, visible, args.get("user_input", ""), snapshot, settings, session, tid)
                        context = compile_context(
                            nested_document,
                            session,
                            history,
                            snapshot,
                            args.get("user_input", ""),
                            settings,
                            tid,
                            generate=args,
                            retrieval=prepared,
                        )
                        from tavern.conversation.cache_breakpoint import model_messages
                        from tavern.core.message_adapter import provider_messages
                        result = await provider.chat_complete(provider_messages(provider, model_messages(context), settings))
                        return result.content
                    finally:
                        await provider.close()
                if method == "cancel":
                    return False
                raise ValueError(f"脚本接口不允许：{method}")

            runtime = await self.runtime.open(
                document,
                history,
                snapshot,
                bool(card["permission"]),
                host,
                self.dependencies.scripts({**card, "document": document}),
                lambda html: self.dependencies.markup(card, html),
            )
            initialized = await runtime.call("snapshot")
            snapshot["state"] = initialized["state"]
            snapshot["scopes"] = initialized["scopes"]
            rules = regex_scripts(document)
            input_raw = req.text
            req.text = await runtime.call("transform", input_raw, rules, 1, "model", 0)

            from tavern.memories.history_recall import HistoryBudget
            from tavern.memories.memory_engine import CallBudget
            from tavern.tracker.task import TaskBudget
            from tavern.tracker.workflow import before_story
            history_budget = HistoryBudget()
            pre_budget = TaskBudget(settings.tracker_max_steps)
            memory_calls = CallBudget(history=history_budget)

            async def run_tracker(phase, current_document, task_history, task_snapshot, **options):
                return await self.run_tracker_task(tid, settings, session, current_document,
                    task_history, task_snapshot, phase=phase, contexts=contexts,
                    history_budget=history_budget,
                    budget=pre_budget if phase == "pre" else TaskBudget(settings.tracker_max_steps),
                    memory_budget=memory_calls,
                    **options)

            original_document = copy.deepcopy(document)
            document, prepared = await before_story(self.store, tid, settings, session, history,
                snapshot, req, document, self.retrieval, run_tracker)
            # Pre-story card edits must reach scripts too, without reinstalling
            # scripts or replaying initialization callbacks.
            from tavern.tracker.workflow import sync_runtime_lore
            mirror = await runtime.call("snapshot")
            sync_runtime_lore(mirror, document, original_document)
            await runtime.call("update", mirror)
            input_id, output_id = reused_user["id"] if reused_user else uid(), uid()
            user_message = reused_user or {
                "id": input_id,
                "parent": history[-1]["id"] if history else None,
                "role": "user",
                "raw": input_raw,
                "model": req.text,
                "display": await runtime.call("transform", input_raw, rules, 1, "display", 0),
                "snapshot": copy.deepcopy(snapshot),
            }
            # The compiler owns input insertion; memory tools may cite the pending user input.
            agent = Writer(self.store, tid, document, session, history, snapshot, req, runtime, settings)
            agent.input_message_id = input_id
            agent.retrieval_engine = self.retrieval
            self.agents[tid] = agent
            agent.retrieval = prepared
            provider = self.provider_factory() if self.provider_factory else None
            self.store.event(tid, "retrieval", {k: v for k, v in agent.retrieval.items() if k != "entries"})
            agent.compile()
            self.store.event(tid, "input_ready", {
                "message": user_message,
                "lore_injections": agent.compiled["next_lore_injections"],
            })
            text = await agent.run(provider)
            # Stage the new floor before MVU callbacks so getCurrentMessageId sees the reply.
            mirror = await runtime.call("snapshot")
            mirror["messages"].append(
                {
                    "message_id": len(history),
                    "id": input_id,
                    "role": "user",
                    "message": input_raw,
                    "variables": copy.deepcopy(snapshot["state"]),
                }
            )
            output_index = len(mirror["messages"])
            mirror["messages"].append(
                {
                    "message_id": output_index,
                    "id": output_id,
                    "role": "assistant",
                    "message": text,
                    "variables": copy.deepcopy(snapshot["state"]),
                }
            )
            await runtime.call("update", mirror)
            candidate_display = await runtime.call("transform", text, rules, 2, "display", 0)
            if card["permission"] and "<script" in candidate_display.lower():
                await runtime.add_message_frame(
                    self.dependencies.markup(card, candidate_display),
                    {**mirror, "serverMain": False, "currentMessage": output_index},
                    host,
                )
            try:
                from tavern.tracker.workflow import after_story, merge_script_lore
                mirror = await runtime.call("snapshot")
                snapshot["state"], snapshot["scopes"] = mirror["state"], mirror["scopes"]
                if mirror.get("loreModified"):
                    if session["policy_effective"] == "directed" and not req.allow_content_edit:
                        raise ValueError("卡片脚本尝试修改设定，但本轮未授权")
                    script_document = merge_script_lore(agent.document, mirror["lore"], mirror, include_existing=True)
                    if script_document != agent.document:
                        agent.document = script_document
                        snapshot["content"] = {"document": copy.deepcopy(script_document),
                                               "reason": "角色脚本修改世界书", "turn": tid}
                candidate_model = await runtime.call("transform", text, rules, 2, "model", 0)
                candidate = {"id": output_id, "parent": input_id, "role": "assistant", "raw": text,
                             "model": candidate_model, "snapshot": copy.deepcopy(snapshot)}
                task_history = [*history, user_message, candidate]
                previous_document = copy.deepcopy(agent.document)
                agent.document, operations = await after_story(self.store, tid, settings, session,
                    task_history, snapshot, req, agent.document, run_tracker, committed_history=history)
                # Only the final process call applies the staged patch and emits
                # message callbacks. Tool attempts never call into MVU.
                mirror = await runtime.call("snapshot")
                initial_lore = copy.deepcopy(mirror["lore"])
                # Post-story setting changes apply to the next task. Current
                # MVU callbacks still see the rule/worldbook baseline they began
                # with; merge their actual deltas into Tracker's draft afterward.
                processed = await runtime.call("process", text, operations, "")
                if req.update_variables:
                    self.store.event(tid, "variable_update_completed", {
                        "purpose": "tracker_post", "operations": operations,
                    })
            except Exception as error:
                self.store.event(tid, "tracker_failed", {"purpose": "tracker_post", "detail": str(error)})
                raise ValueError(
                    "后台 Tracker 或变量校验失败，本次正文与状态未提交。草稿已保留，可以重试本次发言。"
                ) from error
            snapshot["state"], snapshot["scopes"] = processed["state"], processed["scopes"]
            if processed.get("loreModified"):
                if session["policy_effective"] == "directed" and not req.allow_content_edit:
                    raise ValueError("卡片脚本尝试修改设定，但本轮未授权")
                revised = merge_script_lore(agent.document, initial_lore, processed)
                snapshot["content"] = {"document": revised, "reason": "Tracker / 角色脚本修改世界书", "turn": tid}
            mirror = await runtime.call("snapshot")
            sync_runtime_lore(mirror, (snapshot.get("content") or {}).get("document", agent.document), previous_document)
            await runtime.call("update", mirror)
            snapshot["lore"] = agent.compiled["next_lore"]
            snapshot["lore_injections"] = agent.compiled["next_lore_injections"]
            final_text = processed["text"]
            model = await runtime.call("transform", final_text, rules, 2, "model", 0)
            display = await runtime.call("transform", final_text, rules, 2, "display", 0)
            if card["permission"] and "<" in display:
                display = self.dependencies.markup(card, display)
            output = {
                "id": output_id,
                "parent": input_id,
                "role": "assistant",
                "raw": final_text,
                "retrieval_query": agent.retrieval_query,
                "model": model,
                "display": display,
                "snapshot": copy.deepcopy(snapshot),
            }
            from tavern.tracker.workflow import finalize_sources
            finalize_sources(snapshot, [user_message, output], document=agent.document, persona=session["persona"])
            # Coverage includes the final model text, not a provisional macro view.
            archive = snapshot.get("memory_archive")
            if archive and archive["covered_count"] == len(history) + 2:
                from tavern.memories.memory import fingerprint
                archive["fingerprint"] = fingerprint([*history, user_message, output])
            output["snapshot"] = copy.deepcopy(snapshot)
            pending = {
                "messages": [output] if reused_user else [user_message, output],
                "before": before,
                "reason": "Agent / MVU 回合提交",
                "message_edit_notified": list(snapshot.get(NOTIFICATIONS, [])),
            }
            from tavern.conversation.half_context import finalize_record
            pending["half_agent_record"] = finalize_record(
                agent.compiled["half_agent_record"], agent.native_response, agent.clean_response, output)
            self.store.set_turn(tid, draft=final_text)
            changed = review_snapshot(snapshot) != review_snapshot(before)
            if session["policy_effective"] == "preview" and changed:
                self.store.set_turn(tid, status="awaiting_review", pending=dump(pending))
                self.store.event(tid, "awaiting_review", {"before": before, "after": snapshot})
            else:
                self.store.commit_turn(tid, pending)
        except asyncio.CancelledError:
            self.store.set_turn(tid, status="cancelled", error="用户取消，状态未提交")
            self.store.event(tid, "cancelled", {})
        except Exception as e:
            message = str(e)
            if "MVU_UPDATE_INVALID" in message or "runtime.variableUpdate" in message:
                self.store.event(tid, "variable_update_failed", {"detail": message})
                message = "模型的变量更新无效，本次正文与状态未提交。草稿已保留，可以重试本次发言。"
            self.store.set_turn(tid, status="error", error=message)
            self.store.event(tid, "error", {"message": message})
        finally:
            self.agents.pop(tid, None)
            if runtime:
                await runtime.close()
