import json

import pytest

from tavern.core.models import SessionCreate, TurnRequest
from tavern.memories.memory import fingerprint
from tavern.storage.store import Conflict, Store
from tests.support.memory_fixtures import memory_snapshot


def setup_session(tmp_path):
    store = Store(tmp_path)
    card = store.import_card(json.dumps({"name": "test", "first_mes": "hello"}).encode(), "card.json")
    return store, store.new_session(SessionCreate(card_id=card["id"]))


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
def test_edit_notice_requires_verified_memory_coverage(tmp_path, coverage):
    store, session = setup_session(tmp_path)
    mid = session["messages"][0]["id"]
    snapshot = session["snapshot"]
    snapshot.update(memory_snapshot("当前记忆文本"))
    snapshot["memory"] = [{"content": "当前记忆条目", "source": mid, "kind": "fact", "excerpt": "hello"}]
    if coverage != "missing":
        snapshot["memory_archive"] = {"covered_count": 1, "fingerprint": fingerprint(session["messages"]) if coverage == "valid" else "stale"}
    with store.connection() as db:
        db.execute("UPDATE messages SET snapshot=? WHERE id=?", (json.dumps(snapshot), mid))
    edited = store.edit_message(session["id"], mid, "changed", session["revision"])
    assert edited["edit_affected_memory"] is (coverage == "valid")
    assert edited["snapshot"]["memory_document"]["records"][0]["content"] == "当前记忆文本"
    assert edited["snapshot"]["memory"] == snapshot["memory"]
    if coverage == "valid":
        assert edited["snapshot"]["memory_archive"]["fingerprint"] == fingerprint(edited["messages"])
