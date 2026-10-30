import asyncio
import copy
import json
from types import SimpleNamespace

import httpx
import pytest

from tavern.application.api import create_app
from tavern.application.context_operations import ContextOperationRequest
from tavern.conversation.compaction import checkpoint_items, replay_groups
from tavern.core.settings import migrate_settings
from tavern.storage.store import Conflict
from tests.backend.chatgpt_fixtures import completed, sse
from tests.backend.test_chatgpt_integration import connected_store, story
from tests.backend.test_full_agent import Model, current_text, handoff, prepared, runtime, turn


def compact_response(text="OK", number=1):
    response = completed(text)
    response["output"].insert(
        0, {"id": f"cmp_{number}", "type": "compaction", "encrypted_content": f"opaque-{number}"}
    )
    return response


def reported_pressure(monkeypatch, tokens=1000000):
    """Supply prior pressure until the fixture starts its next real request.

    These lifecycle tests isolate cancellation/review from request matching;
    real identity, generation and ordering are exercised in test_domain.
    """
    from tavern.core.context_usage import ContextMeter

    original_usage, original_begin = ContextMeter.usage, ContextMeter.begin
    started = set()

    def usage(meter, current, lane):
        value = original_usage(meter, current, lane)
        if lane in ("foreground", "full_agent") and meter.owner not in started:
            return {**value, "tokens": tokens, "source": "provider", "coverage": "exact", "pending": False}
        return value

    def begin(meter, current, lane):
        started.add(meter.owner)
        return original_begin(meter, current, lane)

    monkeypatch.setattr(ContextMeter, "usage", usage)
    monkeypatch.setattr(ContextMeter, "begin", begin)
    return started.clear


def long_story(store):
    from tavern.core.models import SessionCreate

    config = store.settings_document()
    config.full_agent.official_model.context_tokens = 10000
    config.full_agent.official_model.reply_tokens = 1024
    store.save_settings(config)
    card = store.import_card(
        json.dumps({"name": "Long fixture", "first_mes": "完整开场原文。" * 6000}).encode(), "long.json"
    )
    return store.new_session(SessionCreate(card_id=card["id"], agent_mode="full_agent"))


async def operate(service, session, kind):
    operations = service.context_operations
    operation = operations.start(
        session["id"],
        ContextOperationRequest(
            kind=kind, operation_id=f"{kind}-{session['revision']}", expected_revision=session["revision"]
        ),
    )
    task = operations.tasks[operation["id"]]
    await task
    result = operations.get(operation["id"])
    assert result["status"] == "completed", result
    return result, service.store.session(session["id"])


def test_old_official_settings_move_to_separate_profiles_without_copying_connection():
    from tavern.core.settings import SettingsDocument

    raw = SettingsDocument().model_dump()
    for scope in ("full_agent", "half_agent"):
        raw[scope].pop("default_channel")
    raw["shared"]["providers"][0]["auth_mode"] = "chatgpt_subscription"
    raw["full_agent"]["model"]["model"] = "official-example"
    migrated = migrate_settings(raw)
    assert migrated.full_agent.official_model.model == "official-example"
    assert migrated.full_agent.model.model == "" and migrated.full_agent.model.provider_id == ""
    assert migrated.runtime("full_agent", "chatgpt").model == "official-example"
    assert migrated.runtime("full_agent", "standard").model == ""


def test_unconfigured_independent_editor_does_not_break_story_settings():
    from tavern.core.provider import require_model_connection
    from tavern.core.settings import SettingsDocument
    from tavern.search.retrieval_config import ModelProfile

    config = SettingsDocument()
    config.shared.card_author = ModelProfile(provider_id="", model="")
    config = SettingsDocument.model_validate(config.model_dump())
    runtime_settings = config.runtime()
    assert runtime_settings.for_role("foreground").channel_origin == "standard"
    editor = runtime_settings.for_role("card_author")
    assert editor.model == "" and editor.channel_origin is None
    with pytest.raises(ValueError, match="模型名称"):
        require_model_connection(editor)


async def test_ordinary_custom_cache_parameters_cannot_enable_cloud_compaction():
    from tavern.core.models import Settings
    from tavern.scripting.model_transport import ModelTransport

    settings = Settings(model="fixture", api_mode="responses", channel_origin="standard", use_proxy=False)
    provider = ModelTransport(
        settings,
        "test",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(500))),
    )
    try:
        with pytest.raises(ValueError, match="普通渠道"):
            provider._body(
                [{"role": "user", "content": "test"}],
                [],
                False,
                None,
                {"context_management": [{"type": "compaction", "compact_threshold": 1024}]},
                {},
            )
    finally:
        await provider.close()


