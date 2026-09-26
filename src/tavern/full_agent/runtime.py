"""Thin Full Agent host: authorization, script IO, cancellation, atomic commit."""
import asyncio
import copy
import os

from tavern.character.cards import regex_scripts
from tavern.core.models import TurnRequest
from tavern.full_agent.agent import FullAgent
from tavern.full_agent.lore import RetrievalLane
from tavern.scripting.runtime import ScriptRuntime
from tavern.storage.store import dump, uid


class FullAgentRuntime:
    def __init__(self, store, retrieval, dependencies):
        self.store, self.dependencies = store, dependencies
        self.retrieval = RetrievalLane(retrieval)
        self.runtime = ScriptRuntime()
        self.tasks, self.agents = {}, {}
        self.provider_factory = None

    def submit(self, sid, request):
        if self.store.session(sid)["agent_mode"] != "full_agent":
            raise ValueError("此会话不是 Full Agent 模式")
        if request.scene_refresh or request.generate is not None:
            raise ValueError("Full Agent 使用笔记工具管理记忆，不接受 Half Agent 的后台重建或上下文覆盖请求")
        settings = self.store.settings().for_role("full_agent")
        if self.provider_factory is None:
            if not settings.model:
                raise ValueError("请先配置 Full Agent 模型，或配置其沿用的 Writer 模型")
            if not (settings.api_key or os.environ.get("TAVERN_API_KEY", "")):
                raise ValueError("请先配置 API Key；本地免密端点可填 local")
        tid, created = self.store.create_turn(sid, request)
        if created:
            self.tasks[tid] = asyncio.create_task(self.run(tid))
            def completed(task):
                for key in [k for k, v in self.tasks.items() if v.done()][:-30]:
                    self.tasks.pop(key, None)
            self.tasks[tid].add_done_callback(completed)
        return self.store.turn(tid)

    async def close(self):
        for task in self.tasks.values():
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)
        await self.runtime.close()

    async def cancel(self, tid):
        task = self.tasks.get(tid)
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        elif self.store.turn(tid)["status"] == "awaiting_review":
            self.store.set_turn(tid, status="cancelled", pending=None)
            self.store.event(tid, "cancelled", {})

    async def initialize_session(self, sid):
        session = self.store.session(sid)
        card = self.store.session_card(sid)
        if card["permission"]:
            await self.dependencies.resolve(card)
        async def host(method, args):
            if method == "log":
                return None
            raise ValueError("开场初始化不允许调用模型")
        script = await self.runtime.open(card["document"], session["messages"], session["snapshot"],
            bool(card["permission"]), host, self.dependencies.scripts(card),
            lambda html: self.dependencies.markup(card, html), persona=session["persona"],
            opening=True, greeting_id=session.get("greeting_id"))
        try:
            mirror = await script.call("snapshot")
            snapshot = session["snapshot"]
            snapshot.update(state=mirror["state"], scopes=mirror["scopes"])
            greeting = session["messages"][-1]
            rules = regex_scripts(card["document"])
            model = await script.call("transform", greeting["raw"], rules, 2, "model", 0)
            display = await script.call("transform", greeting["raw"], rules, 2, "display", 0)
            if card["permission"] and "<" in display:
                display = self.dependencies.markup(card, display)
            with self.store.connection() as db:
                db.execute("UPDATE messages SET snapshot=?,model=?,display=? WHERE id=?",
                           (dump(snapshot), model, display, greeting["id"]))
        finally:
            await script.close()
        return self.store.session(sid)

    async def run(self, tid):
        script = None
        try:
            turn = self.store.turn(tid)
            request = TurnRequest.model_validate(turn["request"])
            session = self.store.session(turn["session"])
            settings = self.store.settings().for_role("full_agent").model_copy(deep=True)
            if request.reasoning_effort is not None:
                settings.reasoning_effort = request.reasoning_effort
            history, reused = session["messages"], None
            if request.mode == "regenerate":
                target = request.message_id or session["head"]
                index = next((i for i, m in enumerate(history) if m["id"] == target), None)
                if index is None or index == 0 or history[index]["role"] != "assistant":
                    raise ValueError("请选择可重生成的角色回复")
                history = history[:index]
                if history[-1]["role"] == "user":
                    reused = copy.deepcopy(history.pop())
                    request.text = reused["raw"]
            if not request.text.strip():
                raise ValueError("请输入消息")
            from tavern.full_agent.state import new_state
            before = copy.deepcopy(reused["snapshot"] if reused else history[-1]["snapshot"] if history else
                {"state": {}, "scopes": {}, "lore": {}, "content": None, "full_agent": new_state()})
            snapshot = copy.deepcopy(before)
            card = self.store.session_card(session["id"])
            document = copy.deepcopy((snapshot.get("content") or {}).get("document", card["document"]))
            self.store.set_turn(tid, status="running")
            self.store.event(tid, "started", {"purpose": "full_agent"})
            if card["permission"]:
                await self.dependencies.resolve({**card, "document": document})
            async def host(method, args):
                if method == "log":
                    self.store.event(tid, "script_log", args)
                    return None
                if method == "cancel":
                    return False
                raise ValueError("Full Agent 的角色脚本不允许发起额外模型请求；请使用当前 Agent 的工具")
            script = await self.runtime.open(document, history, snapshot, bool(card["permission"]), host,
                self.dependencies.scripts({**card, "document": document}),
                lambda html: self.dependencies.markup(card, html), persona=session["persona"])
            mirror = await script.call("snapshot")
            snapshot.update(state=mirror["state"], scopes=mirror["scopes"])
            rules, input_raw = regex_scripts(document), request.text
            request.text = await script.call("transform", input_raw, rules, 1, "model", 0)
            input_id, output_id = reused["id"] if reused else uid(), uid()
            user = reused or {"id": input_id, "parent": history[-1]["id"] if history else None, "role": "user", "raw": input_raw,
                "model": request.text, "display": await script.call("transform", input_raw, rules, 1, "display", 0),
                "snapshot": copy.deepcopy(snapshot)}
            for mid, role, text in ((input_id, "user", input_raw), (output_id, "assistant", "")):
                mirror["messages"].append({"message_id": len(mirror["messages"]), "id": mid, "role": role,
                                           "message": text, "variables": copy.deepcopy(snapshot["state"])})
            await script.call("update", mirror)
            agent = FullAgent(self.store, tid, document, session, history, snapshot, request, script,
                              settings, user, self.retrieval)
            self.agents[tid] = agent
            text = await agent.run(self.provider_factory() if self.provider_factory else None)
            mirror = await script.call("snapshot")
            mirror["messages"][-1]["message"] = text
            await script.call("update", mirror)
            candidate = await script.call("transform", text, rules, 2, "display", 0)
            if card["permission"] and "<script" in candidate.lower():
                await script.add_message_frame(self.dependencies.markup(card, candidate),
                    {**mirror, "serverMain": False, "currentMessage": len(mirror["messages"]) - 1}, host)
            processed = await script.call("process", text, [], "")
            snapshot.update(state=processed["state"], scopes=processed["scopes"])
            if processed.get("loreModified"):
                if session["policy_effective"] == "directed" and not request.allow_content_edit:
                    raise ValueError("卡片脚本尝试修改设定，但本轮未授权")
                revised = copy.deepcopy(agent.document)
                revised.get("data", revised).setdefault("character_book", {})["entries"] = processed["lore"]
                snapshot["content"] = {"document": revised, "reason": "角色脚本修改世界书", "turn": tid}
            text = processed["text"]
            model = await script.call("transform", text, rules, 2, "model", 0)
            display = await script.call("transform", text, rules, 2, "display", 0)
            if card["permission"] and "<" in display:
                display = self.dependencies.markup(card, display)
            output = {"id": output_id, "parent": input_id, "role": "assistant", "raw": text,
                      "model": model, "display": display, "snapshot": snapshot, "retrieval_query": agent.retrieval_query}
            pending = {"messages": [output] if reused else [user, output], "before": before,
                       "reason": "Full Agent 回合提交", "full_agent_trace": agent.new_trace}
            if session["policy_effective"] == "preview" and snapshot != before:
                self.store.set_turn(tid, status="awaiting_review", pending=dump(pending))
                self.store.event(tid, "awaiting_review", {"before": before, "after": snapshot})
            else:
                self.store.commit_turn(tid, pending)
        except asyncio.CancelledError:
            self.store.set_turn(tid, status="cancelled", error="用户取消，草稿未提交")
            self.store.event(tid, "cancelled", {})
        except Exception as error:
            self.store.set_turn(tid, status="error", error=str(error))
            self.store.event(tid, "error", {"message": str(error)})
        finally:
            self.agents.pop(tid, None)
            if script:
                await script.close()
