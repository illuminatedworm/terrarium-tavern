"""Cross-channel isolation, snapshot conflicts, compaction and portable storage."""
import copy
import time

import pytest
from kohakuterrarium.testing.llm import ScriptedLLM

from tavern.advisor.context import runtime_config
from tavern.advisor.runtime import ConsultationRequest, OptionsRequest
from tavern.core.model_selection import ModelSelection
from tavern.search.retrieval_config import ModelProfile, Provider
from tavern.storage.backup import backup, restore
from tavern.storage.maintenance import cleanup
from tavern.storage.store import Conflict, Store, dump, uid
from tavern.storage.transfer import ExportSelection, export_package, import_package, read_package
from tests.backend.test_advisor import Failure, ask, setup
from tests.backend.test_card_author import ToolLLM


def test_independent_models_but_immutable_channel(tmp_path):
    _, store, session, advisor = setup(tmp_path)
    settings = store.settings_document()
    settings.shared.providers += [Provider(id="independent", name="独立供应商"), Provider(id="official", name="官方", auth_mode="chatgpt_subscription")]
    settings.shared.advisor.model = ModelProfile(provider_id="independent", model="different-model")
    settings.shared.advisor.official_model = ModelProfile(provider_id="official", model="official-model")
    store.save_settings(settings)
    cfg, _ = runtime_config(store, session, {})
    assert cfg.model == "different-model"
    with pytest.raises(ValueError, match="渠道"):
        advisor.options(session["id"], OptionsRequest(expected_revision=0, model_selection=ModelSelection(provider_id="official", model="official-model")))
    with pytest.raises(ValueError, match="官方"):
        advisor.options(session["id"], OptionsRequest(expected_revision=0, context_strategy="openai_compact"))


class ConcurrentEdit(ToolLLM):
    def __init__(self, store, sid):
        super().__init__([("update_turn_preference", {"action": "replace", "content": "模型提案", "request_ref": "Q1"})])
        self.store, self.sid = store, sid

    async def chat(self, messages, **kwargs):
        if self.call_count:
            current = self.store.session(self.sid)
            self.store.update_session(self.sid, current["revision"], turn_preference="用户最新手动修改")
        async for chunk in super().chat(messages, **kwargs):
            yield chunk


async def test_concurrent_manual_edit_cannot_be_overwritten_at_commit(tmp_path):
    _, store, session, advisor = setup(tmp_path)
    result = await ask(advisor, session["id"], ConcurrentEdit(store, session["id"]), "替换偏好")
    assert result["status"] == "error"
    assert store.session(session["id"])["turn_preference"] == "用户最新手动修改"
    assert not advisor.data.get(session["id"])["exchanges"]
    with pytest.raises(Conflict, match="故事已改变"):
        await ask(advisor, session["id"], ToolLLM([]), "", result["id"])


async def test_cancel_before_start_and_reopen_preserves_progress(tmp_path):
    _, store, session, advisor = setup(tmp_path)
    advisor.provider_factory = lambda: ToolLLM([])
    request = ConsultationRequest(text="讨论", expected_revision=0, operation_id=uid())
    run = advisor.submit(session["id"], request)
    assert advisor.submit(session["id"], request)["id"] == run["id"]
    await advisor.cancel(run["id"])
    assert advisor.data.run(run["id"])["resumable"]
    reopened = Store(tmp_path)
    reopened.recover()
    with reopened.connection() as db:
        assert db.execute("SELECT progress FROM advisor_runs WHERE id=?", (run["id"],)).fetchone()[0]


async def test_local_compaction_keeps_recent_originals_and_story_intact(tmp_path):
    _, store, session, advisor = setup(tmp_path)
    settings = store.settings_document()
    settings.shared.advisor.keep_recent = 1
    store.save_settings(settings)
    for index in range(3):
        result = await ask(advisor, session["id"], ToolLLM([]), f"问题{index}")
        assert result["status"] == "completed"
    before = copy.deepcopy(store.session(session["id"]))
    model = ScriptedLLM(["咨询摘要：问题0、问题1讨论了不同可能，尚未发生。"])
    advisor.provider_factory = lambda: model
    run = advisor.submit(session["id"], ConsultationRequest(expected_revision=3, operation_id=uid(), compact=True))
    await advisor.tasks[run["id"]]
    assert advisor.data.run(run["id"])["status"] == "completed", advisor.data.run(run["id"])
    state = advisor.data.get(session["id"])
    assert "咨询摘要" in state["context"]["summary"]
    assert len(state["exchanges"]) == 3
    preview = advisor.preview(session["id"])
    assert preview["context_valid"]
    assert "问题2" in dump(preview["messages"])
    assert before["snapshot"] == store.session(session["id"])["snapshot"]
    assert all(any(text in dump(model.call_log) for text in (e["user_text"],)) for e in state["exchanges"][:2])
    with store.connection() as db:
        db.execute("UPDATE messages SET model='改写后的开场',raw='改写后的开场' WHERE id=?", (session["head"],))
    preview = advisor.preview(session["id"])
    assert not preview["context_valid"] and not preview["summary"]
    assert "问题0" in dump(preview["messages"])