async def test_channel_boundary_is_enforced_in_settings_creation_and_origin_assignment(tmp_path):
    app = create_app(tmp_path)
    store = app.state.store
    card = store.import_card(json.dumps({"name": "Fixture", "first_mes": "Opening"}).encode(), "fixture.json")
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            config = (await client.get("/api/settings")).json()
            providers = config["shared"]["providers"]
            providers[0]["auth_mode"] = "chatgpt_subscription"
            rejected = await client.patch(
                "/api/settings/shared",
                json={
                    "expected_revision": config["revisions"]["shared"],
                    "changes": {"providers": providers},
                },
            )
            assert rejected.status_code == 400
            created = (
                await client.post(
                    "/api/sessions",
                    json={"card_id": card["id"], "agent_mode": "full_agent", "channel_origin": "standard"},
                )
            ).json()
            assert created["channel_origin"] == "standard"
            rejected = await client.put(
                f"/api/sessions/{created['id']}/channel-origin",
                json={"channel_origin": "chatgpt", "expected_revision": created["revision"]},
            )
            assert rejected.status_code == 409
            op = await client.post(
                f"/api/sessions/{created['id']}/context-operations",
                json={
                    "kind": "enable_compact",
                    "operation_id": "forbidden",
                    "expected_revision": created["revision"],
                },
            )
            assert op.status_code == 400
    finally:
        await app.state.service.close()
        await app.state.editor.close()


async def test_compact_round_trip_preserves_notes_replays_old_tools_and_never_executes_them(
    tmp_path, monkeypatch
):
    store, _, _ = connected_store(tmp_path)
    config = store.settings_document()
    config.full_agent.official_model.context_tokens = 10000
    config.full_agent.official_model.reply_tokens = 1024
    store.save_settings(config)
    _, session = story(store, "full_agent")
    service = runtime(store)
    initial = Model(
        ("write_note", {"key": "plan", "content": "Keep this note", "kind": "plan"}),
        ("new_context", {"handoff": handoff("OLD HANDOFF"), "keep_recent": 0}),
        "A committed story. " * 10000,
    )
    try:
        saved, session = await turn(service, session, initial)
        assert saved["status"] == "committed", saved["error"]
        before_notes = copy.deepcopy(session["snapshot"]["full_agent"]["notes"])
        before_history = copy.deepcopy(session["messages"])
        wire = []
        story_requests = 0

        def route(request):
            nonlocal story_requests
            body = json.loads(request.content)
            wire.append(body)
            if any(
                t.get("name") == "prepare_handoff" for n in body.get("tools", []) for t in n.get("tools", [])
            ):
                return sse(
                    completed(
                        "",
                        [
                            (
                                "prepare_handoff",
                                prepared(
                                    json.dumps(body, ensure_ascii=False),
                                    "NEW HANDOFF: the committed scene continues.",
                                ),
                            )
                        ],
                    )
                )
            if body.get("instructions"):
                assert body["tool_choice"] == "none"
                return sse(compact_response(number=len(wire)))
            story_requests += 1
            if story_requests > 1:
                return sse(completed(""))
            response = compact_response("", len(wire))
            response["output"].extend(
                completed("", [("submit_story", {"content": "The next scene."})])["output"]
            )
            return sse(response)

        store.chatgpt.http_transport = httpx.MockTransport(route)
        reset_observation = reported_pressure(monkeypatch)
        result, session = await operate(service, session, "enable_compact")
        assert result["compacted"] and session["context_strategy"] == "openai_compact"
        assert session["snapshot"]["full_agent"]["notes"] == before_notes
        assert [m["raw"] for m in session["messages"]] == [m["raw"] for m in before_history]
        first = wire[0]
        assert first["context_management"] == [{"type": "compaction", "compact_threshold": 1024}]
        assert first["input"][0]["role"] == "developer"
        assert "当前有效 handoff" not in json.dumps(first["input"], ensure_ascii=False)
        assert any(
            i.get("type") == "function_call" and i.get("name") == "new_context" for i in first["input"]
        )
        assert any(i.get("type") == "function_call_output" for i in first["input"])
        assert "new_context" not in {t["name"] for n in first["tools"] for t in n["tools"]}
        service.full.provider_factory = None
        saved, session = await turn(service, session, None)
        assert saved["status"] == "committed", saved["error"]
        assert any(i.get("type") == "compaction" for i in wire[-1]["input"])
        assert "context_management" not in wire[-1]
        assert session["snapshot"]["full_agent"]["notes"] == before_notes
        reset_observation()
        _, session = await operate(service, session, "enable_local")
        assert session["context_strategy"] == "new_context"
        assert current_text(session["snapshot"]["full_agent"]).startswith("NEW HANDOFF")
        assert session["snapshot"]["full_agent"]["notes"] == before_notes
        export = wire[-1]
        assert not any(item.get("type") == "compaction" for item in export["input"])
        assert "A committed story." in json.dumps(export["input"])
    finally:
        await service.close()


