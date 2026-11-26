"""Application lifecycle and user actions around the story runtime."""

import copy

from starlette.concurrency import run_in_threadpool

from tavern.application.context_operations import ContextOperations
from tavern.scripting.dependencies import Dependencies
from tavern.search.local_models import create_local_model_manager
from tavern.search.retrieval import RetrievalEngine
from tavern.storage.store import Conflict
from tavern.story.runtime import StoryRuntime


class StoryService:
    def __init__(self, store):
        self.store = store
        self.dependencies = Dependencies(store)
        self.local_models = create_local_model_manager(store)
        self.retrieval = RetrievalEngine(store, self.local_models)
        self.japanese_dictionary = self.retrieval.dictionary
        self.context_operations = ContextOperations(store)
        self.story = StoryRuntime(store, self.retrieval, self.dependencies)
        from tavern.media.service import MediaService

        self.media = MediaService(store)

    async def edit_opening(self, sid, edit):
        from tavern.scripting.opening import prepare_opening

        session = self.store.session(sid)
        if session["revision"] != edit.expected_revision:
            raise Conflict("会话已改变，请刷新后重试")
        if not session["opening_editable"]:
            raise Conflict("当前不能修改开场白；仅限首次模型任务前、没有运行中操作的新对话")
        greeting = copy.deepcopy(session["messages"][0])
        greeting.update(raw=edit.text, model=edit.text, display=edit.text)
        # Repeated edits start from clean initialization, never from earlier init markers
        # or script side effects. The editor explicitly describes this reset.
        greeting["snapshot"].update(state={}, scopes={})
        greeting = await prepare_opening(
            self.story.runtime, self.dependencies, session, self.store.session_card(sid), greeting
        )
        return self.store.save_opening(session, greeting)

    async def delete_session(self, sid, *, advisor=None):
        async with self.media.deleting_session(sid):
            if advisor is not None:
                await advisor.cancel_session(sid)
            for job in self.story.novel.data.jobs(sid):
                if job["status"] != "destroyed":
                    await self.story.novel.destroy(job["id"])
            for oid in list(self.context_operations.tasks):
                if self.context_operations.get(oid)["session"] == sid:
                    await self.context_operations.cancel(oid)
            turn_ids = self.store.session_turn_ids(sid)
            for tid in turn_ids:
                if tid in self.story.tasks:
                    await self.story.cancel(tid)
                    self.story.tasks.pop(tid, None)
            async with self.dependencies.lock:
                self.store.delete_session(sid)
        self.store.prune_turn_runs(turn_ids)

    async def delete_card(self, cid):
        async with self.dependencies.lock:
            self.store.delete_card(cid)

    def prepare_indexes(self, sid):
        async def prepare(record):
            source, document, snapshot, turn_number = await run_in_threadpool(
                self.store.session_index_source, sid
            )
            await self.story.retrieval.prepare(
                document,
                [],
                "",
                snapshot,
                self.store.session_settings(source),
                source,
                "prepare",
                turn_number=turn_number,
                index_only=True,
            )

        try:
            return self.local_models.job("prepare:" + sid, prepare)
        except ValueError:
            return None

    async def close(self):
        await self.media.close()
        self.japanese_dictionary.stop()
        await self.context_operations.close()
        await self.story.close()
        await self.local_models.close()
        await self.japanese_dictionary.close()
        await self.dependencies.close()
