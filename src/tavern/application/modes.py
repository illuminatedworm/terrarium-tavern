"""Application routing only. The two story engines never invoke one another."""

from tavern.application.service import RoleplayRuntime
from tavern.full_agent.runtime import FullAgentRuntime


class ModeRuntime:
    def __init__(self, store):
        self.store = store
        from tavern.application.context_operations import ContextOperations

        self.half = RoleplayRuntime(store)
        self.context_operations = ContextOperations(store, self.half.retrieval)
        self.full = FullAgentRuntime(store, self.half.retrieval, self.half.dependencies)

    def __getattr__(self, name):
        # Card IO, review, and user edits are mode-independent application actions.
        return getattr(self.half, name)

    @property
    def tasks(self):
        return {**self.half.tasks, **self.full.tasks}

    @property
    def provider_factory(self):
        return self.half.provider_factory

    @provider_factory.setter
    def provider_factory(self, value):
        self.half.provider_factory = self.full.provider_factory = value

    @property
    def memory_provider_factory(self):
        return self.half.memory_provider_factory

    @memory_provider_factory.setter
    def memory_provider_factory(self, value):
        self.half.memory_provider_factory = value

    @property
    def variable_provider_factory(self):
        return self.half.variable_provider_factory

    @variable_provider_factory.setter
    def variable_provider_factory(self, value):
        self.half.variable_provider_factory = value

    def engine(self, sid):
        return self.full if self.store.session(sid)["agent_mode"] == "full_agent" else self.half

    def submit(self, sid, request):
        return self.engine(sid).submit(sid, request)

    async def initialize_session(self, sid):
        return await self.engine(sid).initialize_session(sid)

    async def cancel(self, tid):
        await self.engine(self.store.turn(tid)["session"]).cancel(tid)

    async def delete_session(self, sid):
        for oid in list(self.context_operations.tasks):
            if self.context_operations.get(oid)["session"] == sid:
                await self.context_operations.cancel(oid)
        for tid in self.store.session_turn_ids(sid):
            if tid in self.full.tasks:
                await self.full.cancel(tid)
                self.full.tasks.pop(tid, None)
        await self.half.delete_session(sid)

    async def interaction(self, sid, payload):
        if self.store.session(sid)["agent_mode"] == "full_agent":
            raise ValueError("Full Agent 暂不支持角色 HTML 中的交互生成；请通过聊天输入和变量工具操作")
        return await self.half.interaction(sid, payload)

    def prepare_indexes(self, sid):
        if self.store.session(sid)["agent_mode"] != "full_agent":
            return self.half.prepare_indexes(sid)

        async def prepare(record):
            s = self.store.session(sid)
            document = (s["snapshot"].get("content") or {}).get(
                "document", self.store.session_card(sid)["document"]
            )
            await self.full.retrieval.prepare(
                document, [], "", s["snapshot"], self.store.session_settings(s), s, "prepare", index_only=True
            )

        try:
            return self.local_models.job("prepare:" + sid, prepare)
        except ValueError:
            return None

    async def close(self):
        self.japanese_dictionary.stop()
        await self.context_operations.close()
        await self.full.close()
        await self.half.close()