async def test_cancelled_replay_is_resumable_and_blocks_story_requests(tmp_path):
    store, _, _ = connected_store(tmp_path)
    session = long_story(store)
    service = runtime(store)
    entered, release = asyncio.Event(), asyncio.Event()

    async def route(request):
        entered.set()
        await release.wait()
        return sse(compact_response())

    store.chatgpt.http_transport = httpx.MockTransport(route)
    try:
        _, session = await operate(service, session, "enable_compact")
        operations = service.context_operations
        op = operations.start(
            session["id"],
            ContextOperationRequest(
                kind="compact", operation_id="cancel", expected_revision=session["revision"]
            ),
        )
        await asyncio.wait_for(entered.wait(), 3)
        from tavern.core.models import TurnRequest

        with pytest.raises(Conflict):
            store.create_turn(
                session["id"],
                TurnRequest(operation_id="blocked", expected_revision=session["revision"], text="Continue"),
            )
        await operations.cancel(op["id"])
        assert store.session(session["id"])["context_strategy"] == "openai_compact"
        release.set()
        operations.resume(op["id"])
        await operations.tasks[op["id"]]
        assert operations.get(op["id"])["status"] == "completed"
    finally:
        await service.close()


async def test_plain_acknowledgement_cannot_be_committed_as_handoff(tmp_path, monkeypatch):
    store, _, _ = connected_store(tmp_path)
    session = long_story(store)
    service = runtime(store)

    def route(request):
        body = json.loads(request.content)
        exporting = any(
            t.get("name") == "prepare_handoff" for n in body.get("tools", []) for t in n.get("tools", [])
        )
        return sse(completed("OK") if exporting else compact_response())

    store.chatgpt.http_transport = httpx.MockTransport(route)
    try:
        _, session = await operate(service, session, "enable_compact")
        before = copy.deepcopy(session["snapshot"])
        reported_pressure(monkeypatch)
        operations = service.context_operations
        op = operations.start(
            session["id"],
            ContextOperationRequest(
                kind="enable_local", operation_id="bad-handoff", expected_revision=session["revision"]
            ),
        )
        await operations.tasks[op["id"]]
        assert operations.get(op["id"])["status"] == "error"
        assert operations.get(op["id"])[
            "usage"
        ]  # Accepted inference is accounted even when semantic validation fails.
        current = store.session(session["id"])
        assert current["context_strategy"] == "openai_compact" and current["snapshot"] == before
    finally:
        await service.close()


