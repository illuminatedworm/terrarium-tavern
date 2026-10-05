"""Explicit edit choices reach real model requests and commit with branch state."""
import copy
import json

import pytest
from httpx import ASGITransport, AsyncClient
from kohakuterrarium.testing.llm import ScriptedLLM

from tavern.application.api import create_app
from tavern.application.service import RoleplayRuntime
from tavern.conversation.message_editing import NOTIFICATIONS, edit_notification, review_snapshot
from tavern.core.models import SessionCreate, TurnRequest
from tavern.storage.store import Conflict, Store, uid
from tests.backend.test_full_agent import Model, runtime, turn
from tests.backend.test_message_versions import generate
from tests.support.tracker_model import TrackerModel


@pytest.fixture(params=["half_agent", "full_agent"])
def story(tmp_path, request):
    store = Store(tmp_path)
    card = store.import_card(b'{"name":"fixture","first_mes":"opening"}', "card.json")
    settings = store.settings()
    settings.full_agent_config.auto_retrieval = False
    settings.retrieval.channels = ["keyword"]
    settings.memory_interval_turns = 50
    store.save_settings(settings)
    session = store.new_session(SessionCreate(card_id=card["id"], agent_mode=request.param))
    return store, card, generate(store, session, "original reply")


def edit(store, session, text="修订后的完整回复", notify=True, mid=None):
    return store.edit_message(session["id"], mid or session["head"], text,
                              session["revision"], notify_ai=notify)


def test_latest_choice_restart_fork_and_atomic_conflict(story):
    store, _, session = story
    edited = edit(store, session)
    assert edited["snapshot"][NOTIFICATIONS] == [session["head"]]
    assert Store(store.root).session(session["id"])["snapshot"][NOTIFICATIONS] == [session["head"]]
    with pytest.raises(Conflict):
        edit(store, session, "must not save", False)
    assert store.session(session["id"])["messages"][-1]["raw"] == "修订后的完整回复"
    assert store.session(session["id"])["snapshot"][NOTIFICATIONS] == [session["head"]]
    fork = store.fork_session(edited["id"], edited["head"], edited["revision"])
    assert fork["snapshot"][NOTIFICATIONS] == [fork["head"]]
    quiet = edit(store, edited, "静默修订", False)
    assert NOTIFICATIONS not in quiet["snapshot"]
    assert store.session(fork["id"])["snapshot"][NOTIFICATIONS] == [fork["head"]]
    # Independent edited messages retain their own latest choice.
    user = quiet["messages"][-2]["id"]
    chosen = edit(store, quiet, "修订输入", True, user)
    both = edit(store, chosen)
    assert set(both["snapshot"][NOTIFICATIONS]) == {user, quiet["head"]}
    latest = edit(store, both, "最后修订", False)
    assert latest["snapshot"][NOTIFICATIONS] == [user]
    assert latest["snapshot"]["state"] == session["snapshot"]["state"]


async def test_api_defaults_and_preview_do_not_consume(story):
    store, _, session = story
    app = create_app(store.root)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
            url = f"/api/sessions/{session['id']}/messages/{session['head']}"
            quiet = await client.put(url, json={"text": "silent", "expected_revision": session["revision"]})
            assert quiet.status_code == 200
            assert NOTIFICATIONS not in quiet.json()["snapshot"]
            chosen = await client.put(url, json={"text": "explicit", "notify_ai": True,
                                                "expected_revision": quiet.json()["revision"]})
            assert chosen.status_code == 200
            assert chosen.json()["snapshot"][NOTIFICATIONS] == [session["head"]]
            preview = await client.post(f"/api/sessions/{session['id']}/messages-preview", json={"text": "next"})
            assert preview.status_code == 200, preview.text
            assert "用户已修订" in json.dumps(preview.json(), ensure_ascii=False)
            assert app.state.store.session(session["id"])["snapshot"][NOTIFICATIONS] == [session["head"]]
    finally:
        await app.state.service.close()


def test_notice_uses_valid_references_or_complete_text(story):
    store, _, session = story
    text = "完整修订" * 4000 + "尾部不可丢弃"
    edited = edit(store, session, text)
    notice = edit_notification(edited["snapshot"], edited["messages"])
    assert text in notice and "H3" not in notice
    referenced = edit_notification(edited["snapshot"], edited["messages"], {edited["head"]: text})
    assert "H3" in referenced and text not in referenced
    assert edited["head"] not in notice and edited["head"] not in referenced
    assert review_snapshot(edited["snapshot"]) == review_snapshot({
        **edited["snapshot"], NOTIFICATIONS: []})


async def run_story(store, session, model, *, regenerate=False, tracker=None):
    if session["agent_mode"] == "full_agent":
        service = runtime(store)
        try:
            return await turn(service, session, model, mode="regenerate" if regenerate else "send",
                              message_id=session["head"] if regenerate else None)
        finally:
            await service.close()
    service = RoleplayRuntime(store)
    service.provider_factory = lambda: model
    if tracker:
        service.tracker_provider_factory = lambda: tracker
    try:
        result = service.submit(session["id"], TurnRequest(text="next", operation_id=uid(),
            expected_revision=session["revision"], mode="regenerate" if regenerate else "send",
            message_id=session["head"] if regenerate else None, update_variables=tracker is not None))
        await service.tasks[result["id"]]
        return store.turn(result["id"]), store.session(session["id"])
    finally:
        await service.close()


