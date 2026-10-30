import json
from pathlib import Path

import pytest

from tavern.conversation.context import compile_context, search
from tavern.core.cache_policy import normalize_usage
from tavern.core.models import SessionCreate, Settings, TurnRequest
from tavern.core.usage import aggregate_cache_usage
from tavern.storage.store import Conflict, Store


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path)


@pytest.fixture
def session(store):
    c = store.import_card(Path("samples/雾港来信.json").read_bytes(), "card.json")
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
    messages = runtime_messages([{"role": "user", "content": "完整输入"}], "foreground")
    current = adapt_messages(messages, cfg)[0]
    meter = cfg._context_meter
    measure = describe(current, cfg)
    sequence, _ = meter.begin(measure, "foreground")
    meter.finish(
        measure,
        "foreground",
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


async def test_context_sample_survives_restart_and_cleanup_without_stale_response(store):
    from io import BytesIO
    from uuid import uuid4

    import httpx
    from PIL import Image

    from tavern.character.editor_attachments import attachment_path
    from tavern.character.editor_images import IMAGE_REFS, image_metadata, materialize_images
    from tavern.core.context_usage import bind_meter, context_usage, describe
    from tavern.core.message_adapter import adapt_messages, provider_messages, runtime_messages
    from tavern.scripting.model_transport import ModelTransport
    from tavern.scripting.request_context import ProviderRequestContext, configure_operation
    from tavern.storage.maintenance import cleanup

    cfg = bind_meter(
        Settings(
            providers=[{"id": "default", "api_key": "test-key"}],
            foreground={"provider_id": "default", "model": "sample-model"},
            model_request_mode="non_stream",
        ).for_role("foreground"),
        store,
        "story",
        "sample-owner",
    )
    wid = uuid4().hex
    data = BytesIO()
    Image.new("RGB", (2, 2)).save(data, format="PNG")
    data = data.getvalue()
    file = {
        "ref": "F1",
        "extension": ".png",
        "message_ref": "M1",
        "size_bytes": len(data),
        **image_metadata(".png", data),
    }
    path = attachment_path(store, wid, file)
    path.parent.mkdir(parents=True)
    path.write_bytes(data)
    messages = runtime_messages([{"role": "user", "content": "正文输入", IMAGE_REFS: ["F1"]}], "card_author")
    current = adapt_messages(messages, cfg)[0]
    assert context_usage(current, cfg)["tokens"] is None
    hydrated = materialize_images(messages, store, wid, [file])
    assert describe(adapt_messages(hydrated, cfg)[0], cfg) == describe(current, cfg)
    reported = iter([4321, 99])

    def route(request):
        sent = json.loads(request.content)["messages"]
        assert sent[0]["content"][0]["text"] == "正文输入"
        assert sent[0]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")
        assert IMAGE_REFS not in sent[0]
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"role": "assistant", "content": "完整回复"}, "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": next(reported), "completion_tokens": 12},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(route)) as client:
        transport = ModelTransport(cfg.model_copy(deep=True), "test-key", http_client=client)
        cfg._context_generation = "compiled-after-provider-creation"
        try:
            await transport.chat_complete(provider_messages(transport, hydrated, cfg))
        finally:
            await transport.close()
    assert context_usage(current, cfg)["tokens"] == 4321
    assert context_usage(current, cfg)["coverage"] == "exact"
    appended = context_usage([*current, {"role": "user", "content": "再看原图", IMAGE_REFS: ["F1"]}], cfg)
    assert appended["tokens"] == 4321 and appended["coverage"] == "appended"

    # Nested generation shares Settings on API-key routes. Its operation must
    # not redirect the next Writer/window query to the script sample.
    async with httpx.AsyncClient(transport=httpx.MockTransport(route)) as client:
        nested = ModelTransport(cfg, "test-key", http_client=client)
        configure_operation(
            nested, ProviderRequestContext(wid, "nested", "window", "device"), lambda *_: None, "script"
        )
        try:
            await nested.chat_complete(provider_messages(nested, hydrated, cfg))
        finally:
            await nested.close()
    assert context_usage(current, cfg, purpose="script")["tokens"] == 99
    assert context_usage(current, cfg)["tokens"] == 4321
    assert context_usage(current, cfg.for_role("foreground"))["tokens"] == 4321
    cleanup(store)
    reopened = Store(store.root)
    bind_meter(cfg, reopened, "story", "sample-owner")
    assert context_usage(current, cfg)["source"] == "provider"
    measure, meter = describe(current, cfg), cfg._context_meter
    older, _ = meter.begin(measure, "foreground")
    newer, seen = meter.begin(measure, "foreground")
    assert seen["pending"] and seen["tokens"] == 4321
    meter.finish(measure, "foreground", older, {"prompt_tokens": 99})
    pending = context_usage(current, cfg)
    assert pending["pending"] and pending["tokens"] == 4321
    meter.finish(measure, "foreground", newer, {"prompt_tokens": 4567})
    meter.finish(measure, "foreground", older, {"prompt_tokens": 99})
    assert context_usage(current, cfg)["tokens"] == 4567
    assert meter.usage(measure, "background")["source"] == "unavailable"
    latest, _ = meter.begin(measure, "foreground")
    meter.finish(measure, "foreground", latest, {})
    assert context_usage(current, cfg)["tokens"] is None
    latest, _ = meter.begin(measure, "foreground")
    meter.finish(measure, "foreground", latest, {"prompt_tokens": 4600})
    latest, _ = meter.begin(measure, "foreground")
    meter.fail(measure, "foreground", latest)
    # A cancelled or failed attempt restores the last completed sample.
    assert context_usage(current, cfg)["tokens"] == 4600
    assert context_usage(current, cfg)["last_attempt"] == "request_failed"
    first, _ = meter.begin(measure, "first-call")
    meter.fail(measure, "first-call", first)
    assert meter.usage(measure, "first-call")["unavailable_reason"] == "request_failed"

    def rejected(request):
        message = "prompt is too long: 9000 tokens > 8192 maximum"
        return httpx.Response(400, json={"error": {"type": "invalid_request_error", "message": message}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(rejected)) as client:
        failing = ModelTransport(cfg, "test-key", http_client=client)
        try:
            with pytest.raises(Exception, match="prompt is too long"):
                await failing.chat_complete(provider_messages(failing, hydrated, cfg))
        finally:
            await failing.close()
    overflow = context_usage(current, cfg)
    assert overflow["tokens"] is None and overflow["overflow"]
    interrupted, _ = meter.begin(measure, "foreground")
    meter.begin(measure, "first-restart")
    reopened.recover()
    meter.finish(measure, "foreground", interrupted, {"prompt_tokens": 999})
    # Restart restores the rejection; the late response cannot replace it.
    restored = context_usage(current, cfg)
    assert restored["overflow"] and restored["tokens"] is None
    assert restored["last_attempt"] == "interrupted"
    assert meter.usage(measure, "first-restart")["unavailable_reason"] == "interrupted"
    bind_meter(cfg, reopened, "story", "new-branch")
    assert context_usage(current, cfg)["source"] == "unavailable"

    # Formats change before a new observation can be attached to old calibration
    # data; preserve visible window settings and back up the original database.
    raw = reopened.settings_document().model_dump()
    raw["version"] = 13
    raw["half_agent"].update(
        chunk_mode="tokens",
        chunk_max_tokens=6000,
        context_pressure_target=0.6,
        history_message_cap=24,
        chunk_size=4,
    )
    with reopened.connection() as db:
        db.execute(
            "INSERT INTO turns(id,session,operation,request,status,created) VALUES ('legacy-pressure','fixture','fixture','{}','interrupted',0)"
        )
        db.execute(
            "INSERT INTO full_agent_progress VALUES ('legacy-pressure',?)",
            (
                json.dumps(
                    {
                        "version": 3,
                        "output_id": "draft",
                        "must_rollover": True,
                        "story": {"content": "未提交正文完整保留"},
                    }
                ),
            ),
        )
        db.execute(
            "INSERT INTO card_author_progress VALUES ('legacy-editor','legacy-run',?)",
            (
                json.dumps(
                    {
                        "context": {"frames": {"pressure": {"forced": True}}},
                        "pending_calls": {"forced": True, "results": [{"content": "已经完成的工具"}]},
                    }
                ),
            ),
        )
        db.execute("UPDATE meta SET value='15' WHERE key='schema_version'")
        db.execute("UPDATE meta SET value=? WHERE key='settings'", (json.dumps(raw),))
    migrated = Store(store.root)
    settings = migrated.settings_document()
    assert settings.version == 15 and settings.half_agent.chunk_mode == "messages"
    assert (settings.half_agent.history_message_cap, settings.half_agent.chunk_size) == (24, 4)
    assert len(list(store.root.glob("before-upgrade-schema15-settings13-*.sqlite3"))) == 1
    with migrated.connection() as db:
        assert db.execute("SELECT count(*) FROM context_token_samples").fetchone()[0] == 0
        progress = json.loads(
            db.execute("SELECT payload FROM full_agent_progress WHERE turn='legacy-pressure'").fetchone()[0]
        )
        assert not progress["must_rollover"] and progress["story"]["content"] == "未提交正文完整保留"
        progress = json.loads(
            db.execute("SELECT payload FROM card_author_progress WHERE workspace='legacy-editor'").fetchone()[
                0
            ]
        )
        assert (
            not progress["context"]["frames"]["pressure"]["forced"]
            and not progress["pending_calls"]["forced"]
        )
        assert progress["pending_calls"]["results"] == [{"content": "已经完成的工具"}]

    from tavern.core.settings import migrate_settings

    raw["half_agent"]["chunk_mode"] = "off"
    assert migrate_settings(raw).half_agent.chunk_mode == "off"


def test_conversation_cache_usage_sums_requests_without_filling_missing_counters(store, session):
    from tavern.storage.maintenance import cleanup

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
    assert store.session_cache_usage(session["id"]) is None
    req = TurnRequest(text="你好", operation_id="usage", expected_revision=0)
    tid, _ = store.create_turn(session["id"], req)
    narrative = {
        "prompt_tokens": 1000,
        "cached_tokens": 300,
        "raw_usage": [usage["raw_usage"] for usage in samples[:2]],
    }
    store.event(tid, "usage", {"purpose": "narrative", "usage": narrative})
    store.event(tid, "usage", {"purpose": "maintenance", "usage": samples[2]})
    assert store.latest_usage(session["id"]) == narrative
    store.set_turn(tid, status="committed")
    next_tid, _ = store.create_turn(session["id"], req.model_copy(update={"operation_id": "next"}))
    store.event(next_tid, "usage", {"purpose": "narrative", "usage": samples[3]})
    store.set_turn(next_tid, status="committed")
    with store.connection() as db:
        db.execute(
            """INSERT INTO context_operations(id,session,operation,kind,status,expected_revision,payload,created)
            VALUES ('compact',?,'compact','compact','completed',0,?,0)""",
            (
                session["id"],
                json.dumps(
                    {
                        "usage": [{"prompt_tokens": 500, "cached_tokens": 200}],
                        "replay": {"private": "expired"},
                    }
                ),
            ),
        )
    total = {"prompt_tokens": 4500, "cached_tokens": 2300}
    assert store.session_cache_usage(session["id"]) == total
    other = store.new_session(SessionCreate(card_id=session["card"]))
    other_tid, _ = store.create_turn(other["id"], req)
    store.event(other_tid, "usage", {"usage": {}})
    store.set_turn(other_tid, status="committed")
    cleanup(store)
    assert store.session_cache_usage(session["id"]) == total
    with store.connection() as db:
        compacted = json.loads(
            db.execute("SELECT payload FROM context_operations WHERE id='compact'").fetchone()[0]
        )
        assert set(compacted) == {"usage"}
    store.event(next_tid, "usage", {"usage": {"prompt_tokens": 100, "cached_tokens": 0}})
    assert store.session_cache_usage(session["id"]) == {"prompt_tokens": 4600, "cached_tokens": 2300}
    store.event(next_tid, "usage", {"usage": {"prompt_tokens": 100}})
    assert store.session_cache_usage(session["id"]) == {"prompt_tokens": 4700, "cached_tokens": None}


def test_context_sends_mid_system_without_probe(store, session):
    card = store.card(session["card"])
    out = compile_context(
        card["document"], session, session["messages"], session["snapshot"], "灯塔", Settings(), "seed"
    )
    assert any(x["id"] == "1" for x in out["lore"])
    assert any(x["source"] == "lore:1" and x["role"] == "system" for x in out["trace"])
    assert not any(x["source"] == "provider-fallback" for x in out["trace"])


def test_user_system_prompt_is_used_by_context(store, session):
    settings = Settings(system_prompt="用户自定义系统提示")
    store.save_settings(settings)
    loaded = store.settings()
    out = compile_context(
        store.card(session["card"])["document"],
        session,
        session["messages"],
        session["snapshot"],
        "继续",
        loaded,
        "custom",
    )
    rules = next(item for item in out["trace"] if item["source"] == "rules")
    assert rules["content"] == "用户自定义系统提示" and rules["role"] == "system"


def test_aggregated_lore_preview_retains_entry_titles_and_ids(store, session):
    document = store.card(session["card"])["document"]
    data = document.get("data", document)
    data["character_book"]["entries"] = [
        {
            "id": "1",
            "keys": [],
            "enabled": True,
            "constant": True,
            "comment": "标题一",
            "content": "内容一",
            "extensions": {"position": 0},
        },
        {
            "id": "2",
            "keys": [],
            "enabled": True,
            "constant": True,
            "comment": "标题二",
            "content": "内容二",
            "extensions": {"position": 0},
        },
    ]
    out = compile_context(
        document, session, session["messages"], session["snapshot"], "继续", Settings(), "titles"
    )
    lore = [item for item in out["trace"] if item.get("lore_region") == "constant"]
    assert [item["lore_ids"] for item in lore] == [["1"], ["2"]]
    assert [item["title"] for item in lore] == ["标题一", "标题二"]
    assert all(set(message) == {"role", "content"} for message in out["messages"])


def test_card_edits_replace_current_content_for_existing_sessions(store, session):
    card = store.card(session["card"])
    doc = card["document"]
    doc["data"]["description"] = "NEW"
    updated = store.edit_card(card["id"], doc)
    assert updated["document"] == doc and "version" not in updated
    existing = store.session(session["id"])
    compiled = compile_context(
        store.card(existing["card"])["document"],
        existing,
        existing["messages"],
        existing["snapshot"],
        "continue",
        Settings(),
        "updated-card",
    )
    assert "NEW" in next(m["content"] for m in compiled["trace"] if m["source"] == "character")


def test_chinese_search():
    found = search([{"content": "蓝色灯塔在秋分亮起"}, {"content": "街角书店"}], "灯塔")
    assert len(found) == 1
