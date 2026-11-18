from pathlib import Path
from types import SimpleNamespace

import pytest

from tavern.core.cache_policy import normalize_usage
from tavern.core.models import SessionCreate, Settings, TurnRequest
from tavern.core.usage import aggregate_cache_usage
from tavern.storage.store import Conflict, Store
from tavern.story.context import compile_window


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path)


@pytest.fixture
def session(store):
    c = store.import_card(Path("samples/雾港来信.json").read_bytes())
    return store.new_session(SessionCreate(card_id=c["id"]))


def test_revision_and_idempotency(store, session):
    req = TurnRequest(text="你好", operation_id="one", expected_revision=0)
    tid, created = store.create_turn(session["id"], req)
    assert created
    assert store.create_turn(session["id"], req) == (tid, False)
    with pytest.raises(Conflict):
        store.create_turn(session["id"], req.model_copy(update={"text": "changed"}))
    with pytest.raises(Conflict):
        store.update_session(session["id"], 0, head=session["head"])
    store.recover()
    assert store.turn(tid)["status"] == "interrupted"


def test_request_observations_keep_missing_fields_and_provider_scopes_distinct():
    from tavern.core.context_usage import (
        OBSERVATION_VERSION,
        context_overflow,
        describe,
        exceeds,
        pressure_tokens,
        resolve,
    )
    from tavern.core.usage import observed_usage

    cfg = Settings(api_mode="gemini", model="gemini-4-argon")
    cfg._context_generation = "window-one"
    messages = [{"role": "user", "content": "继续"}]
    observation = observed_usage(
        {
            "promptTokenCount": 1000,
            "candidatesTokenCount": 30,
            "thoughtsTokenCount": 20,
            "totalTokenCount": 1050,
            "cachedContentTokenCount": 0,
            "promptTokensDetails": [{"modality": "IMAGE", "tokenCount": 80}],
        },
        "gemini",
    )
    baseline = {
        "version": OBSERVATION_VERSION,
        "last_attempt": "completed",
        "request": describe(messages, cfg),
        "observation": observation,
        "pending": False,
        "previous": None,
        "unavailable_reason": None,
    }
    assert resolve(describe(messages, cfg), baseline)["coverage"] == "exact"
    appended = messages + [{"role": "assistant", "content": "完整正文尾部" * 1000}]
    result = resolve(describe(appended, cfg), baseline)
    assert result["coverage"] == "appended" and result["tokens"] == 1000
    assert pressure_tokens(result) == 1000
    assert result["output_tokens"] == 30 and result["thinking_tokens"] == 20
    assert result["reported_total_tokens"] == 1050 and result["output_includes_thinking"] is False
    assert result["input_modalities"] == [{"modality": "IMAGE", "tokens": 80}]
    rewritten = resolve(describe(appended[::-1], cfg), baseline)
    assert rewritten["tokens"] == 1000 and rewritten["coverage"] == "changed"
    assert pressure_tokens(rewritten) is None
    rejected = {
        **baseline,
        "observation": None,
        "unavailable_reason": "context_overflow",
        "last_attempt": "context_overflow",
    }
    over = resolve(describe(appended, cfg), rejected)
    assert over["tokens"] is None and over["overflow"] and exceeds(over, 10**9)
    smaller_output = cfg.model_copy(update={"reply_tokens": 128})
    assert not exceeds(resolve(describe(appended, smaller_output), rejected), 0)
    assert pressure_tokens(resolve(describe(appended, smaller_output), baseline)) == 1000
    assert not exceeds(resolve(describe(appended[::-1], cfg), rejected), 0)
    failed = {**rejected, "unavailable_reason": "request_failed", "last_attempt": "request_failed"}
    assert not exceeds(resolve(describe(appended, cfg), failed), 0)
    for text in (
        "invalid_request_error：prompt is too long: 215000 tokens > 200000 maximum",
        "The input token count (1200000) exceeds the maximum number of tokens allowed (1048576).",
    ):
        assert context_overflow(ValueError(text))
    assert resolve(describe(messages, cfg, [{"type": "web_search"}]), baseline)["tokens"] is None
    cfg._context_generation = "window-two"
    assert resolve(describe(messages, cfg), baseline)["tokens"] is None
    cfg._context_generation = "window-one"
    cfg.model = "another-model"
    assert resolve(describe(messages, cfg), baseline)["tokens"] is None
    missing = observed_usage(normalize_usage({}, "gemini"), "gemini")
    assert missing["tokens"] is missing["output_tokens"] is missing["reported_total_tokens"] is None
    zero = observed_usage({"promptTokenCount": 0, "candidatesTokenCount": 0}, "gemini")
    assert zero["tokens"] == zero["output_tokens"] == 0 and zero["reported_total_tokens"] is None
    assert observed_usage({"raw_usage": [{"prompt_tokens": 9000}]}, "chat_completions")["tokens"] is None
    assert observed_usage({"input_tokens": 100}, "anthropic")["tokens"] is None
    raw = {
        "input_tokens": 9000,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "output_tokens": 600,
        "output_tokens_details": {"thinking_tokens": 100},
        "iterations": [
            {
                "type": "message",
                "input_tokens": 100,
                "cache_read_input_tokens": 800,
                "cache_creation_input_tokens": 100,
            },
            {"type": "compaction", "input_tokens": 8000},
            {
                "type": "message",
                "input_tokens": 200,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
            },
            {"type": "advisor", "input_tokens": 9000},
        ],
    }
    measured = observed_usage(
        raw,
        "anthropic",
        {
            "context_management": {
                "applied_edits": [
                    {
                        "type": "clear_thinking_20251015",
                        "cleared_input_tokens": 50,
                        "cleared_thinking_turns": 1,
                    },
                ]
            }
        },
    )
    assert measured["tokens"] == 200 and measured["input_scope"] == "last_iteration"
    assert measured["output_tokens"] == 600 and measured["thinking_tokens"] == 100
    assert (
        measured["reported_total_tokens"] is None
        and measured["context_edits"][0]["cleared_input_tokens"] == 50
    )
    raw["iterations"] = [{"type": "compaction", "input_tokens": 8000}]
    assert observed_usage(raw, "anthropic")["tokens"] is None
    response = observed_usage(
        {"input_tokens": 200, "output_tokens": 50, "output_tokens_details": {"reasoning_tokens": 20}},
        "responses",
        {"reasoning": {"context": "current_turn"}},
    )
    assert response["reasoning_context"] == "current_turn" and response["output_includes_thinking"]
    assert response["output_tokens"] == 50 and response["thinking_tokens"] == 20


