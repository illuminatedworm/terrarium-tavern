import json

import pytest
from httpx import ASGITransport, AsyncClient

from tavern.application.api import create_app
from tavern.core.models import SessionCreate, TurnRequest
from tavern.storage.store import Conflict, Store, dump
from tavern.story.context import compile_window, load_trace
from tavern.story.lore_records import message_lore_previews, saved_input_ready, turn_lore_previews
from tests.backend.test_lore_recovery import document, entry
from tests.backend.test_story import Model, Script, ScriptHost, runtime, turn


def seed(store):
    card = store.import_card(json.dumps(document(entry())).encode())
    session = store.new_session(SessionCreate(card_id=card["id"]))
    snapshot = session["snapshot"] | {
        "lore": {"e": {"present": True}},
        "lore_injections": [
            {
                "id": "e",
                "title": "灯塔",
                "content": "隐藏的世界书正文",
                "anchor_id": session["head"],
                "source_ids": [session["head"]],
            }
        ],
    }
    with store.connection() as db:
        db.execute("UPDATE messages SET snapshot=? WHERE id=?", (dump(snapshot), session["head"]))
    return store.session(session["id"])


async def test_removal_hides_content_but_defers_cooldown_until_window_exit(tmp_path):
    store = Store(tmp_path)
    tower, port = "钟楼里挂着铜铃。", "旧港的末班船在黄昏出航。"
    card = document(entry(keys=["钟楼"], content=tower), entry("port", keys=["旧港"], content=port))
    saved = store.import_card(json.dumps(card).encode())
    session = store.new_session(SessionCreate(card_id=saved["id"]))
    settings = store.settings()
    settings.retrieval.channels = ["keyword"]
    store.save_settings(settings)
    reader = ("read_lore_full", {"refs": ["L1", "L2"]})
    service = runtime(store)
    try:
        result, session = await turn(
            service, session, Model(("recall_lore", {"query": "旧港"}), reader, reader, "继续。")
        )
        assert result["status"] == "committed", result["error"]
    finally:
        await service.close()
    branch = store.fork_session(session["id"], session["head"], session["revision"])
    anchor = branch["messages"][0]["id"]
    changed = store.remove_lore_injection(branch["id"], anchor, "e", branch["revision"])
    tombstone = {"id": "e", "anchor_id": anchor, "manually_removed": True}
    assert changed["snapshot"]["lore_injections"] == [tombstone]
    assert changed["snapshot"]["lore"] == branch["snapshot"]["lore"]
    assert store.session(session["id"])["snapshot"]["lore_injections"][0]["content"] == tower
    trace = load_trace(store, changed["id"], changed["messages"])
    reads = [row["message"] for row in trace if "_lore_anchors" in row["message"]]
    assert [row["_lore_anchors"] for row in reads] == [{"e": anchor}] * 2
    assert all(tower in row["content"] for row in reads)
    del reads[0]["_lore_anchors"]  # A schema 34 read beside a new read of the same batch.
    settings.context_strategy = "openai_compact"
    compiled = compile_window(
        card, changed, changed["messages"], changed["snapshot"], "继续", settings, [], trace=trace
    )
    assert tower not in dump(compiled["_compact_body"])
    batches = [json.loads(m["content"])["data"] for m in compiled["messages"] if m["role"] == "tool"]
    assert [data["entries"] for data in batches if "entries" in data] == [[{"ref": "L2", "text": port}]] * 2


def test_removal_checks_revision_scope_and_active_turn(tmp_path):
    store = Store(tmp_path)
    session = seed(store)
    args = (session["id"], session["head"], "e")
    with pytest.raises(Conflict):
        store.remove_lore_injection(*args, 99)
    with pytest.raises(ValueError):
        store.remove_lore_injection(session["id"], "other", "e", 0)
    with pytest.raises(ValueError):
        store.remove_lore_injection(session["id"], session["head"], "missing", 0)
    assert store.session(session["id"])["snapshot"] == session["snapshot"]
    store.create_turn(
        session["id"], TurnRequest(text="继续", expected_revision=0, operation_id="removal-test")
    )
    with pytest.raises(Conflict):
        store.remove_lore_injection(*args, 0)


async def test_removal_api(tmp_path):
    app = create_app(tmp_path)
    session = seed(app.state.store)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.request(
            "DELETE",
            f"/api/sessions/{session['id']}/lore-injections/{session['head']}/e",
            json={"expected_revision": 0},
        )
    assert response.status_code == 200, response.text
    assert response.json()["snapshot"]["lore_injections"][0]["manually_removed"]
    await app.state.service.close()