async def test_local_handoff_repairs_total_limit_once_without_applying_failed_edits():
    from tavern.core.models import Settings
    from tavern.full_agent.compaction import make_handoff
    from tavern.full_agent.handoff import HandoffError, HandoffWrite, apply_write, check_limits, render_text

    settings = Settings(full_agent_config={"chapters_total_tokens": 4096})
    history = [{"id": f"m{i}", "role": ("assistant", "user")[i % 2], "raw": f"原文{i}"} for i in range(20)]
    ledger = {"chapters": [], "current": [], "revision": 0}
    point = {"text": "a " * 410}
    for index in range(9):
        ledger = apply_write(
            ledger,
            HandoffWrite(
                chapters=[
                    {"op": "add", "through": f"H{(index + 1) * 2}", "title": "经过", "points": [point]}
                ],
                current=[{"text": "当前场景"}],
            ),
            history,
        )
    check_limits(ledger, history, settings.full_agent_config, settings)
    before = copy.deepcopy(ledger)
    add = {"op": "add", "title": "下一章", "points": [point]}
    bad = {"chapters": [add], "current": [{"text": "当前场景"}]}
    corrected = {
        "chapters": [
            {"op": "merge", "chapters": ["C1", "C2"], "title": "旧事", "points": [{"text": "约定仍然有效"}]},
            add,
        ],
        "current": [{"text": "当前场景"}],
    }
    compiled = {"messages": [{"role": "system", "content": render_text(ledger, history)}]}
    model = Model(("prepare_handoff", bad), ("prepare_handoff", corrected))
    result, usage = await make_handoff(compiled, settings, model, ledger, history, 20)
    assert len(model.requests) == len(usage) == 2
    assert "现有章节总量" in model.requests[0]["messages"][-1]["content"]
    assert "merge" in model.requests[0]["messages"][-1]["content"]
    assert "章节总量" in model.requests[1]["messages"][-1]["content"]
    assert "提案未应用" in model.requests[1]["messages"][-1]["content"]
    assert result["chapters"][-1]["end"] == history[-1]["id"] and ledger == before
    check_limits(result, history, settings.full_agent_config, settings)
    stubborn = Model(*[("prepare_handoff", bad)] * 3)
    with pytest.raises(HandoffError, match="章节总量"):
        await make_handoff(compiled, settings, stubborn, ledger, history, 20)
    assert len(stubborn.requests) == 2 and ledger == before

    # A malformed proposal must identify the fix without echoing its arbitrary
    # field name or replaying the rejected write into the correction request.
    private = "48c2515daf9742308a8b50777d39dd75"
    malformed = {**corrected, private: "PRIVATE_INPUT"}
    model = Model(("prepare_handoff", malformed), ("prepare_handoff", corrected))
    result, usage = await make_handoff(compiled, settings, model, ledger, history, 20)
    feedback = model.requests[1]["messages"][-1]["content"]
    assert "不接受参数 @D1" in feedback and "请删除" in feedback
    assert private not in feedback and "PRIVATE_INPUT" not in feedback
    assert "expected_parameters" not in feedback
    assert len(usage) == 2 and ledger == before


def test_compaction_boundary_does_not_hide_pending_calls_and_groups_remain_whole():
    call = {"type": "function_call", "call_id": "c1", "name": "read_state", "arguments": "{}"}
    first = {"type": "compaction", "encrypted_content": "first"}
    last = {"type": "compaction", "encrypted_content": "last"}
    assert checkpoint_items([first, call, last]) == [first, call]
    assert checkpoint_items([call, last]) is None
    assert checkpoint_items([first, last]) == [last]
    messages = [
        {
            "role": "assistant",
            "tool_calls": [{"id": "c1", "function": {"name": "read_state", "arguments": "{}"}}],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "Complete source"},
        {"role": "user", "content": "Next"},
    ]
    assert replay_groups(messages) == [messages[:2], messages[2:]]
    with pytest.raises(ValueError):
        replay_groups(messages[:1])


def test_compaction_source_identity_includes_tool_failure_status():
    from tavern.conversation.compaction import canonical, hashes

    message = {"role": "tool", "tool_call_id": "c1", "content": "Complete source", "_tool_error": True}
    assert canonical(message) == message
    assert hashes([message]) != hashes([{k: v for k, v in message.items() if k != "_tool_error"}])


async def test_long_replay_resumes_at_complete_batch_and_rebuilds_after_model_change():
    from tavern.conversation.compaction import hashes, opaque_message, prepare_turn, valid
    from tavern.core.models import Settings
    from tavern.scripting.request_context import response_origin

    settings = Settings(
        model="fixture",
        auth_mode="chatgpt_subscription",
        context_tokens=10000,
        reply_tokens=1024,
        chunk_mode="off",
        context_strategy="openai_compact",
    )
    body = [
        message
        for i in range(5)
        for message in (
            {"role": "user", "content": f"Exchange {i}: " + "complete reference sentence. " * 900},
            {"role": "assistant", "content": f"Complete reply {i}."},
        )
    ]
    compiled = {
        "_compact_header": [{"role": "system", "content": "Rules"}],
        "_compact_body": body,
        "_compact_signature": "fixture-header",
        "_compact_stale": False,
        "tools": [],
        "context_usage": {"tokens": 20000, "coverage": "exact", "pending": False, "overflow": False},
    }
    pending = {"role": "user", "content": "Current unfinished exchange must stay verbatim"}
    compiled.update(
        _compact_body=[*body, pending],
        _replay_complete=len(body),
        _compact_covered=2,
        compaction_opaque=True,
        messages=[
            *compiled["_compact_header"],
            opaque_message([{"type": "compaction", "encrypted_content": "old-checkpoint"}], settings),
            *body[2:],
            pending,
        ],
    )

    class ReplayProvider:
        last_tool_calls = []
        last_native_response = None

        def __init__(self):
            self.requests = []
            self.attempts = []

        async def chat_complete(self, messages, **kwargs):
            self.attempts.append(copy.deepcopy(messages))
            assert pending not in messages
            if sum(m["role"] == "user" for m in messages) > 2:
                raise ValueError("maximum context length exceeded")
            self.requests.append(copy.deepcopy(messages))
            self.last_native_response = {
                "items": [{"type": "compaction", "encrypted_content": f"opaque-{len(self.requests)}"}]
            }
            return SimpleNamespace(content="OK", usage={"input_tokens": 2000})

    saved = []

    async def stop_after_batch(value):
        saved.append(value)
        raise asyncio.CancelledError()

    provider = ReplayProvider()
    with pytest.raises(asyncio.CancelledError):
        await prepare_turn(compiled, settings, provider, save=stop_after_batch)
    assert "old-checkpoint" in json.dumps(provider.attempts[0])
    assert body[0] in provider.attempts[1]
    assert 0 < saved[-1]["processed"] < len(body)
    resume_provider = ReplayProvider()
    checkpoint = await prepare_turn(compiled, settings, resume_provider, progress=saved[-1])
    assert checkpoint["covered"] == hashes(body)
    assert "old-checkpoint" not in json.dumps(resume_provider.attempts)
    assert body[0]["content"] not in json.dumps(resume_provider.requests)
    assert valid(checkpoint, body, settings, "fixture-header")
    assert not valid(checkpoint, body, settings.model_copy(update={"model": "other"}), "fixture-header")
    assert not valid(checkpoint, body[:-1], settings, "fixture-header")
    assert checkpoint["origin"] == response_origin(settings)