@pytest.mark.parametrize("phase,api", [("begin", "gemini"), ("finish", "chat_completions")])
async def test_meter_write_lock_and_repeated_cancellation_settle_before_return(
    store, monkeypatch, phase, api
):
    import asyncio
    import threading

    import httpx

    from tavern.core.context_usage import bind_meter, context_usage, describe
    from tavern.core.message_adapter import adapt_messages, provider_messages, runtime_messages
    from tavern.scripting.model_transport import ModelTransport

    cfg = bind_meter(
        Settings(model="fixture", api_mode=api, api_key="test-key", model_request_mode="non_stream"),
        store,
        "story",
        "locked-meter",
    )
    messages = runtime_messages([{"role": "user", "content": "完整输入"}], "story")
    current = adapt_messages(messages, cfg)[0]
    meter = cfg._context_meter
    measure = describe(current, cfg)
    sequence, _ = meter.begin(measure, "story")
    meter.finish(
        measure,
        "story",
        sequence,
        {"promptTokenCount": 777} if api == "gemini" else {"prompt_tokens": 777},
    )
    entered = asyncio.Event()
    loop = asyncio.get_running_loop()
    event_thread = threading.get_ident()
    original = getattr(meter, phase)

    def observed_write(*args, **kwargs):
        assert threading.get_ident() != event_thread
        loop.call_soon_threadsafe(entered.set)
        return original(*args, **kwargs)

    monkeypatch.setattr(meter, phase, observed_write)
    sent = []
    # The event-loop thread owns this competing write lock. A synchronous
    # meter call would prevent it from receiving the signal or releasing it.
    with store.connection() as lock:
        if phase == "begin":
            lock.execute("BEGIN IMMEDIATE")

        def response(request):
            sent.append(request)
            lock.execute("BEGIN IMMEDIATE")
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {"message": {"role": "assistant", "content": "完整回复"}, "finish_reason": "stop"}
                    ],
                    "usage": {"prompt_tokens": 123, "completion_tokens": 4},
                },
            )

        provider = ModelTransport(
            cfg, "test-key", http_client=httpx.AsyncClient(transport=httpx.MockTransport(response))
        )
        task = asyncio.create_task(provider.chat_complete(provider_messages(provider, messages, cfg)))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done(), "cancel must wait for the worker transaction to settle"
            lock.commit()
            with pytest.raises(asyncio.CancelledError):
                await task
            usage = context_usage(current, cfg)
            assert not usage["pending"]
            assert usage["tokens"] == (777 if phase == "begin" else 123)
            assert usage["last_attempt"] == ("request_cancelled" if phase == "begin" else "completed")
            assert len(sent) == (0 if phase == "begin" else 1)
            assert len(provider.usage_history) == (0 if phase == "begin" else 1)
            assert provider._context_sample is None
        finally:
            lock.rollback()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await provider.close()


