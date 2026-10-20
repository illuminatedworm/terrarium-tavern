"""Story execution and display share the same explicit permission record."""

import copy

import pytest
from httpx import ASGITransport, AsyncClient

from tavern.application.api import create_app
from tavern.character.card_author_store import EditorStore
from tavern.core.models import SessionCreate, TurnRequest
from tavern.storage.store import Conflict, Store, dump


def create_story(store):
    card = store.import_card(
        dump({"name": "面板权限", "first_mes": "开场", "extensions": {"unknown": {"keep": True}}}).encode(),
        "card.json",
    )
    return card, store.new_session(SessionCreate(card_id=card["id"]))


async def test_permission_endpoint_is_story_owned_and_does_not_replay_or_edit_data(tmp_path):
    app = create_app(tmp_path)
    store = app.state.store
    card, session = create_story(store)
    original_card = store.session_card(session["id"])
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1") as client:
            result = await client.put(
                f"/api/sessions/{session['id']}/script-permission",
                json={"enabled": True, "expected_revision": session["revision"]},
            )
            assert result.status_code == 200, result.text
            updated = result.json()
            assert updated["card"] == original_card | {"permission": 1}
            assert updated["session"]["messages"] == session["messages"]
            assert updated["session"]["snapshot"] == session["snapshot"]
            assert store.card(card["id"])["permission"] == 0
            assert updated["session"]["revision"] == session["revision"] + 1
            fork = store.fork_session(session["id"], session["head"], updated["session"]["revision"])
            assert store.session_card(fork["id"])["permission"] == 1
            disabled = await client.put(
                f"/api/sessions/{session['id']}/script-permission",
                json={"enabled": False, "expected_revision": updated["session"]["revision"]},
            )
            assert disabled.status_code == 200
            assert disabled.json()["card"]["permission"] == 0
            assert store.session_card(fork["id"])["permission"] == 1
            assert Store(tmp_path).session_card(session["id"])["permission"] == 0
    finally:
        await app.state.editor.close()
        await app.state.service.close()


@pytest.mark.parametrize("status", ["queued", "running", "awaiting_review"])
def test_permission_changes_do_not_race_an_active_turn(tmp_path, status):
    store = Store(tmp_path)
    _, session = create_story(store)
    tid, _ = store.create_turn(
        session["id"], TurnRequest(text="下一轮", expected_revision=0, operation_id="turn")
    )
    store.set_turn(tid, status=status)
    before = copy.deepcopy(store.session_card(session["id"]))
    with pytest.raises(Conflict):
        store.set_session_script_permission(session["id"], 0, True)
    assert store.session_card(session["id"]) == before
    assert store.session(session["id"])["revision"] == 0


def test_stale_permission_change_does_not_overwrite_current_choice(tmp_path):
    store = Store(tmp_path)
    _, session = create_story(store)
    store.set_session_script_permission(session["id"], 0, True)
    with pytest.raises(Conflict):
        store.set_session_script_permission(session["id"], 0, False)
    assert store.session_card(session["id"])["permission"] == 1


@pytest.mark.parametrize("initial,current", [(0, 1), (1, 0)])
def test_editor_copy_inherits_current_user_permission_instead_of_stale_draft(tmp_path, initial, current):
    store = Store(tmp_path)
    card, _ = create_story(store)
    with store.connection() as db:
        db.execute("UPDATE cards SET permission=? WHERE id=?", (initial, card["id"]))
    edits = EditorStore(store)
    workspace = edits.create(card["id"])
    with store.connection() as db:
        db.execute("UPDATE cards SET permission=?,revision=revision+1 WHERE id=?", (current, card["id"]))
    copied = edits.save(workspace["id"], workspace["revision"], "copy", "save-copy")
    assert copied["permission"] == current
    assert copied["document"]["extensions"] == card["document"]["extensions"]