async def test_subscription_stream_compaction_item_survives_empty_terminal_output(tmp_path):
    from tavern.conversation.compaction import opaque_message
    from tavern.core.provider import make_provider

    store, _, _ = connected_store(tmp_path)
    settings = store.settings("full_agent", "chatgpt")
    settings.context_strategy = "openai_compact"
    compaction = {"type": "compaction", "id": "cmp_stream", "encrypted_content": "opaque-stream"}
    captured = []

    def route(request):
        captured.append(json.loads(request.content))
        payload = completed("OK")
        items = [compaction, *payload["output"]]
        return sse(
            events=[
                *[
                    {"type": "response.output_item.done", "output_index": i, "item": item}
                    for i, item in enumerate(items)
                ],
                {"type": "response.completed", "response": {**payload, "output": []}},
            ]
        )

    store.chatgpt.http_transport = httpx.MockTransport(route)
    provider = await make_provider(settings)
    try:
        await provider.chat_complete([{"role": "user", "content": "Continue"}])
        assert provider.last_native_response["items"][0] == compaction
        assert "context_management" not in captured[-1]
        provider.settings.compact_auto = False
        await provider.chat_complete(
            [
                {"role": "system", "content": "Rules"},
                opaque_message([compaction], settings),
                {"role": "user", "content": "Continue again"},
            ]
        )
        assert "context_management" not in captured[-1]
        assert captured[-1]["input"][1] == compaction
    finally:
        await provider.close()


@pytest.mark.parametrize("played", [False, True])
async def test_short_mode_switches_use_complete_sources_without_maintenance_requests(tmp_path, played):
    from tavern.full_agent.context import compile_window, load_trace

    store, _, _ = connected_store(tmp_path)
    _, session = story(store, "full_agent")
    service = runtime(store)

    def unexpected_provider(_):
        raise AssertionError("Short source must not even initialize a maintenance provider")

    service.context_operations.provider_factory = unexpected_provider
    try:
        if played:
            saved, session = await turn(
                service,
                session,
                Model(
                    ("write_note", {"key": "promise", "content": "Original promise", "kind": "plan"}),
                    ("new_context", {"handoff": handoff("OLD SUMMARY MUST NOT BE REUSED"), "keep_recent": 0}),
                    "A short committed reply.",
                ),
            )
            assert saved["status"] == "committed"
        originals = [m["raw"] for m in session["messages"]]
        notes = copy.deepcopy(session["snapshot"]["full_agent"]["notes"])
        result, session = await operate(service, session, "enable_compact")
        assert result["usage"] == [] and not result["converted"]
        revision = session["revision"]
        _, session = await operate(service, session, "enable_compact")
        assert session["revision"] == revision
        result, session = await operate(service, session, "enable_local")
        assert result["usage"] == [] and not result["converted"]
        assert session["snapshot"]["full_agent"]["window"]["start"] == 0
        assert current_text(session["snapshot"]["full_agent"]) == ""
        compiled = compile_window(
            store.session_card(session["id"])["document"],
            session,
            session["messages"],
            session["snapshot"],
            "",
            store.session_settings(session),
            [],
            trace=load_trace(store, session["id"], session["messages"]),
        )
        if played:
            assert any(
                call["function"]["name"] == "write_note"
                for m in compiled["messages"]
                for call in m.get("tool_calls", [])
            )
        assert [m["raw"] for m in session["messages"]] == originals
        assert session["snapshot"]["full_agent"]["notes"] == notes
        _, session = await operate(service, session, "enable_compact")
        model = Model("Only the requested story response.")
        saved, session = await turn(service, session, model)
        assert saved["status"] == "committed", saved.get("error")
        assert len(model.requests) == 1
        assert "_maintenance_instructions" not in model.requests[0]
    finally:
        await service.close()


