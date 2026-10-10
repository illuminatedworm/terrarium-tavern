import asyncio
import copy
import json
from types import SimpleNamespace

import httpx
import pytest

from tavern.application.api import create_app
from tavern.core.settings import migrate_settings
from tavern.full_agent.compaction import checkpoint_items, replay_groups
from tavern.full_agent.context_operations import ContextOperationRequest
from tavern.storage.store import Conflict
from tests.backend.chatgpt_fixtures import completed, sse
from tests.backend.test_chatgpt_integration import connected_store, story
from tests.backend.test_full_agent import Model, runtime, turn


def compact_response(text="OK", number=1):
    response = completed(text)
    response["output"].insert(
        0, {"id": f"cmp_{number}", "type": "compaction", "encrypted_content": f"opaque-{number}"}
    )
    return response


async def operate(service, session, kind):
    operations = service.full.context_operations
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
    config.shared.card_editor = ModelProfile(provider_id="", model="")
    config = SettingsDocument.model_validate(config.model_dump())
    runtime_settings = config.runtime()
    assert runtime_settings.for_role("foreground").channel_origin == "standard"
    editor = runtime_settings.for_role("card_editor")
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


async def test_compact_round_trip_preserves_notes_replays_old_tools_and_never_executes_them(tmp_path):
    store, _, _ = connected_store(tmp_path)
    _, session = story(store, "full_agent")
    service = runtime(store)
    initial = Model(
        ("write_note", {"key": "plan", "content": "Keep this note", "kind": "plan"}),
        ("new_context", {"handoff": "OLD HANDOFF", "keep_recent": 0}),
        "A committed story.",
    )
    try:
        saved, session = await turn(service, session, initial)
        assert saved["status"] == "committed", saved["error"]
        before_notes = copy.deepcopy(session["snapshot"]["full_agent"]["notes"])
        before_history = copy.deepcopy(session["messages"])
        wire = []

        def route(request):
            body = json.loads(request.content)
            wire.append(body)
            if any(
                t.get("name") == "prepare_handoff" for n in body.get("tools", []) for t in n.get("tools", [])
            ):
                return sse(
                    completed(
                        "", [("prepare_handoff", {"handoff": "NEW HANDOFF: the committed scene continues."})]
                    )
                )
            if body.get("instructions"):
                assert body["tool_choice"] == "none"
                return sse(compact_response(number=len(wire)))
            return sse(compact_response("The next scene.", len(wire)))

        store.chatgpt.http_transport = httpx.MockTransport(route)
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
        assert "context_management" in wire[-1]
        assert session["snapshot"]["full_agent"]["notes"] == before_notes
        _, session = await operate(service, session, "enable_local")
        assert session["context_strategy"] == "new_context"
        assert session["snapshot"]["full_agent"]["handoff"]["text"].startswith("NEW HANDOFF")
        assert session["snapshot"]["full_agent"]["notes"] == before_notes
    finally:
        await service.close()


async def test_cancelled_replay_is_resumable_and_blocks_story_requests(tmp_path):
    store, _, _ = connected_store(tmp_path)
    _, session = story(store, "full_agent")
    service = runtime(store)
    entered, release = asyncio.Event(), asyncio.Event()

    async def route(request):
        entered.set()
        await release.wait()
        return sse(compact_response())

    store.chatgpt.http_transport = httpx.MockTransport(route)
    try:
        operations = service.full.context_operations
        op = operations.start(
            session["id"],
            ContextOperationRequest(
                kind="enable_compact", operation_id="cancel", expected_revision=session["revision"]
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
        assert store.session(session["id"])["context_strategy"] == "new_context"
        release.set()
        operations.resume(op["id"])
        await operations.tasks[op["id"]]
        assert operations.get(op["id"])["status"] == "completed"
    finally:
        await service.close()


async def test_plain_acknowledgement_cannot_be_committed_as_handoff(tmp_path):
    store, _, _ = connected_store(tmp_path)
    _, session = story(store, "full_agent")
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
        operations = service.full.context_operations
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


async def test_long_replay_resumes_at_complete_batch_and_rebuilds_after_model_change():
    from tavern.core.models import Settings
    from tavern.full_agent.compaction import hashes, rebuild, valid
    from tavern.scripting.request_context import response_origin

    settings = Settings(
        model="fixture",
        auth_mode="chatgpt_subscription",
        context_tokens=10000,
        context_strategy="openai_compact",
    )
    body = [
        {"role": "user", "content": f"Exchange {i}: " + "complete reference sentence. " * 500}
        for i in range(5)
    ]
    compiled = {
        "_compact_header": [{"role": "system", "content": "Rules"}],
        "_compact_body": body,
        "_compact_signature": "fixture-header",
        "tools": [],
    }

    class ReplayProvider:
        last_tool_calls = []
        last_native_response = None

        def __init__(self):
            self.requests = []

        async def chat_complete(self, messages, **kwargs):
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
        await rebuild(compiled, settings, provider, save=stop_after_batch)
    assert 0 < saved[-1]["processed"] < len(body)
    resume_provider = ReplayProvider()
    checkpoint, progress = await rebuild(compiled, settings, resume_provider, progress=saved[-1])
    assert progress["processed"] == len(body) and checkpoint["covered"] == hashes(body)
    assert body[0]["content"] not in json.dumps(resume_provider.requests)
    assert valid(checkpoint, body, settings, "fixture-header")
    assert not valid(checkpoint, body, settings.model_copy(update={"model": "other"}), "fixture-header")
    assert not valid(checkpoint, body[:-1], settings, "fixture-header")
    assert checkpoint["origin"] == response_origin(settings)


async def test_subscription_stream_compaction_item_survives_empty_terminal_output(tmp_path):
    from tavern.core.provider import make_provider
    from tavern.full_agent.compaction import opaque_message

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
