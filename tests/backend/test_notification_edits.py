import json

import pytest

from tavern.core.models import SessionCreate, TurnRequest
from tavern.memories.memory import fingerprint
from tavern.storage.store import Conflict, Store, uid
from tests.support.memory_fixtures import memory_snapshot


def setup_session(tmp_path):
    store = Store(tmp_path)
    card = store.import_card(json.dumps({"name": "test", "first_mes": "hello"}).encode(), "card.json")
    return store, store.new_session(SessionCreate(card_id=card["id"]))


def test_failed_input_can_be_edited_after_options_change_without_reusing_progress(tmp_path):
    store, session = setup_session(tmp_path)
    tid, _ = store.create_turn(session["id"], TurnRequest(text="old", operation_id="failed", expected_revision=0))
    store.event(tid, "input_ready", {"message": {"parent": session["head"], "raw": "old"}})
    store.set_turn(tid, status="error")
    with store.connection() as db:
        db.execute("INSERT INTO full_agent_progress(turn,payload) VALUES (?,?)", (tid, '{}'))
    changed = store.update_session(session["id"], 0, turn_preference="新偏好")
    assert store.turn(tid)["retry_head"] == session["head"]
    edited = store.edit_pending_input(tid, "new", changed["revision"])
    assert edited["request"]["expected_revision"] == changed["revision"]
    assert not edited["resumable"]
    store.create_turn(session["id"], TurnRequest(**{**edited["request"], "operation_id": "retry"}))
    with pytest.raises(Conflict):
        store.edit_pending_input(tid, "stale", changed["revision"])


def test_failed_input_from_another_head_is_not_editable(tmp_path):
    store, session = setup_session(tmp_path)
    tid, _ = store.create_turn(session["id"], TurnRequest(text="old", operation_id="failed", expected_revision=0))
    store.event(tid, "input_ready", {"message": {"parent": "another-branch"}})
    store.set_turn(tid, status="error")
    changed = store.update_session(session["id"], 0, turn_preference="新偏好")
    with pytest.raises(Conflict):
        store.edit_pending_input(tid, "stale", changed["revision"])


@pytest.mark.parametrize("status", ["error", "cancelled", "interrupted"])
def test_pending_edit_persists_and_can_retry(tmp_path, status):
    store, session = setup_session(tmp_path)
    tid, _ = store.create_turn(session["id"], TurnRequest(text="old", operation_id="first", expected_revision=session["revision"]))
    store.set_turn(tid, status=status, draft="old draft")
    edited = store.edit_pending_input(tid, "new", session["revision"])
    assert edited["request"]["text"] == "new"
    assert edited["draft"] == ""
    assert Store(tmp_path).turn(tid)["request"]["text"] == "new"
    request = {**edited["request"], "operation_id": "retry"}
    retry_id, _ = store.create_turn(session["id"], TurnRequest(**request))
    assert store.turn(retry_id)["request"]["text"] == "new"
    with pytest.raises(Conflict):
        store.edit_pending_input(tid, "stale", session["revision"])


@pytest.mark.parametrize("coverage", ["valid", "invalid", "missing"])
@pytest.mark.parametrize("role", ["assistant", "user"])
def test_edit_notice_requires_verified_memory_coverage(tmp_path, coverage, role):
    store, session = setup_session(tmp_path)
    mid = session["messages"][0]["id"]
    if role == "user":
        mid, reply = uid(), uid()
        tid, _ = store.create_turn(session["id"], TurnRequest(text="hello", operation_id="round", expected_revision=0))
        store.set_turn(tid, status="running")
        store.commit_turn(tid, {"before": session["snapshot"], "messages": [
            {"id": mid, "parent": session["head"], "role": "user", "raw": "hello", "model": "hello",
             "display": "hello", "snapshot": session["snapshot"]},
            {"id": reply, "parent": mid, "role": "assistant", "raw": "reply", "model": "reply",
             "display": "reply", "snapshot": session["snapshot"]},
        ]})
        session = store.session(session["id"])
    snapshot = session["snapshot"]
    snapshot.update(memory_snapshot("当前记忆文本"))
    snapshot["memory"] = [{"content": "当前记忆条目", "source": mid, "kind": "fact", "excerpt": "hello"}]
    if coverage != "missing":
        snapshot["memory_archive"] = {"covered_count": len(session["messages"]), "fingerprint": fingerprint(session["messages"]) if coverage == "valid" else "stale"}
    with store.connection() as db:
        db.execute("UPDATE messages SET snapshot=? WHERE id=?", (json.dumps(snapshot), session["head"]))
    edited = store.edit_message(session["id"], mid, "changed", session["revision"])
    assert edited["edit_affected_memory"] is (coverage == "valid")
    assert edited["head"] == session["head"]
    assert next(message for message in edited["messages"] if message["id"] == mid)["raw"] == "changed"
    assert edited["snapshot"]["memory_document"]["records"][0]["content"] == "当前记忆文本"
    assert edited["snapshot"]["memory"] == snapshot["memory"]
    if coverage == "valid":
        assert edited["snapshot"]["memory_archive"]["fingerprint"] == fingerprint(edited["messages"])