@pytest.mark.parametrize("strategy", ["openai_compact", "new_context"])
async def test_replay_preserves_oversized_whole_exchanges_and_resumes(strategy):
    from tavern.conversation.compaction import rebuild
    from tavern.core.models import Settings
    from tavern.core.tokens import request_tokens
    from tavern.full_agent.compaction import rebuild_local

    settings = Settings(
        model="fixture", context_tokens=8192, reply_tokens=1024, chunk_mode="off", context_strategy=strategy
    )
    body, starts = [], []
    for index in range(3):
        starts.append({"offset": len(body), "history_start": index * 2})
        body.extend(
            [
                # Adapter-configured lore role must not create another exchange.
                {"role": "user", "content": f"Anchored lore {index}"},
                {"role": "user", "content": f"User {index}: " + "whole original sentence. " * 2400},
                {
                    "role": "assistant",
                    "content": "Tool process",
                    "tool_calls": [
                        {"id": f"c{index}", "function": {"name": "read_state", "arguments": "{}"}}
                    ],
                },
                {"role": "tool", "tool_call_id": f"c{index}", "content": f"Entire result {index}"},
                {"role": "assistant", "content": f"Final reply {index} with its complete ending."},
            ]
        )
    compiled = {
        "_compact_header": [],
        "_compact_body": body,
        "_compact_signature": "fixture",
        "_compact_stale": False,
        "_replay_header": [],
        "_replay_body": body,
        "_replay_starts": starts,
        "tools": [],
        "context_usage": {"tokens": 20000, "coverage": "exact", "pending": False, "overflow": False},
    }

    class Provider(Model):
        async def chat_complete(self, messages, **kwargs):
            originals = sum(m.get("content", "").startswith("User ") for m in messages)
            if originals > (1 if strategy == "openai_compact" else 2):
                raise ValueError("maximum context length exceeded")
            self.requests.append(copy.deepcopy(messages))
            assert request_tokens(messages, settings) > settings.context_tokens
            if strategy == "new_context":
                assert not any(item.get("_native_response") for item in messages)
            self.last_native_response = {"items": [{"type": "compaction", "encrypted_content": "fresh"}]}
            self.last_tool_calls = (
                []
                if strategy == "openai_compact"
                else [
                    SimpleNamespace(
                        name="prepare_handoff",
                        arguments=json.dumps(
                            prepared(
                                json.dumps(messages, ensure_ascii=False), "Fresh summary from this replay."
                            )
                        ),
                    )
                ]
            )
            return SimpleNamespace(content="OK", usage={"input_tokens": 5000})

    history = [{"id": f"m{i}", "role": ("user", "assistant")[i % 2], "raw": f"m{i}"} for i in range(6)]

    def run(*args, **kwargs):
        if strategy == "openai_compact":
            return rebuild(*args, **kwargs)
        return rebuild_local(*args[:3], history, **kwargs)

    progress = []

    async def stop(value):
        progress.append(value)
        raise asyncio.CancelledError()

    first = Provider()
    with pytest.raises(asyncio.CancelledError):
        await run(compiled, settings, first, save=stop)
    assert progress[0]["processed"] == 5
    resumed = Provider()
    await run(compiled, settings, resumed, progress=progress[0])
    assert len(first.requests) == 1 and len(resumed.requests) == 2
    for index, request in enumerate(first.requests + resumed.requests):
        for original in body[index * 5 : (index + 1) * 5]:
            assert original in request
    if strategy == "new_context":
        assert not any(item.get("_native_response") for request in resumed.requests for item in request)


