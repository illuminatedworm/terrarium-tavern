import json

import pytest
from httpx import ASGITransport, AsyncClient

from tavern.application.api import create_app
from tavern.core.models import SessionCreate, TurnRequest
from tavern.storage.store import Conflict, Store, dump
from tavern.story.context import compile_window, load_trace
from tests.backend.test_lore_recovery import document, entry
from tests.backend.test_story import Model, runtime, turn


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
