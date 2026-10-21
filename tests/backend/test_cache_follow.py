import copy
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from tavern.application.api import create_app
from tavern.core.models import SessionCreate, TurnRequest
from tavern.storage.store import Conflict, Store, uid


@pytest.fixture(params=["half_agent", "full_agent"])
def story(tmp_path, request):
    store = Store(tmp_path)
    card = store.import_card(Path("samples/雾港来信.json").read_bytes(), "card.json")
    session = store.new_session(SessionCreate(card_id=card["id"], agent_mode=request.param))
    return store, session


def commit_reply(store, session, regenerate=False):
    request = TurnRequest(
        text="下一轮",
        operation_id=uid(),
        expected_revision=session["revision"],
        mode="regenerate" if regenerate else "send",
        message_id=session["head"] if regenerate else None,
    )
    turn_id, _ = store.create_turn(session["id"], request)
    store.set_turn(turn_id, status="running")
    snapshot = copy.deepcopy(session["snapshot"])
    if regenerate:
        parent = session["messages"][-1]["parent"]
        messages = []
    else:
        parent = uid()
        messages = [
            {
                "id": parent,
                "parent": session["head"],
                "role": "user",
                "raw": "输入",
                "model": "输入",
                "display": "输入",
                "snapshot": snapshot,
            }
        ]
    messages.append(
        {
            "id": uid(),
            "parent": parent,
            "role": "assistant",
            "raw": "正文",
            "model": "正文",
            "display": "正文",
            "snapshot": snapshot,
        }
    )
    pending = {"messages": messages, "before": snapshot}
    store.commit_turn(turn_id, pending)
    store.commit_turn(turn_id, pending)
    return store.session(session["id"])


def test_default_follow_advances_every_two_replies_to_second_latest(story):
    store, session = story
    assert session["cache_breakpoint_follow"] == {"interval": 2, "offset": 2, "anchor": None, "progress": 0}
    anchor = None
    for count in range(1, 7):
        session = commit_reply(store, session)
        if count % 2 == 0:
            anchor = session["messages"][-3]["id"]
        assert session["cache_breakpoint_follow"] == {
            "interval": 2,
            "offset": 2,
            "anchor": anchor,
            "progress": count % 2,
        }
        assert session["cache_breakpoint_effective_id"] == anchor
        if count == 3:
            store = Store(store.root)
            assert (
                store.session(session["id"])["cache_breakpoint_follow"] == session["cache_breakpoint_follow"]
            )


def test_follow_counts_only_new_committed_replies_and_survives_restart(story):
    store, session = story
    for _ in range(4):
        session = commit_reply(store, session)
    replies = [message["id"] for message in session["messages"][2::2]]
    session = store.set_cache_breakpoint(session["id"], session["revision"], True, None, 3, 2)
    assert session["cache_breakpoint_follow"] == {
        "interval": 3,
        "offset": 2,
        "anchor": replies[-2],
        "progress": 0,
    }
    for count in (1, 2):
        session = commit_reply(store, session)
        assert session["cache_breakpoint_follow"]["anchor"] == replies[-2]
        assert session["cache_breakpoint_follow"]["progress"] == count
    previous = session["cache_breakpoint_follow"]
    session = commit_reply(store, session, regenerate=True)
    assert session["cache_breakpoint_follow"] == previous
    session = Store(store.root).session(session["id"])
    assert session["cache_breakpoint_follow"] == previous
    session = commit_reply(store, session)
    assert session["cache_breakpoint_follow"]["progress"] == 0
    assert session["cache_breakpoint_follow"]["anchor"] == session["messages"][-3]["id"]
    with pytest.raises(Conflict):
        store.set_cache_breakpoint(session["id"], session["revision"] - 1, True, None, 2, 1)
    assert store.session(session["id"])["cache_breakpoint_follow"] == session["cache_breakpoint_follow"]