def test_compact_observation_and_auto_setting_decide_rebuild():
    from tavern.conversation.compaction import project_compact
    from tavern.core.models import Settings

    settings = Settings(
        model="fixture",
        context_tokens=8192,
        reply_tokens=1024,
        chunk_mode="off",
        context_strategy="openai_compact",
    )
    measured = {"tokens": None, "coverage": "unavailable", "pending": False, "overflow": False}
    settings._context_meter = SimpleNamespace(usage=lambda *_: measured)

    def projection(state=None):
        return project_compact(
            {
                "messages": [{"role": "user", "content": "Complete source. " * 3000}],
                "diagnostics": [],
                "trace": [],
            },
            state or {},
            settings,
            [],
            0,
        )

    assert not projection()["compact_requires_rebuild"]
    measured.update(tokens=8192, coverage="exact")
    assert not projection()["compact_requires_rebuild"]
    measured["tokens"] = 8193
    assert projection()["compact_requires_rebuild"]
    # A definite size rejection carries no count but still proves the overflow.
    measured.update(tokens=None, coverage="unavailable", overflow=True)
    assert projection()["compact_requires_rebuild"]
    settings.compact_auto = False
    assert not projection()["compact_requires_rebuild"]
    assert projection({"compaction": {"version": 0}})["compact_requires_rebuild"]
    # Only a stale server compaction, not a stale plain baseline, re-compacts.
    assert not projection({"compaction": {"version": 0}})["_compact_stale"]
    assert projection({"compaction": {"version": 0, "items": [{"type": "compaction"}]}})["_compact_stale"]


@pytest.mark.parametrize("automatic", [True, False])
async def test_user_turn_compacts_before_story_only_when_enabled_and_never_during_tools(
    tmp_path, automatic, monkeypatch
):
    from tavern.core.models import TurnRequest

    store, _, _ = connected_store(tmp_path)
    config = store.settings_document()
    config.full_agent.official_model.context_tokens = 10000
    config.full_agent.official_model.reply_tokens = 1024
    store.save_settings(config)
    _, session = story(store, "full_agent")
    service = runtime(store)
    wire, stages = [], []
    text = "本轮输入必须完整保留。" * 4000

    def route(request):
        body = json.loads(request.content)
        wire.append(body)
        if body.get("instructions"):
            stages.append("maintenance")
            assert text not in json.dumps(body["input"], ensure_ascii=False)
            return sse(compact_response())
        stages.append("story")
        assert "context_management" not in body
        assert text in json.dumps(body["input"], ensure_ascii=False)
        if stages.count("story") == 1:
            return sse(completed("", [("read_history", {"ref": "H2"})]))
        if stages.count("story") == 3:
            return sse(completed(""))
        return sse(
            completed(
                "", [("submit_story", {"content": "The final reply follows the complete current input."})]
            )
        )

    store.chatgpt.http_transport = httpx.MockTransport(route)
    try:
        _, session = await operate(service, session, "enable_compact")
        assert wire == []
        session = store.update_session(session["id"], session["revision"], compact_auto=int(automatic))
        reported_pressure(monkeypatch)
        started = service.submit(
            session["id"],
            TurnRequest(text=text, operation_id="long-user-input", expected_revision=session["revision"]),
        )
        await service.tasks[started["id"]]
        saved = store.turn(started["id"])
        assert saved["status"] == "committed", saved.get("error")
        assert stages == (["maintenance"] if automatic else []) + ["story", "story", "story"]
        assert store.session(session["id"])["messages"][-2]["raw"] == text
    finally:
        await service.close()


async def test_failed_pre_turn_compaction_does_not_generate_or_commit_story(tmp_path, monkeypatch):
    from tavern.core.models import TurnRequest

    store, _, _ = connected_store(tmp_path)
    config = store.settings_document()
    config.full_agent.official_model.context_tokens = 10000
    config.full_agent.official_model.reply_tokens = 1024
    store.save_settings(config)
    _, session = story(store, "full_agent")
    service = runtime(store)
    requests = []

    def route(request):
        body = json.loads(request.content)
        requests.append(body)
        assert body.get("instructions"), "Failure must stop before the story request"
        return sse(completed("OK without a compaction checkpoint"))

    store.chatgpt.http_transport = httpx.MockTransport(route)
    try:
        _, session = await operate(service, session, "enable_compact")
        before = copy.deepcopy(session["snapshot"])
        reported_pressure(monkeypatch)
        started = service.submit(
            session["id"],
            TurnRequest(
                text="Whole oversized input. " * 15000,
                operation_id="no-compaction",
                expected_revision=session["revision"],
            ),
        )
        await service.tasks[started["id"]]
        saved = store.turn(started["id"])
        assert saved["status"] == "error"
        assert "未生成压缩状态" in saved["error"]
        assert len(requests) == 1
        current = store.session(session["id"])
        assert current["head"] == session["head"] and current["snapshot"] == before
    finally:
        await service.close()