def model_for(session, *responses):
    return Model(*responses) if session["agent_mode"] == "full_agent" else ScriptedLLM(list(responses))


def sent_text(model):
    messages = model.requests[0]["messages"] if isinstance(model, Model) else model.last_messages
    return json.dumps(messages, ensure_ascii=False)


@pytest.mark.parametrize("regenerate", [False, True])
@pytest.mark.parametrize("notify", [False, True])
async def test_actual_next_request_and_consumption(story, regenerate, notify):
    store, _, session = story
    edited = edit(store, session, notify=notify)
    model = model_for(edited, "new reply")
    result, current = await run_story(store, edited, model, regenerate=regenerate)
    assert result["status"] == "committed", result["error"]
    assert ("用户已修订" in sent_text(model)) is notify
    if notify:
        assert ("修订后的完整回复" in sent_text(model))
        assert edited["head"] not in sent_text(model)
    assert NOTIFICATIONS not in current["snapshot"]
    next_model = model_for(current, "next reply")
    result, _ = await run_story(store, current, next_model)
    assert result["status"] == "committed", result["error"]
    assert "用户已修订" not in sent_text(next_model)


async def test_failure_cancellation_and_rejected_review_keep_queue(story):
    store, _, session = story
    edited = edit(store, session)
    model = model_for(edited, RuntimeError("fixture model failure"))
    result, current = await run_story(store, edited, model)
    assert result["status"] == "error"
    assert current["snapshot"][NOTIFICATIONS] == [edited["head"]]
    for status in ("cancelled", "awaiting_review"):
        tid, _ = store.create_turn(current["id"], TurnRequest(text="next", operation_id=uid(),
                                                           expected_revision=current["revision"]))
        store.set_turn(tid, status=status)
        if status == "awaiting_review":
            service = RoleplayRuntime(store)
            try:
                service.review(tid, False)
            finally:
                await service.close()
        assert store.session(current["id"])["snapshot"][NOTIFICATIONS] == [edited["head"]]


async def test_half_tracker_gets_notice_without_starting_extra_tasks(story):
    store, _, session = story
    if session["agent_mode"] != "half_agent":
        return
    edited = edit(store, session)
    tracker = TrackerModel(("patch_state", {"operations": [], "reason": "unchanged"}), "完成")
    writer = model_for(edited, "new reply")
    result, current = await run_story(store, edited, writer, tracker=tracker)
    assert result["status"] == "committed", result["error"]
    assert "用户已修订" in sent_text(writer)
    assert "用户已修订" in json.dumps(tracker.requests[0]["messages"], ensure_ascii=False)
    assert "修订后的完整回复" in json.dumps(tracker.requests[0]["messages"], ensure_ascii=False)
    assert NOTIFICATIONS not in current["snapshot"]


async def test_full_tool_continuations_keep_one_notice_and_silent_edits_keep_staleness(story):
    store, _, session = story
    if session["agent_mode"] != "full_agent":
        return
    # A useful handoff must remain marked stale even when no instruction is sent.
    with store.connection() as db:
        snapshot = copy.deepcopy(session["snapshot"])
        snapshot["full_agent"]["handoff"]["text"] = "原先的剧情交接"
        db.execute("UPDATE messages SET snapshot=? WHERE id=?", (json.dumps(snapshot), session["head"]))
    quiet = edit(store, session, notify=False)
    assert quiet["snapshot"]["full_agent"]["handoff"]["stale"]
    silent_model = Model("quiet next")
    result, current = await run_story(store, quiet, silent_model)
    assert result["status"] == "committed", result["error"]
    assert "原始剧情已编辑" not in sent_text(silent_model)
    assert "用户已修订" not in sent_text(silent_model)
    chosen = edit(store, current)
    model = Model(("read_history", {"ref": "H1"}), "continued next")
    result, current = await run_story(store, chosen, model)
    assert result["status"] == "committed", result["error"]
    for request in model.requests:
        assert json.dumps(request["messages"], ensure_ascii=False).count("用户已修订") == 1
    assert NOTIFICATIONS not in current["snapshot"]


def test_notification_consumption_waits_for_accepted_commit(story):
    store, _, session = story
    edited = edit(store, session)
    tid, _ = store.create_turn(edited["id"], TurnRequest(text="next", operation_id=uid(),
                                                     expected_revision=edited["revision"]))
    user_id, reply_id = uid(), uid()
    snapshot = copy.deepcopy(edited["snapshot"])
    pending = {"before": snapshot, "message_edit_notified": [edited["head"]], "messages": [
        {"id": user_id, "parent": edited["head"], "role": "user", "raw": "next", "model": "next",
         "display": "next", "snapshot": snapshot},
        {"id": reply_id, "parent": user_id, "role": "assistant", "raw": "reply", "model": "reply",
         "display": "reply", "snapshot": snapshot},
    ]}
    store.set_turn(tid, status="awaiting_review", pending=json.dumps(pending))
    assert store.session(edited["id"])["snapshot"][NOTIFICATIONS] == [edited["head"]]
    store.commit_turn(tid, pending)
    store.commit_turn(tid, pending)
    current = store.session(edited["id"])
    assert NOTIFICATIONS not in current["snapshot"]
    assert len(current["messages"]) == len(edited["messages"]) + 2