async def test_same_turn_edit_keeps_the_original_admission_with_its_reply(tmp_path):
    from tavern.core.message_adapter import unwrapped
    from tavern.storage.transfer import ExportSelection, export_package, import_package, read_package

    store = Store(tmp_path)
    card = document(entry(keys=["钟楼"], comment="{{user}}的钟楼", content="旧钟楼九点关门。"))
    saved = store.import_card(dump(card).encode())
    settings = store.settings()
    settings.retrieval.channels = ["keyword"]
    store.save_settings(settings)
    session = store.new_session(SessionCreate(card_id=saved["id"], persona="小林", policy="review"))
    sid, anchor = session["id"], session["head"]
    service = runtime(store)
    model = Model(
        (
            "revise_content",
            {
                "operations": [
                    {
                        "op": "update_lore",
                        "ref": "L1",
                        "changes": {"title": "新钟楼", "content": "现在全天开放。"},
                    }
                ],
                "comment": "重建",
            },
        ),
        "我们走过钟楼。",
    )
    try:
        attempt, _ = await turn(service, session, model, allow_content_edit=True)
        assert attempt["status"] == "awaiting_review", attempt["error"]
        original = next(
            json.loads(unwrapped(m["content"], "lorebook"))
            for m in model.requests[0]["messages"]
            if m.get("content", "").startswith("<lorebook>")
        )
        assert original["title"] == "小林的钟楼" and original["text"] == "旧钟楼九点关门。"
        expected = [{"id": "e", "payload": original}]
        tid = attempt["id"]
        ready = saved_input_ready(store, tid)
        assert ready["message"]["id"] == attempt["pending"]["messages"][0]["id"]
        assert ready["lore_admissions"] == [{"anchor_id": anchor, "id": "e", "title": "小林的钟楼"}]
        assert turn_lore_previews(store, sid, tid, anchor, payload=True) == expected
        store.review_turn(tid, True)
        current = store.session(sid)
        assert current["snapshot"]["lore_injections"] == []
        assert current["messages"][-1]["lore_admissions"] == ready["lore_admissions"]
        assert message_lore_previews(store, sid, current["head"], anchor, payload=True) == expected
        archive, blobs = read_package(
            export_package(store, ExportSelection(settings=False, sessions=[sid])), store
        )
        restored = store.session(import_package(store, archive, blobs)["sessions"][0])
        restored_anchor = restored["messages"][0]["id"]
        assert (
            message_lore_previews(store, restored["id"], restored["head"], restored_anchor, payload=True)
            == expected
        )
        removed = store.remove_lore_injection(restored["id"], restored_anchor, "e", restored["revision"])
        assert "lore_admissions" not in removed["messages"][-1]
        assert removed["snapshot"]["lore_injections"] == []
    finally:
        await service.close()


async def test_script_admission_survives_failure_before_progress_and_expires_with_logs(tmp_path):
    from tavern.storage.maintenance import cleanup

    class Startup(ScriptHost):
        async def open(self, document, history, snapshot, *args, **kwargs):
            script = Script(history, snapshot)
            script.mirror.update(
                lore=[entry("temporary", keys=["钟楼"], content="脚本新增的钟楼。")], loreModified=True
            )
            return script

    def unavailable():
        raise ValueError("模型连接不可用")

    app = create_app(tmp_path)
    store, service = app.state.store, app.state.service
    saved = store.import_card(dump(document()).encode())
    settings = store.settings()
    settings.retrieval.channels = ["keyword"]
    store.save_settings(settings)
    session = store.new_session(SessionCreate(card_id=saved["id"]))
    service.story.runtime, service.story.provider_factory = Startup(), unavailable
    sid, anchor = session["id"], session["head"]
    try:
        tid = service.story.submit(
            sid,
            TurnRequest(
                text="钟楼",
                operation_id="first-failure",
                expected_revision=session["revision"],
                allow_content_edit=True,
            ),
        )["id"]
        await service.story.tasks[tid]
        assert store.story_progress(tid) is None
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
            restored = (await client.get(f"/api/turns/{tid}?include_input=true")).json()
            assert (
                restored["status"] == "error"
                and restored["input_ready"]["lore_admissions"][0]["id"] == "temporary"
            )
            url = f"/api/sessions/{sid}/story/turns/{tid}/lore-previews?anchor_id={anchor}"
            assert (await client.get(url)).json() == [
                {"id": "temporary", "ref": "L1", "opening": "脚本新增的钟楼。", "truncated": False}
            ]
            store.update_session(sid, session["revision"], title="后来修改的标题")
            with store.connection() as db:
                db.execute("UPDATE turns SET created=0 WHERE id=?", (tid,))
            cleanup(store)
            assert (await client.get(url)).status_code == 410
            assert (await client.get(f"/api/turns/{tid}?include_input=true")).json()["input_ready"] is None
    finally:
        await service.close()
