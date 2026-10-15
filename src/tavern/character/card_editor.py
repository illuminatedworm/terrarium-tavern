"""Independent Card Editor service and durable, explicitly resumed operations."""
import asyncio
import json
import time

from tavern.character.card_editor_store import EditorStore
from tavern.character.editor_config import SYSTEM as SYSTEM
from tavern.character.editor_run import EditorRun
from tavern.character.editor_runtime_config import editor_runtime_config
from tavern.core.provider import make_provider, require_model_connection
from tavern.storage.store import Conflict, dump, uid


class CardEditorService:
    def __init__(self, store):
        self.store, self.edits = store, EditorStore(store)
        self.tasks = {}
        self.provider_factory = None
        self.web_transport = None

    async def close(self):
        await asyncio.gather(*(self.cancel(rid) for rid in list(self.tasks)), return_exceptions=True)

    def submit(self, wid, request, *, legacy=True):
        rid = uid()
        payload = request.model_dump()
        payload["_legacy"] = legacy
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute("SELECT id FROM card_edit_runs WHERE workspace=? AND operation=?", (wid, request.operation_id)).fetchone()
            if prior:
                return self.edits.run(prior[0])
            self.edits.check(db, wid, request.expected_revision, idle=False)
            if db.execute("SELECT 1 FROM card_edit_runs WHERE workspace=? AND status IN ('queued','running')", (wid,)).fetchone():
                raise Conflict("当前已有运行中的编辑任务")
            pending = db.execute("SELECT run,payload FROM card_editor_progress WHERE workspace=?", (wid,)).fetchone()
            if request.resume_run_id:
                if not pending or pending[0] != request.resume_run_id:
                    raise Conflict("只能继续当前对话最新的未完成进度")
                payload["_input_ref"] = json.loads(pending[1])["current_refs"][-1]
            elif pending:
                raise Conflict("请先继续或丢弃本轮未完成进度")
            elif not request.text.strip():
                raise ValueError("请输入制卡要求")
            cfg = editor_runtime_config(self.store, wid, payload)
            if not self.provider_factory:
                require_model_connection(cfg)
            if request.text.strip():
                payload["_input_ref"] = self.edits.message(db, wid, "user", request.text, rid)
            db.execute("INSERT INTO card_edit_runs(id,workspace,operation,request,status,created) VALUES (?,?,?,?,'queued',?)",
                       (rid, wid, request.operation_id, dump(payload), time.time()))
            db.execute("UPDATE card_editor_conversations SET updated=? WHERE id=?", (time.time(), wid))
        # A queued task can be stopped before its coroutine runs. Persist its
        # baseline now so shutdown/immediate cancellation remains resumable.
        try:
            owner = EditorRun(self, rid)
            owner.save()
        except Exception as error:
            with self.store.connection() as db:
                db.execute("UPDATE card_edit_runs SET status='error',error=? WHERE id=?", (str(error), rid))
            raise
        task = asyncio.create_task(self.run(rid, owner))
        self.tasks[rid] = task
        task.add_done_callback(lambda done: self.tasks.pop(rid, None))
        return self.edits.run(rid)

    async def cancel(self, rid):
        self.edits.run(rid)
        task = self.tasks.get(rid)
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        with self.store.connection() as db:
            changed = db.execute("UPDATE card_edit_runs SET status='cancelled',error='已停止，可继续或丢弃本轮' WHERE id=? AND status IN ('queued','running')", (rid,)).rowcount
        if changed:
            self.edits.event(rid, "cancelled", {})
        return self.edits.run(rid)

    async def run(self, rid, owner=None):
        provider = None
        try:
            with self.store.connection() as db:
                db.execute("UPDATE card_edit_runs SET status='running' WHERE id=?", (rid,))
            owner = owner or EditorRun(self, rid)
            owner.save()
            provider = self.provider_factory() if self.provider_factory else await make_provider(owner.cfg,
                lambda payload: self.edits.event(rid, "provider_request", payload))
            await owner.run(provider)
        except asyncio.CancelledError:
            with self.store.connection() as db:
                db.execute("UPDATE card_edit_runs SET status='cancelled',error='已停止，未提交进度保留，可继续或丢弃' WHERE id=?", (rid,))
            self.edits.event(rid, "cancelled", {})
        except Exception as error:
            with self.store.connection() as db:
                db.execute("UPDATE card_edit_runs SET status='error',error=? WHERE id=?", (str(error), rid))
            self.edits.event(rid, "error", {"message": str(error)})
        finally:
            if provider:
                await provider.close()
