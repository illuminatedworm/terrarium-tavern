"""Rewrite references reach real requests without inheriting a rejected reply's state."""

import copy
import json

import pytest

from tavern.core.models import SessionCreate, TurnRequest
from tavern.storage.store import Conflict, Store, dump, uid
from tests.backend.test_message_versions import generate
from tests.backend.test_story import Model, current_text, handoff, runtime, written


@pytest.fixture
def story(tmp_path):
    store = Store(tmp_path)
    card = store.import_card(b'{"name":"fixture","first_mes":"opening"}')
    settings = store.settings()
    settings.story_config.auto_retrieval = False
    settings.retrieval.channels = ["keyword"]
    store.save_settings(settings)
    session = store.new_session(SessionCreate(card_id=card["id"]))
    session = generate(store, session, "RAW_REPLY_MUST_USE_MODEL_TEXT")
    text = "待重写的完整正文\n" * 1200 + "REFERENCE_END_MUST_SURVIVE"
    snapshot = copy.deepcopy(session["snapshot"])
    snapshot["story"]["handoff"] = written(session["messages"], "HANDOFF_FROM_REPLACED_REPLY")
    with store.connection() as db:
        db.execute(
            "UPDATE messages SET model=?,snapshot=? WHERE id=?", (text, dump(snapshot), session["head"])
        )
        db.execute(
            "INSERT INTO story_trace(session,message_id,seq,window_id,payload) VALUES (?,?,?,?,?)",
            (
                session["id"],
                session["head"],
                1,
                snapshot["story"]["window"]["id"],
                dump({"role": "system", "content": "TOOLS_FROM_REPLACED_REPLY"}),
            ),
        )
    return store, store.session(session["id"]), text


async def run(service, session, model, **options):
    service.story.provider_factory = lambda: model
    request = TurnRequest(text="next", operation_id=uid(), expected_revision=session["revision"], **options)
    result = service.story.submit(session["id"], request)
    await service.story.tasks[result["id"]]
    return service.store.turn(result["id"]), service.store.session(session["id"])


def requests(model):
    return [r["messages"] for r in model.requests]


@pytest.mark.parametrize("include", [None, False, True])
async def test_reference_is_opt_in_complete_and_only_for_this_turn(story, include):
    store, session, text = story
    service = runtime(store)
    user = copy.deepcopy(session["messages"][-2])
    model = Model(("read_state", {}), "replacement")
    try:
        options = {} if include is None else {"regenerate_include_reply": include}
        result, current = await run(
            service, session, model, mode="regenerate", regenerate_note="缩短正文", **options
        )
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
        assert current["snapshot"]["story"]["handoff"] == user["snapshot"]["story"]["handoff"]
        next_model = Model("following")
        result, _ = await run(service, current, next_model)
        assert result["status"] == "committed", result["error"]
        following = json.dumps(requests(next_model), ensure_ascii=False)
        assert ("REFERENCE_END_MUST_SURVIVE" in following) is (include is True)
        assert ("<rewrite_reference>" in following) is (include is True)
        assert ("此前的待重写回复参考已失效" in following) is (include is True)
        assert "此前的重生成要求已撤销。" in following
    finally:
        await service.close()


async def test_send_does_not_treat_latest_reply_as_a_rewrite_reference(story):
    store, session, _ = story
    service = runtime(store)
    model = Model("following")
    try:
        result, _ = await run(service, session, model, regenerate_include_reply=True)
        assert result["status"] == "committed", result["error"]
        assert "rewrite_reference" not in json.dumps(requests(model))
    finally:
        await service.close()


async def test_repeated_rewrite_references_current_candidate_and_restores_same_baseline(story):
    store, session, text = story
    service = runtime(store)
    baseline = copy.deepcopy(session["messages"][-2]["snapshot"])
    try:
        for candidate in ("first replacement", "second replacement"):
            model = Model(candidate)
            result, session = await run(
                service,
                session,
                model,
                mode="regenerate",
                message_id=session["head"],
                regenerate_include_reply=True,
            )
            assert result["status"] == "committed", result["error"]
            reference = next(m for m in requests(model)[0] if "<rewrite_reference>" in m.get("content", ""))
            assert text in reference["content"]
            assert session["messages"][-2]["snapshot"] == baseline
            if candidate == "second replacement":
                assert "REFERENCE_END_MUST_SURVIVE" not in reference["content"]
            text = candidate
    finally:
        await service.close()


async def test_story_rollover_retains_reference_and_appends_expiry_next_turn(story):
    store, session, text = story
    service = runtime(store)
    model = Model(
        ("new_context", {"handoff": handoff("本轮新交接"), "keep_recent": 0}),
        ("read_state", {}),
        "replacement",
    )
    try:
        result, current = await run(service, session, model, mode="regenerate", regenerate_include_reply=True)
        assert result["status"] == "committed", result["error"]
        for messages in requests(model):
            assert sum(text in m.get("content", "") for m in messages) == 1
            assert "HANDOFF_FROM_REPLACED_REPLY" not in json.dumps(messages)
        assert current_text(current["snapshot"]["story"]) == "本轮新交接"
        next_model = Model("following")
        result, _ = await run(service, current, next_model)
        assert result["status"] == "committed", result["error"]
        following = requests(next_model)[0]
        assert sum(text in m.get("content", "") for m in following) == 1
        assert "此前的待重写回复参考已失效" in json.dumps(following, ensure_ascii=False)
    finally:
        await service.close()


async def test_story_retry_keeps_reference_and_changing_choice_requires_restart(story):
    store, session, text = story
    service = runtime(store)
    try:
        failed, current = await run(
            service,
            session,
            Model(("read_state", {}), RuntimeError("disconnected")),
            mode="regenerate",
            regenerate_include_reply=True,
        )
        assert failed["status"] == "error"
        assert store.story_progress(failed["id"])
        with pytest.raises(Conflict, match="输入或授权已改变"):
            store.create_turn(
                session["id"],
                TurnRequest(
                    **{
                        **failed["request"],
                        "operation_id": uid(),
                        "resume_turn_id": failed["id"],
                        "regenerate_include_reply": False,
                    }
                ),
            )
        model = Model("replacement after retry")
        result, current = await run(
            service,
            current,
            model,
            mode="regenerate",
            regenerate_include_reply=True,
            resume_turn_id=failed["id"],
        )
        assert result["status"] == "committed", result["error"]
        assert sum(text in m.get("content", "") for m in requests(model)[0]) == 1
        next_model = Model("following")
        result, _ = await run(service, current, next_model)
        assert result["status"] == "committed", result["error"]
        following = requests(next_model)[0]
        assert sum(text in m.get("content", "") for m in following) == 1
        assert "此前的待重写回复参考已失效" in json.dumps(following, ensure_ascii=False)
    finally:
        await service.close()
