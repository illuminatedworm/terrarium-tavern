from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from tavern.application.api import create_app
from tavern.core.models import SessionCreate, TurnRequest
from tavern.storage.store import Conflict, Store, dump, uid
from tavern.story.context import compile_window


@pytest.fixture
def story(tmp_path):
    store = Store(tmp_path)
    card = store.import_card(Path("samples/雾港来信.json").read_bytes())
    session = store.new_session(SessionCreate(card_id=card["id"]))
    parent = session["head"]
    with store.connection() as db:
        for role, text in [
            ("user", "推开门"),
            ("assistant", "欢迎"),
            ("user", "走向灯塔"),
            ("assistant", "后来"),
        ]:
            mid = uid()
            snapshot = {
                "state": {"place": text},
                "story": session["snapshot"]["story"],
                "lore": {"visits": 2},
                "scopes": {"message:0": {"note": "保留"}},
                "content": {"document": card["document"]},
            }
            db.execute(
                "INSERT INTO messages(id,session,parent,role,raw,model,display,snapshot,created) VALUES (?,?,?,?,?,?,?,?,?)",
                (mid, session["id"], parent, role, text, text, text, dump(snapshot), 1),
            )
            parent = mid
        db.execute(
            "UPDATE sessions SET head=?,revision=2,persona_data=? WHERE id=?",
            (parent, dump({"description": "旅人"}), session["id"]),
        )
    return store, store.session(session["id"])


def test_fork_copies_only_selected_prefix_and_survives_original_deletion(story):
    store, original = story
    selected = original["messages"][2]
    fork = store.fork_session(original["id"], selected["id"], original["revision"])
    assert fork["id"] != original["id"]
    assert [m["raw"] for m in fork["messages"]] == [m["raw"] for m in original["messages"][:3]]
    assert {m["id"] for m in fork["messages"]}.isdisjoint(m["id"] for m in original["messages"])
    assert fork["snapshot"] == selected["snapshot"]
    assert fork["persona_data"] == original["persona_data"]
    assert fork["card"] == original["card"]
    assert fork["title"] == f"{original['title']} · 副本"
    assert fork["turns"] == [] and fork["revision"] == 0
    assert store.session(original["id"]) == original
    store.delete_session(original["id"])
    assert store.session(fork["id"])["messages"][-1]["raw"] == selected["raw"]


def test_session_titles_are_persisted_and_forks_get_distinct_titles(story):
    store, original = story
    renamed = store.update_session(original["id"], original["revision"], title="  灯塔线  ")
    assert renamed["title"] == "灯塔线"
    assert store.sessions()[0]["title"] == "灯塔线"
    first = store.fork_session(original["id"], renamed["head"], renamed["revision"])
    second = store.fork_session(original["id"], renamed["head"], renamed["revision"])
    assert first["title"] == "灯塔线 · 副本"
    assert second["title"] == "灯塔线 · 副本 2"
    assert store.session(original["id"])["title"] == "灯塔线"
    with pytest.raises(Conflict):
        store.update_session(original["id"], original["revision"], title="过期修改")
    with pytest.raises(ValueError):
        store.update_session(original["id"], renamed["revision"], title="   ")


def test_existing_schema_adds_titles_without_losing_sessions(story):
    store, original = story
    with store.connection() as db:
        db.execute("ALTER TABLE sessions DROP COLUMN title")
    reopened = Store(store.root)
    assert reopened.session(original["id"])["title"] == store.card(original["card"])["name"]
    assert reopened.session(original["id"])["messages"] == original["messages"]


def test_selected_progress_retains_equivalent_backend_model_context(story):
    store, original = story
    selected = original["messages"][2]
    selected["snapshot"]["story"]["window"]["start"] = 1
    with store.connection() as db:
        db.execute("UPDATE messages SET snapshot=? WHERE id=?", (dump(selected["snapshot"]), selected["id"]))
    fork = store.fork_session(original["id"], selected["id"], original["revision"])
    at_progress = original | {
        "head": selected["id"],
        "messages": original["messages"][:3],
        "snapshot": selected["snapshot"],
    }
    document = selected["snapshot"]["content"]["document"]
    expected = compile_window(
        document,
        at_progress,
        at_progress["messages"],
        at_progress["snapshot"],
        "继续",
        store.settings(),
        [],
    )
    actual = compile_window(document, fork, fork["messages"], fork["snapshot"], "继续", store.settings(), [])
    assert actual["messages"] == expected["messages"]
    assert fork["snapshot"]["scopes"] == selected["snapshot"]["scopes"]
    assert fork["snapshot"]["lore"] == selected["snapshot"]["lore"]


def test_fork_rejects_stale_foreign_and_running_requests_without_partial_writes(story):
    store, original = story
    for head, revision, exception in [
        (original["head"], 1, Conflict),
        ("foreign", 2, ValueError),
        (original["messages"][1]["id"], 2, ValueError),
    ]:
        with pytest.raises(exception):
            store.fork_session(original["id"], head, revision)
    store.create_turn(original["id"], TurnRequest(text="运行中", operation_id="running", expected_revision=2))
    with pytest.raises(Conflict):
        store.fork_session(original["id"], original["head"], 2)
    assert len(store.sessions()) == 1


async def test_fork_endpoint_returns_independent_conversation(story):
    store, original = story
    app = create_app(store.root)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.post(
            f"/api/sessions/{original['id']}/fork",
            json={"message_id": original["messages"][2]["id"], "expected_revision": 2},
        )
        assert response.status_code == 201
        assert len(response.json()["messages"]) == 3
        assert response.json()["id"] != original["id"]


async def test_session_reader_preserves_historical_rendering_data(story):
    store, original = story
    sid = original["id"]
    message = original["messages"][2]
    snapshot = message["snapshot"]
    document = snapshot["content"]["document"]
    card = document.get("data", document)
    card["nickname"] = "旧称"
    record = {"id": "legacy", "anchor_id": message["parent"], "title": "旧版注入"}
    snapshot["lore_injections"] = [record]
    with store.connection() as db:
        db.execute("UPDATE messages SET snapshot=? WHERE id=?", (dump(snapshot), message["id"]))

    app = create_app(store.root)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
            full_response = await client.get(f"/api/sessions/{sid}")
            reader_response = await client.get(f"/api/sessions/{sid}", params={"reader": "true"})
            assert full_response.status_code == reader_response.status_code == 200
            full, reader = full_response.json(), reader_response.json()
            assert full["messages"][2]["snapshot"] == snapshot
            assert {**reader, "messages": full["messages"]} == full
            assert [item["id"] for item in reader["messages"]] == [item["id"] for item in full["messages"]]
            assert reader["messages"][2]["snapshot"] == {
                "content": {"document": {"name": card["name"], "nickname": "旧称"}},
                "state": snapshot["state"],
                "scopes": snapshot["scopes"],
                "lore_injections": [record],
            }
    finally:
        await app.state.service.close()


async def test_title_endpoint_updates_session_and_list(story):
    store, original = story
    app = create_app(store.root)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.put(
            f"/api/sessions/{original['id']}/title",
            json={"title": "另一条线", "expected_revision": original["revision"]},
        )
        assert response.status_code == 200
        assert response.json()["title"] == "另一条线"
        listing = await client.get("/api/sessions")
        assert listing.json()[0]["title"] == "另一条线"