def test_manual_greeting_rejection_fork_and_auto_reset(story):
    store, session = story
    with pytest.raises(ValueError):
        store.set_cache_breakpoint(session["id"], session["revision"], False, session["head"])
    for _ in range(4):
        session = commit_reply(store, session)
    session = store.set_cache_breakpoint(session["id"], session["revision"], True, None, 3, 1)
    session = commit_reply(store, session)
    fork = store.fork_session(session["id"], session["head"], session["revision"])
    assert fork["cache_breakpoint_follow"] == {
        "interval": 3,
        "offset": 1,
        "anchor": fork["head"],
        "progress": 0,
    }
    session = store.set_cache_breakpoint(session["id"], session["revision"], False, session["head"])
    session = commit_reply(store, session, regenerate=True)
    assert session["cache_breakpoint_effective_id"] is None
    assert "当前分支" in session["cache_breakpoint_reason"]
    session = store.set_cache_breakpoint(session["id"], session["revision"], True, None)
    assert session["cache_breakpoint_follow"] == {
        "interval": 3,
        "offset": 1,
        "anchor": session["head"],
        "progress": 0,
    }


async def test_api_validation_legacy_updates_and_additive_migration(story):
    store, session = story
    session = commit_reply(store, session)
    app = create_app(store.root)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        path = f"/api/sessions/{session['id']}/cache-breakpoint"
        for value in (0, -1, 1.5, True):
            response = await client.put(
                path,
                json={"automatic": True, "expected_revision": session["revision"], "follow_interval": value},
            )
            assert response.status_code == 422
        session = store.set_cache_breakpoint(session["id"], session["revision"], True, None, 5, 1)
        response = await client.put(
            path,
            json={
                "automatic": False,
                "message_id": session["head"],
                "expected_revision": session["revision"],
            },
        )
        assert response.status_code == 200
        assert response.json()["cache_breakpoint_follow"]["interval"] == 5
    with store.connection() as database:
        database.execute("ALTER TABLE sessions DROP COLUMN cache_breakpoint_follow")
    migrated = Store(store.root).session(session["id"])
    assert migrated["cache_breakpoint_auto"] is False
    assert migrated["cache_breakpoint_message"] == session["head"]
    assert migrated["cache_breakpoint_follow"]["interval"] == 2
    assert migrated["cache_breakpoint_follow"]["offset"] == 2


def test_failures_maintenance_versions_and_rollover_do_not_advance(story):
    store, session = story
    for _ in range(3):
        session = commit_reply(store, session)
    session = store.set_cache_breakpoint(session["id"], session["revision"], True, None, 4, 1)
    session = commit_reply(store, session)
    previous = session["cache_breakpoint_follow"]
    for status in ("error", "cancelled", "interrupted"):
        turn_id, _ = store.create_turn(
            session["id"],
            TurnRequest(text="未完成", operation_id=uid(), expected_revision=session["revision"]),
        )
        store.set_turn(turn_id, status=status)
        assert store.session(session["id"])["cache_breakpoint_follow"] == previous
    turn_id, _ = store.create_turn(
        session["id"],
        TurnRequest(operation_id=uid(), expected_revision=session["revision"], scene_refresh="memory"),
    )
    store.set_turn(turn_id, status="running")
    store.commit_turn(
        turn_id,
        {
            "scene": {"message_id": session["head"], "snapshot": session["snapshot"]},
            "before": session["snapshot"],
            "messages": [],
        },
    )
    session = store.session(session["id"])
    assert session["cache_breakpoint_follow"] == previous
    original = session["head"]
    session = commit_reply(store, session, regenerate=True)
    session = store.switch_message_version(session["id"], session["head"], original, session["revision"])
    assert session["cache_breakpoint_follow"] == previous
    if session["agent_mode"] == "full_agent":
        snapshot = copy.deepcopy(session["snapshot"])
        snapshot["full_agent"]["window"]["start"] = len(session["messages"])
        from tavern.storage.store import dump

        with store.connection() as database:
            database.execute("UPDATE messages SET snapshot=? WHERE id=?", (dump(snapshot), session["head"]))
        session = store.session(session["id"])
        assert session["cache_breakpoint_effective_id"] is None
        assert "窗口" in session["cache_breakpoint_reason"]
        assert session["cache_breakpoint_follow"] == previous


def test_fixed_prefix_waits_for_interval_even_when_enough_replies_exist(story):
    store, session = story
    session = store.set_cache_breakpoint(session["id"], session["revision"], True, None, 5, 1)
    session = commit_reply(store, session)
    assert session["cache_breakpoint_effective_id"] is None
    assert session["cache_breakpoint_follow"]["progress"] == 1
    assert "下次跟进" in session["cache_breakpoint_reason"]