def test_conversation_cache_usage_sums_requests_without_filling_missing_counters():
    samples = [
        normalize_usage(
            {"prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": 75}}, "chat_completions"
        ),
        normalize_usage({"input_tokens": 900, "input_tokens_details": {"cached_tokens": 225}}, "responses"),
        normalize_usage(
            {"input_tokens": 100, "cache_read_input_tokens": 800, "cache_creation_input_tokens": 100},
            "anthropic",
        ),
        normalize_usage({"promptTokenCount": 2000, "cachedContentTokenCount": 1000}, "gemini"),
    ]
    # Unequal request sizes must be weighted by tokens, not averaged as rates.
    assert aggregate_cache_usage(samples[:2]) == {"prompt_tokens": 1000, "cached_tokens": 300}
    assert aggregate_cache_usage([{"prompt_tokens": 0, "cached_tokens": 0, "raw_usage": {}}]) == {
        "prompt_tokens": None,
        "cached_tokens": None,
    }
    assert aggregate_cache_usage([{"cached_tokens": 40}])["prompt_tokens"] is None
    assert (
        aggregate_cache_usage([{"input_tokens": 100, "cache_read_input_tokens": 800}])["prompt_tokens"]
        is None
    )
    assert aggregate_cache_usage(samples[2:]) == {"prompt_tokens": 3000, "cached_tokens": 1800}


