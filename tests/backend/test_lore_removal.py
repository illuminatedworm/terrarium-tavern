import json

import pytest
from httpx import ASGITransport, AsyncClient

from tavern.application.api import create_app
from tavern.core.models import SessionCreate, TurnRequest
from tavern.storage.store import Conflict, Store, dump, uid
from tests.backend.test_conversation_lore import compile, document, message


def seed(store):
    card = store.import_card(json.dumps({"first_mes": "欢迎", **document()}).encode(), "card.json")
    session = store.new_session(SessionCreate(card_id=card["id"]))
    out = compile(document(), session["messages"], session["snapshot"])
    snapshot = session["snapshot"] | {"lore": out["next_lore"], "lore_injections": out["next_lore_injections"]}
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
    for _ in range(3):
        out = compile(document(), changed["messages"], changed["snapshot"])
        assert out["next_lore_injections"] == [tombstone]
        assert out["next_lore"] == {"e": {"present": True}}
        assert not any(m.get("lore_ids") == ["e"] for m in out["trace"])
        assert "灯塔：" not in json.dumps(out["messages"], ensure_ascii=False)
        assert "manually_removed" not in json.dumps(out["messages"])
        changed["snapshot"].update(lore=out["next_lore"], lore_injections=out["next_lore_injections"])
    # Natural removal of the anchor starts cooldown; no same-request reinjection.
    history = [message("u", "user", "继续"), message("a", "assistant", "好")]
    exited = compile(document(), history, changed["snapshot"])
    assert exited["next_lore_injections"] == []
    assert exited["next_lore"] == {"e": {"present": False, "removed_at": 2}}
    restored = Store(tmp_path).session(session["id"])
    assert restored["snapshot"]["lore_injections"] == [tombstone]
    fork = store.fork_session(session["id"], session["head"], changed["revision"])
    assert fork["snapshot"]["lore_injections"][0]["anchor_id"] == fork["head"]
    assert not compile(document(), fork["messages"], fork["snapshot"])["lore"]


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
    store.create_turn(session["id"], TurnRequest(text="继续", expected_revision=0, operation_id="removal-test"))
    with pytest.raises(Conflict):
        store.remove_lore_injection(*args, 0)


async def test_removal_api(tmp_path):
    app = create_app(tmp_path)
    session = seed(app.state.store)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.request("DELETE", f'/api/sessions/{session["id"]}/lore-injections/{session["head"]}/e',
                                        json={"expected_revision": 0})
    assert response.status_code == 200, response.text
    assert response.json()["snapshot"]["lore_injections"][0]["manually_removed"]
    await app.state.service.close()


@pytest.mark.parametrize("greeting_anchor", [True, False])
def test_regeneration_from_pre_injection_user_snapshot_keeps_removal(tmp_path, greeting_anchor):
    store = Store(tmp_path)
    card = store.import_card(json.dumps({"first_mes": "欢迎", **document()}).encode(), "card.json")
    session = store.new_session(SessionCreate(card_id=card["id"]))
    user_id, reply_id = uid(), uid()
    anchor = session["head"] if greeting_anchor else user_id
    record = {"id": "e", "anchor_id": anchor, "title": "灯塔", "content": "秘密设定"}
    snapshot = session["snapshot"] | {"lore": {"e": {"present": True}}, "lore_injections": [record]}
    with store.connection() as db:
        for mid, parent, role, snap in [(user_id, session["head"], "user", session["snapshot"]),
                                        (reply_id, user_id, "assistant", snapshot)]:
            db.execute("INSERT INTO messages(id,session,parent,role,raw,model,display,snapshot,created) VALUES (?,?,?,?,?,?,?,?,?)",
                       (mid, session["id"], parent, role, "灯塔", "灯塔", "灯塔", dump(snap), 1))
        db.execute("UPDATE sessions SET head=? WHERE id=?", (reply_id, session["id"]))
    changed = store.remove_lore_injection(session["id"], anchor, "e", 0)
    before = changed["messages"][-2]["snapshot"]
    assert before["lore_injections"] == [{"id": "e", "anchor_id": anchor, "manually_removed": True}]
    from tavern.conversation.context import compile_context
    from tests.backend.test_conversation_lore import Settings
    out = compile_context(document(), changed, changed["messages"][:-2], before, "灯塔", Settings(), "regen",
                          input_message_id=user_id)
    assert out["next_lore"] == {"e": {"present": True}}
    assert out["next_lore_injections"] == before["lore_injections"]
    assert all(not m.get("lore_ids") for m in out["trace"])
