"""Rewrite references reach real requests without inheriting a rejected reply's state."""
import copy
import json

import pytest
from kohakuterrarium.testing.llm import ScriptedLLM

from tavern.application.service import RoleplayRuntime
from tavern.core.models import SessionCreate, TurnRequest
from tavern.storage.store import Conflict, Store, dump, uid
from tests.backend.test_full_agent import Model, ScriptHost, runtime
from tests.backend.test_message_versions import generate


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
    session = generate(store, session, "RAW_REPLY_MUST_USE_MODEL_TEXT")
    text = "待重写的完整正文\n" * 1200 + "REFERENCE_END_MUST_SURVIVE"
    snapshot = copy.deepcopy(session["snapshot"])
    if request.param == "full_agent":
        snapshot["full_agent"]["handoff"]["text"] = "HANDOFF_FROM_REPLACED_REPLY"
    with store.connection() as db:
        db.execute("UPDATE messages SET model=?,snapshot=? WHERE id=?", (text, dump(snapshot), session["head"]))
        if request.param == "full_agent":
            db.execute("INSERT INTO full_agent_trace(session,message_id,seq,window_id,payload) VALUES (?,?,?,?,?)", (
                session["id"], session["head"], 1, snapshot["full_agent"]["window"]["id"],
                dump({"role": "system", "content": "TOOLS_FROM_REPLACED_REPLY"})))
    return store, store.session(session["id"]), text


def service_for(store, session):
    if session["agent_mode"] == "full_agent":
        return runtime(store)
    service = RoleplayRuntime(store)
    service.runtime = ScriptHost()
    return service


async def run(service, session, model, **options):
    if session["agent_mode"] == "full_agent":
        service.full.provider_factory = lambda: model
    else:
        service.provider_factory = lambda: model
    request = TurnRequest(text="next", operation_id=uid(), expected_revision=session["revision"], **options)
    result = service.submit(session["id"], request)
    await service.tasks[result["id"]]
    return service.store.turn(result["id"]), service.store.session(session["id"])


def model_for(session, *responses):
    return Model(*responses) if session["agent_mode"] == "full_agent" else ScriptedLLM(list(responses))


def requests(model):
    return [r["messages"] for r in model.requests] if isinstance(model, Model) else [model.last_messages]


@pytest.mark.parametrize("include", [None, False, True])
async def test_reference_is_opt_in_complete_and_only_for_this_turn(story, include):
    store, session, text = story
    service = service_for(store, session)
    user = copy.deepcopy(session["messages"][-2])
    model = model_for(session, *(("read_state", {}), "replacement")
                      if session["agent_mode"] == "full_agent" else ("replacement",))
    try:
        options = {} if include is None else {"regenerate_include_reply": include}
        result, current = await run(service, session, model, mode="regenerate", regenerate_note="缩短正文", **options)
        assert result["status"] == "committed", result["error"]
        for messages in requests(model):
            wire = json.dumps(messages, ensure_ascii=False)
            assert any(text in m.get("content", "") for m in messages) is (include is True)
            assert ("<rewrite_reference>" in wire) is (include is True)
            assert "缩短正文" in wire
            assert "RAW_REPLY_MUST_USE_MODEL_TEXT" not in wire
            assert "HANDOFF_FROM_REPLACED_REPLY" not in wire
            assert "TOOLS_FROM_REPLACED_REPLY" not in wire
            assert session["head"] not in wire
            assert not any(m.get("content") == text and m["role"] == "assistant" for m in messages)
        assert current["messages"][-2] == user
        assert len(current["messages"]) == len(session["messages"])
        assert current["snapshot"]["state"] == user["snapshot"]["state"]
        if session["agent_mode"] == "full_agent":
            assert current["snapshot"]["full_agent"]["handoff"] == user["snapshot"]["full_agent"]["handoff"]
        next_model = model_for(current, "following")
        result, _ = await run(service, current, next_model)
        assert result["status"] == "committed", result["error"]
        assert "REFERENCE_END_MUST_SURVIVE" not in json.dumps(requests(next_model))
        assert "rewrite_reference" not in json.dumps(requests(next_model))
    finally:
        await service.close()


async def test_send_does_not_treat_latest_reply_as_a_rewrite_reference(story):
    store, session, _ = story
    service = service_for(store, session)
    model = model_for(session, "following")
    try:
        result, _ = await run(service, session, model, regenerate_include_reply=True)
        assert result["status"] == "committed", result["error"]
        assert "rewrite_reference" not in json.dumps(requests(model))
    finally:
        await service.close()


async def test_repeated_rewrite_references_current_candidate_and_restores_same_baseline(story):
    store, session, text = story
    service = service_for(store, session)
    baseline = copy.deepcopy(session["messages"][-2]["snapshot"])
    try:
        for candidate in ("first replacement", "second replacement"):
            model = model_for(session, candidate)
            result, session = await run(service, session, model, mode="regenerate",
                                        message_id=session["head"], regenerate_include_reply=True)
            assert result["status"] == "committed", result["error"]
            reference = next(m for m in requests(model)[0] if "<rewrite_reference>" in m.get("content", ""))
            assert text in reference["content"]
            assert session["messages"][-2]["snapshot"] == baseline
            if candidate == "second replacement":
                assert "REFERENCE_END_MUST_SURVIVE" not in reference["content"]
            text = candidate
    finally:
        await service.close()


@pytest.mark.parametrize("story", ["full_agent"], indirect=True)
async def test_full_rollover_retains_one_reference_without_carrying_it_to_next_turn(story):
    store, session, text = story
    service = service_for(store, session)
    model = Model(("new_context", {"content": "本轮新交接", "keep_recent": 0}),
                  ("read_state", {}), "replacement")
    try:
        result, current = await run(service, session, model, mode="regenerate", regenerate_include_reply=True)
        assert result["status"] == "committed", result["error"]
        for messages in requests(model):
            assert sum(text in m.get("content", "") for m in messages) == 1
            assert "HANDOFF_FROM_REPLACED_REPLY" not in json.dumps(messages)
        assert current["snapshot"]["full_agent"]["handoff"]["text"] == "本轮新交接"
        next_model = Model("following")
        result, _ = await run(service, current, next_model)
        assert result["status"] == "committed", result["error"]
        assert "rewrite_reference" not in json.dumps(requests(next_model))
    finally:
        await service.close()


@pytest.mark.parametrize("story", ["full_agent"], indirect=True)
async def test_full_retry_keeps_reference_and_changing_choice_requires_restart(story):
    store, session, text = story
    service = service_for(store, session)
    try:
        failed, current = await run(service, session,
            Model(("read_state", {}), RuntimeError("disconnected")),
            mode="regenerate", regenerate_include_reply=True)
        assert failed["status"] == "error"
        assert store.full_agent_progress(failed["id"])
        with pytest.raises(Conflict, match="输入或授权已改变"):
            store.create_turn(session["id"], TurnRequest(**{
                **failed["request"], "operation_id": uid(), "resume_turn_id": failed["id"],
                "regenerate_include_reply": False}))
        model = Model("replacement after retry")
        result, current = await run(service, current, model, mode="regenerate",
                                    regenerate_include_reply=True, resume_turn_id=failed["id"])
        assert result["status"] == "committed", result["error"]
        assert sum(text in m.get("content", "") for m in requests(model)[0]) == 1
        next_model = Model("following")
        result, _ = await run(service, current, next_model)
        assert result["status"] == "committed", result["error"]
        assert "REFERENCE_END_MUST_SURVIVE" not in json.dumps(requests(next_model))
    finally:
        await service.close()