def test_story_cache_restores_previous_segment_on_rejection_and_expires_across_restart(
    store, session, monkeypatch
):
    from tavern.storage.maintenance import cleanup
    from tavern.storage.store import new_snapshot
    from tavern.storage.story_cache import story_cache_window

    now = 10_000
    monkeypatch.setattr("tavern.storage.story_cache.time", SimpleNamespace(time=lambda: now))
    session = store.update_session(session["id"], session["revision"], head=None)
    sid, snapshot = session["id"], session["snapshot"]
    settings = Settings(model="story-fixture")
    old_window = story_cache_window(snapshot, settings.context_strategy)
    draft_snapshot = new_snapshot()
    new_window = story_cache_window(draft_snapshot, settings.context_strategy)

    request = TurnRequest(text="继续", operation_id="initial", expected_revision=session["revision"])
    legacy, _ = store.create_turn(sid, request)
    store.event(legacy, "usage", {"usage": {"prompt_tokens": 40, "cached_tokens": 40}})
    store.set_turn(legacy, status="cancelled")
    assert store.story_cache.read(sid, settings, snapshot) == (None, None)

    tid, _ = store.create_turn(sid, request.model_copy(update={"operation_id": "review"}))
    store.set_turn(tid, status="running")
    store.story_cache.begin(sid, tid, settings, snapshot)
    prior = {"prompt_tokens": 100, "cached_tokens": 70}
    store.event(tid, "usage", {"usage": prior}, cache=(sid, old_window))
    now += 2000  # Model and tool work within a round cannot expire the segment.
    provisional = {"prompt_tokens": 50, "cached_tokens": 10}
    store.event(tid, "usage", {"usage": provisional}, cache=(sid, new_window))
    store.set_turn(tid, status="awaiting_review", cache_snapshot=draft_snapshot)
    assert store.story_cache.read(sid, settings, snapshot)[0] == provisional
    store.reject_review(tid)
    snapshot = store.session(sid)["snapshot"]
    assert store.story_cache.read(sid, settings, snapshot)[0] == prior
    cleanup(store)
    assert store.latest_usage(sid) == provisional

    continued, _ = store.create_turn(sid, request.model_copy(update={"operation_id": "old-window"}))
    store.story_cache.begin(sid, continued, settings, snapshot)
    store.event(
        continued, "usage", {"usage": {"prompt_tokens": 25, "cached_tokens": 0}}, cache=(sid, old_window)
    )
    assert store.story_cache.read(sid, settings, snapshot) == (prior, None)
    store.set_turn(continued, status="cancelled")
    total = {"prompt_tokens": 125, "cached_tokens": 70}

    store = Store(store.root)
    store.recover()
    snapshot = store.session(sid)["snapshot"]
    now += 1799
    assert store.story_cache.read(sid, settings, snapshot) == (total, now + 1)
    assert store.story_cache.read(sid, Settings(model="other"), snapshot) == (None, None)
    now += 1
    assert store.story_cache.read(sid, settings, snapshot) == (None, None)
    last, _ = store.create_turn(sid, request.model_copy(update={"operation_id": "after-idle"}))
    store.story_cache.begin(sid, last, settings, snapshot)
    zero = {"prompt_tokens": 70, "cached_tokens": 0}
    store.event(last, "usage", {"usage": zero}, cache=(sid, old_window))
    assert store.story_cache.read(sid, settings, snapshot) == (None, None)
    store.event(last, "usage", {"usage": {"prompt_tokens": 30}}, cache=(sid, old_window))
    store.set_turn(last, status="error")
    assert store.story_cache.read(sid, settings, snapshot)[0] == {"prompt_tokens": 100, "cached_tokens": None}


def test_user_system_prompt_is_used_by_context(store, session):
    settings = Settings(story_system_prompt="用户自定义系统提示")
    store.save_settings(settings)
    loaded = store.settings()
    out = compile_window(
        store.card(session["card"])["document"],
        session,
        session["messages"],
        session["snapshot"],
        "继续",
        loaded,
        [],
    )
    rules = next(item for item in out["trace"] if item["source"] == "rules")
    assert rules["content"] == "用户自定义系统提示" and rules["role"] == "system"


def test_card_edits_replace_current_content_for_existing_sessions(store, session):
    card = store.card(session["card"])
    doc = card["document"]
    doc["data"]["description"] = "NEW"
    updated = store.edit_card(card["id"], doc)
    assert updated["document"] == doc and "version" not in updated
    existing = store.session(session["id"])
    compiled = compile_window(
        store.card(existing["card"])["document"],
        existing,
        existing["messages"],
        existing["snapshot"],
        "continue",
        Settings(),
        [],
    )
    assert "NEW" in next(m["content"] for m in compiled["trace"] if m["source"] == "description")
