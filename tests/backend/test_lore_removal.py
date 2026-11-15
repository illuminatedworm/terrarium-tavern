import json

import pytest
from httpx import ASGITransport, AsyncClient

from tavern.application.api import create_app
from tavern.core.models import SessionCreate, TurnRequest
from tavern.storage.store import Conflict, Store, dump
from tests.backend.test_lore_recovery import document, entry


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


def test_removal_hides_content_but_defers_cooldown_until_window_exit(tmp_path):
    store = Store(tmp_path)
    session = seed(store)
    changed = store.remove_lore_injection(session["id"], session["head"], "e", session["revision"])
    tombstone = {"id": "e", "anchor_id": session["head"], "manually_removed": True}
    assert changed["snapshot"]["lore_injections"] == [tombstone]
    assert changed["snapshot"]["lore"] == session["snapshot"]["lore"]
    from tavern.story.context import compile_window

    compiled = compile_window(
        store.session_card(session["id"])["document"],
        changed,
        changed["messages"],
        changed["snapshot"],
        "继续",
        store.settings(),
        [],
    )
    assert "隐藏的世界书正文" not in json.dumps(compiled["messages"], ensure_ascii=False)
    restored = Store(tmp_path).session(session["id"])
    assert restored["snapshot"]["lore_injections"] == [tombstone]
    fork = store.fork_session(session["id"], session["head"], changed["revision"])
    assert fork["snapshot"]["lore_injections"][0]["anchor_id"] == fork["head"]


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