async def test_fixed_source_edit_rebuilds_before_tool_continuation_without_replaying_tools(
    tmp_path, monkeypatch
):
    from tavern.character.cards import normalize
    from tavern.core.models import TurnRequest

    store, _, _ = connected_store(tmp_path)
    _, session = story(store, "full_agent")
    service = runtime(store)
    stages = []
    current_input = "本轮正在修改设定，必须完整保留这个输入。"

    def route(request):
        body = json.loads(request.content)
        if body.get("instructions"):
            stages.append("maintenance")
            assert current_input not in json.dumps(body["input"], ensure_ascii=False)
            assert not any(item.get("type") == "function_call" for item in body["input"])
            return sse(compact_response(number=len(stages)))
        stages.append("story")
        assert "context_management" not in body
        assert current_input in json.dumps(body["input"], ensure_ascii=False)
        if stages.count("story") == 1:
            return sse(
                completed(
                    "",
                    [
                        (
                            "revise_content",
                            {
                                "operations": [
                                    {"op": "set_character", "field": "description", "value": "新的固定设定"}
                                ],
                                "comment": "用户明确要求修改设定",
                            },
                        )
                    ],
                )
            )
        if stages.count("story") == 3:
            return sse(completed(""))
        assert stages == ["maintenance", "story", "maintenance", "story"]
        assert "新的固定设定" in json.dumps(body["input"], ensure_ascii=False)
        assert sum(item.get("type") == "function_call" for item in body["input"]) == 1
        return sse(completed("", [("submit_story", {"content": "修改设定后的完整正文。"})]))

    store.chatgpt.http_transport = httpx.MockTransport(route)
    try:
        _, session = await operate(service, session, "enable_compact")
        reported_pressure(monkeypatch)
        started = service.submit(
            session["id"],
            TurnRequest(
                text=current_input,
                operation_id="edit-fixed-source",
                expected_revision=session["revision"],
                allow_content_edit=True,
            ),
        )
        await service.tasks[started["id"]]
        saved = store.turn(started["id"])
        assert saved["status"] == "committed", saved.get("error")
        assert stages == ["maintenance", "story", "maintenance", "story", "story"]
        current = store.session(session["id"])
        assert current["messages"][-1]["raw"] == "修改设定后的完整正文。"
        assert normalize(current["snapshot"]["content"]["document"])["description"] == "新的固定设定"
    finally:
        await service.close()


async def test_pre_turn_recompaction_keeps_the_pending_exchange_outside_the_checkpoint():
    from tavern.conversation.compaction import hashes, prepare_turn, project_compact
    from tavern.core.models import Settings
    from tavern.scripting.request_context import response_origin

    settings = Settings(model="fixture", auth_mode="chatgpt_subscription", context_strategy="openai_compact")
    history = [
        {"role": "user", "content": "Old user input"},
        {"role": "assistant", "content": "Old final reply"},
    ]
    pending = {"role": "user", "content": "Entire new user input. " * 15000}

    def project(state):
        return project_compact(
            {
                "messages": history + [pending],
                "diagnostics": [],
                "trace": [],
                "_replay_complete": 2,
            },
            state,
            settings,
            [],
            0,
        )

    source = project({})
    state = {
        "compaction": {
            "version": 1,
            "origin": response_origin(settings),
            "signature": source["_compact_signature"],
            "covered": hashes(history),
            "items": [{"type": "compaction", "encrypted_content": "prior"}],
        }
    }

    class Provider(Model):
        async def chat_complete(self, messages, **kwargs):
            self.requests.append(messages)
            assert pending not in messages
            assert messages[0]["_native_response"]["items"][0]["encrypted_content"] == "prior"
            self.last_native_response = {"items": [{"type": "compaction", "encrypted_content": "fresh"}]}
            return SimpleNamespace(content="OK", usage={})

    provider = Provider()
    state["compaction"] = await prepare_turn(project(state), settings, provider)
    assert len(provider.requests) == 1
    assert state["compaction"]["covered"] == hashes(history)
    projected = project(state)
    assert projected["messages"][-1] == pending
    assert projected["messages"][0]["_native_response"]["items"][0]["encrypted_content"] == "fresh"