async def test_only_full_backup_preserves_disposable_consultation(tmp_path):
    _, store, session, advisor = setup(tmp_path / "source")
    await ask(advisor, session["id"], ToolLLM([]), "需要保存的咨询")
    archived = export_package(store, ExportSelection(settings=False, sessions=[session["id"]]))
    target = Store(tmp_path / "target")
    result = import_package(target, *read_package(archived, target))
    from tavern.advisor.storage import AdvisorStore
    restored = AdvisorStore(target).get(result["sessions"][0])
    assert not restored["exchanges"] and not restored["context"]
    with store.connection() as db:
        db.execute("UPDATE advisor_runs SET created=?", (time.time() - 30 * 86400,))
    cleanup(store)
    assert advisor.public(session["id"])["exchanges"][0]["user_text"] == "需要保存的咨询"
    failed = await ask(advisor, session["id"], Failure([("read_history", {"ref": "H1"})]))
    destination = tmp_path / "backup.zip"
    backup(store, destination)
    recovered = restore(destination, tmp_path / "restore")
    assert AdvisorStore(recovered).run(failed["id"])["resumable"]
    # Stopped consultation progress no longer blocks exporting the story.
    export_package(store, ExportSelection(settings=False, sessions=[session["id"]]))


async def test_clear_removes_all_consultation_state_and_rejects_stale_requests(tmp_path):
    app, store, session, advisor = setup(tmp_path)
    sid = session["id"]
    advisor.options(sid, OptionsRequest(expected_revision=0, recent_exchanges=7))
    completed = await ask(advisor, sid, ToolLLM([("update_turn_preference", {
        "action": "replace", "content": "保留已保存偏好", "request_ref": "Q1",
    })]), "保存偏好")
    other = store.fork_session(sid, session["head"], store.session(sid)["revision"])
    await ask(advisor, other["id"], ToolLLM([]), "另一个故事的咨询")
    failed = await ask(advisor, sid, Failure([("update_turn_preference", {
        "action": "replace", "content": "不能复活的草稿", "request_ref": "Q1",
    })]), "替换偏好")
    with store.connection() as db:
        db.execute("UPDATE advisor_conversations SET context=? WHERE session=?", (dump({"summary": "旧摘要", "opaque": "旧密文"}), sid))
    before = copy.deepcopy(store.session(sid))
    revision = advisor.data.get(sid)["revision"]
    from httpx import ASGITransport, AsyncClient
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1") as client:
        response = await client.post(f"/api/sessions/{sid}/advisor/clear", json={"expected_revision": revision})
        assert response.status_code == 200, response.text
        assert response.json()["effective"]["recent_exchanges"] == 7
        stale = await client.post(f"/api/sessions/{sid}/advisor/clear", json={"expected_revision": revision})
        assert stale.status_code == 409
    state = advisor.data.get(sid)
    assert not state["exchanges"] and not state["context"] and state["run"] is None
    assert state["revision"] == revision + 1
    assert store.session(sid) == before
    assert len(advisor.data.get(other["id"])["exchanges"]) == 1
    with store.connection() as db:
        assert db.execute("SELECT count(*) FROM advisor_conversations WHERE session=?", (sid,)).fetchone()[0] == 1
        assert not db.execute("SELECT 1 FROM advisor_events WHERE run IN (?,?)", (completed["id"], failed["id"])).fetchone()
    with pytest.raises(Conflict, match="对话已改变"):
        advisor.submit(sid, ConsultationRequest(text="旧标签页请求", expected_revision=revision, operation_id=uid()))
    with pytest.raises(Conflict, match="只能继续最新进度"):
        await ask(advisor, sid, ToolLLM([]), "", failed["id"])
    result = await ask(advisor, sid, ToolLLM([]), "新的咨询")
    assert result["status"] == "completed"
    assert len(advisor.data.get(sid)["exchanges"]) == 1


async def test_clear_requires_stopping_active_run_and_cannot_restore_progress(tmp_path):
    _, store, session, advisor = setup(tmp_path)
    sid = session["id"]
    advisor.provider_factory = lambda: ToolLLM([])
    run = advisor.submit(sid, ConsultationRequest(text="讨论", expected_revision=0, operation_id=uid()))
    with pytest.raises(Conflict, match="请先停止"):
        advisor.data.clear(sid, 0)
    await advisor.cancel(run["id"])
    advisor.data.clear(sid, 0)
    reopened = Store(tmp_path)
    reopened.recover()
    from tavern.advisor.storage import AdvisorStore
    state = AdvisorStore(reopened).get(sid)
    assert state["run"] is None and not state["exchanges"] and not state["context"]
