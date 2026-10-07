"""Final SDK projections, persistent owner settings, and in-flight adapter changes."""
import asyncio
import copy
import json

import httpx
import pytest
from pydantic import ValidationError

from tavern.application.api import create_app
from tavern.character.card_editor import CardEditorService
from tavern.core.message_adapter import (
    APIS,
    MessageAdapterConfig,
    adapt_messages,
    bind_adapter,
    catalog,
    rule_for,
    runtime_messages,
)
from tavern.core.models import ContextBlock, ContextRecipe, SessionCreate, Settings
from tavern.scripting.model_transport import ModelTransport
from tavern.storage.message_adapters import recipe_types
from tavern.storage.store import Conflict, Store


def config(**rules):
    return {"version": 1, "mappings": {"chat_completions": rules}}


def story(tmp_path, mode="half_agent"):
    store = Store(tmp_path)
    card = store.import_card(b'{"name":"fixture","first_mes":"Opening"}', "fixture.json")
    return store, store.new_session(SessionCreate(card_id=card["id"], agent_mode=mode))


def state_messages():
    return runtime_messages([
        {"role": "system", "message_type": "root_system", "content": "Rules"},
        {"role": "system", "message_type": "state", "content": "V1"},
        {"role": "user", "content": "Continue"},
        {"role": "system", "message_type": "state", "content": "V2", "runtime_phase": "body"},
    ])


@pytest.mark.parametrize("api", APIS)
def test_removed_call_notifications_are_purged_from_settings_and_catalog(api):
    original = {"mappings": {api: {
        "last_call_notice": {"role": "user", "wrap": True},
        "compat:last_call_notice:assistant": {"role": "assistant", "wrap": False},
        "call_budget": {"role": "system", "wrap": True},
        "handoff": {"role": "user", "wrap": False}}}}
    before = copy.deepcopy(original)
    cleaned = MessageAdapterConfig.model_validate(original).model_dump()
    assert cleaned["mappings"][api] == {"handoff": {"role": "user", "wrap": False}}
    assert original == before
    for lane in ("full_agent", "card_editor"):
        assert not {"call_budget", "last_call_notice"} & {item["id"] for item in catalog(lane)}


@pytest.mark.parametrize("kind", ["story", "editor"])
async def test_reopen_cleans_persisted_call_notification_mappings(tmp_path, kind):
    store, session = story(tmp_path, "full_agent")
    editor = CardEditorService(store)
    try:
        owner = session["id"] if kind == "story" else editor.edits.create_conversation()["id"]
        lane = "full_agent" if kind == "story" else "card_editor"
        saved = {lane: config(last_call_notice={"role": "user", "wrap": True})}
        with store.connection() as db:
            db.execute("UPDATE message_adapter_configs SET config=? WHERE owner_kind=? AND owner_id=?",
                       (json.dumps(saved), kind, owner))
        reopened = Store(store.root)
        result = reopened.message_adapter_settings(kind, owner)
        assert result["config"][lane]["mappings"] == {"chat_completions": {}}
        assert result["revision"] == 1
        assert "last_call_notice" not in str(result)
        assert reopened.message_adapter(kind, owner, lane)["config"] == result["config"][lane]
        with reopened.connection() as db:
            persisted = db.execute("SELECT config FROM message_adapter_configs WHERE owner_kind=? AND owner_id=?",
                                   (kind, owner)).fetchone()["config"]
        assert "last_call_notice" not in persisted
        assert Store(store.root).message_adapter_settings(kind, owner)["revision"] == 1
    finally:
        await editor.close()


@pytest.mark.parametrize("api", APIS)
def test_full_agent_default_application_messages_remain_system_for_every_protocol(api):
    settings = Settings(runtime_mode="full_agent", api_mode=api)
    types = [item["id"] for item in catalog("full_agent") if item["id"] not in ("user", "assistant", "tool")]
    rows = runtime_messages([{"role": "system", "message_type": name, "content": name} for name in types]
        + [{"role": "user", "content": "Continue"}, {"role": "assistant", "content": "Reply"},
           {"role": "tool", "content": "Result"},
           {"role": "system", "message_type": "handoff", "content": "Updated memory"}], "full_agent")
    projected, _, _ = adapt_messages(rows, settings)
    assert all(row["role"] == "system" for row in projected if row["message_type"] in types)
    assert [row["role"] for row in projected if row["message_type"] in ("user", "assistant", "tool")] == ["user", "assistant", "tool"]
    assert all(item["defaults"][api]["role"] == "system" for item in catalog("full_agent") if item["id"] in types)


@pytest.mark.parametrize("api", APIS)
def test_note_list_defaults_are_wrapped_and_mapping_is_reversible(api):
    settings = Settings(runtime_mode="full_agent", api_mode=api)
    messages = runtime_messages([
        {"role": "system", "source": "character", "content": "Character"},
        {"role": "system", "source": "note_list", "content": "<tavern_note_list>\n线索/钟楼\n</tavern_note_list>"},
        {"role": "user", "source": "input", "content": "Continue"},
    ], "full_agent")
    original = copy.deepcopy(messages)
    defaults, _, _ = adapt_messages(messages, settings)
    assert defaults[1]["role"] == "system"
    assert defaults[1]["content"] == "<tavern_note_list>\n线索/钟楼\n</tavern_note_list>"
    assert defaults[1]["runtime_phase"] == "header"
    directory_type = next(t for t in catalog("full_agent") if t["id"] == "note_list")
    assert directory_type["configurable"] and not directory_type["appendable"]
    assert directory_type["defaults"][api] == {"role": "system", "wrap": True}
    assert all(t["id"] != "note_list" for lane in ("foreground", "background", "card_editor") for t in catalog(lane))
    settings.message_adapter = MessageAdapterConfig.model_validate({
        "mappings": {api: {"note_list": {"role": "user", "wrap": False}}}})
    projected, _, _ = adapt_messages(messages, settings)
    assert projected[1]["role"] == "user"
    assert projected[1]["content"] == "线索/钟楼"
    assert [m["message_type"] for m in projected] == ["character", "note_list", "user"]
    assert messages == original


@pytest.mark.parametrize("api", APIS)
def test_full_agent_existing_owner_and_new_owner_inherit_system_defaults(tmp_path, api):
    store, existing = story(tmp_path, "full_agent")
    reopened = Store(store.root)
    new = reopened.new_session(SessionCreate(card_id=existing["card"], agent_mode="full_agent"))
    configs = [reopened.settings_document().full_agent.model.message_adapter.model_dump()]
    configs.extend(reopened.message_adapter("story", owner["id"], "full_agent")["config"] for owner in (existing, new))
    for adapter in configs:
        for item in catalog("full_agent"):
            if item["configurable"]:
                assert rule_for(item["id"], adapter, api, "full_agent")["role"] == "system"


@pytest.mark.parametrize("api", ["chat_completions", "responses", "anthropic", "gemini"])
@pytest.mark.parametrize("mode", ["half_agent", "full_agent"])
async def test_same_type_has_one_sdk_role_and_labels_are_reversible(api, mode):
    settings = Settings(model="fixture", api_mode=api, runtime_mode=mode, message_adapter={"mappings": {api: {
        "state": {"role": "user", "wrap": False}}}})
    original = state_messages()
    before = copy.deepcopy(original)
    provider = ModelTransport(settings, "local-fixture", http_client=httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(500))))
    try:
        body = provider._request(original)
        rows = body.get("messages", body.get("input", body.get("contents")))
        text = json.dumps(rows)
        assert "V1" in text and "V2" in text and "tavern_state" not in text
        assert all(row["role"] == "user" for row in rows if "V1" in json.dumps(row) or "V2" in json.dumps(row))
        assert "message_type" not in json.dumps(body) and "runtime_content" not in json.dumps(body)
        assert original == before
        settings.message_adapter.mappings[api]["state"].wrap = True
        assert "<tavern_state>" in json.dumps(provider._request(original))
    finally:
        await provider.close()


def test_managed_header_order_is_stable_across_mapping_changes_and_body_stays_put():
    messages = runtime_messages([
        {"role": "system", "source": "rules", "content": "Rules"},
        {"role": "system", "source": "author_note", "content": "A"},
        {"role": "system", "source": "state", "content": "B"},
        {"role": "system", "source": "description", "content": "Character"},
        {"role": "user", "content": "Continue"},
        {"role": "system", "source": "state", "content": "B2", "runtime_phase": "body"},
    ])
    settings = Settings(runtime_mode="full_agent", message_adapter=config(
        author_note={"role": "user", "wrap": True}, state={"role": "system", "wrap": True}))
    first, reordered, _ = adapt_messages(messages, settings)
    assert [m["message_type"] for m in first[:4]] == ["root_system", "card_description", "state", "author_note"]
    assert first[-1]["runtime_phase"] == "body" and first[-1]["content"].endswith("B2\n</tavern_state>")
    settings.message_adapter = MessageAdapterConfig.model_validate(config(
        author_note={"role": "user", "wrap": False}, state={"role": "user", "wrap": False}))
    second, _, _ = adapt_messages(reordered, settings)
    assert [m["message_type"] for m in second[:4]] == ["root_system", "card_description", "author_note", "state"]


def test_half_agent_warns_without_reordering_and_custom_text_is_not_unwrapped():
    messages = runtime_messages([
        {"role": "system", "source": "rules", "content": "Rules"},
        {"role": "system", "source": "state", "content": "State"},
        {"role": "system", "source": "description", "content": "Character"},
        {"role": "user", "content": "Input"},
        {"role": "system", "source": "custom", "content": "<tavern_custom_context>\nUser text\n</tavern_custom_context>"},
    ])
    projected, _, warnings = adapt_messages(messages, Settings(message_adapter=config(state={"role": "user", "wrap": False})))
    assert [m["message_type"] for m in projected] == [m["message_type"] for m in messages]
    assert len(warnings) == 2
    assert projected[-1]["content"] == messages[-1]["content"]


def test_fixed_types_and_unknown_types_cannot_be_overridden():
    for name in ("root_system", "user", "tool", "lore_constant", "missing"):
        with pytest.raises((ValueError, ValidationError)):
            MessageAdapterConfig.model_validate(config(**{name: {"role": "user", "wrap": False}}))


@pytest.mark.parametrize("api", ["anthropic", "gemini"])
@pytest.mark.parametrize("role", ["system", "developer"])
@pytest.mark.parametrize("accepted", [True, False])
async def test_native_protocol_sends_selected_roles_and_leaves_validation_to_server(api, role, accepted):
    settings = Settings(api_mode=api, model="fixture", base_url="https://fixture.test", use_proxy=False,
        message_adapter={"mappings": {api: {"state": {"role": role, "wrap": False}}}})
    original = state_messages()
    before = copy.deepcopy(original)
    requests = []

    def route(request):
        requests.append(json.loads(request.content))
        if not accepted:
            return httpx.Response(400, json={"error": {"message": "fixture rejects selected role"}})
        payload = ({"model": "fixture", "stop_reason": "end_turn", "content": [{"type": "text", "text": "done"}]}
            if api == "anthropic" else {"modelVersion": "fixture", "candidates": [{"finishReason": "STOP",
                "content": {"role": "model", "parts": [{"text": "done"}]}}]})
        return httpx.Response(200, json=payload)

    provider = ModelTransport(settings, "local-fixture", http_client=httpx.AsyncClient(transport=httpx.MockTransport(route)))
    try:
        if accepted:
            assert (await provider.chat_complete(original)).content == "done"
        else:
            with pytest.raises(httpx.HTTPStatusError) as error:
                await provider.chat_complete(original)
            assert error.value.response.status_code == 400
            assert error.value.response.json()["error"]["message"] == "fixture rejects selected role"
        assert len(requests) == 1
        rows = requests[0]["messages" if api == "anthropic" else "contents"]
        content_key = "content" if api == "anthropic" else "parts"
        assert [row["role"] for row in rows] == (["user", role] if role == "system" else [role, "user", role])
        expected_parts = [{"type": "text", "text": "V2"}] if api == "anthropic" else [{"text": "V2"}]
        assert rows[-1][content_key] == expected_parts
        if role == "developer":
            assert "V1" in json.dumps(rows[0])
        else:
            assert "V1" in json.dumps(requests[0]["system" if api == "anthropic" else "systemInstruction"])
        assert original == before
        assert settings.message_adapter.mappings[api]["state"].role == role
    finally:
        await provider.close()


def test_legacy_recipe_roles_have_distinct_compatible_types():
    recipe = ContextRecipe(blocks=[ContextBlock(id="a", source="state"),
                                  ContextBlock(id="b", source="state", role="user")]).model_dump()
    migrated = recipe_types(copy.deepcopy(recipe))
    assert [b["message_type"] for b in migrated["blocks"]] == ["state", "compat:state:user"]
    rows = runtime_messages([{"role": b["role"], "message_type": b["message_type"], "content": b["id"]}
                             for b in migrated["blocks"]])
    assert [r["role"] for r in adapt_messages(rows, Settings())[0]] == ["system", "user"]


def test_global_defaults_are_snapshotted_and_forks_and_reopen_are_independent(tmp_path):
    store, old = story(tmp_path)
    document = store.settings_document()
    document.half_agent.foreground.message_adapter = MessageAdapterConfig.model_validate(config(state={"role": "user", "wrap": False}))
    store.save_settings(document)
    new = store.new_session(SessionCreate(card_id=old["card"]))
    assert store.message_adapter("story", old["id"], "foreground")["config"]["mappings"] == {}
    assert store.message_adapter("story", new["id"], "foreground")["config"]["mappings"]["chat_completions"]["state"]["role"] == "user"
    saved = store.message_adapter_settings("story", old["id"])
    saved["config"]["foreground"] = config(state={"role": "assistant", "wrap": True})
    store.save_message_adapter("story", old["id"], 0, saved["config"])
    assert store.session(old["id"])["revision"] == 0
    fork = store.fork_session(old["id"], old["head"], 0)
    assert store.message_adapter("story", fork["id"], "foreground")["config"] == saved["config"]["foreground"]
    saved["config"]["foreground"] = config(state={"role": "user", "wrap": False})
    store.save_message_adapter("story", fork["id"], 0, saved["config"])
    assert Store(store.root).message_adapter("story", old["id"], "foreground")["config"]["mappings"]["chat_completions"]["state"]["role"] == "assistant"
    with pytest.raises(Conflict):
        store.save_message_adapter("story", old["id"], 0, saved["config"])


@pytest.mark.parametrize("kind", ["story", "editor"])
async def test_next_sdk_call_uses_live_adapter_while_current_request_keeps_snapshot(tmp_path, kind):
    store, session = story(tmp_path)
    editor = CardEditorService(store)
    try:
        owner = session["id"] if kind == "story" else editor.edits.create_conversation()["id"]
        lane = "foreground" if kind == "story" else "card_editor"
        cfg = bind_adapter(Settings(model="fixture", base_url="https://fixture.test", use_proxy=False), store, kind, owner, lane)
        received, release = asyncio.Event(), asyncio.Event()
        requests = []
        async def route(request):
            requests.append(json.loads(request.content))
            if len(requests) == 1:
                received.set()
                await release.wait()
            return httpx.Response(200, json={"id": "fixture", "model": "fixture", "choices": [{
                "message": {"role": "assistant", "content": "done"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})
        provider = ModelTransport(cfg, "local-fixture", http_client=httpx.AsyncClient(transport=httpx.MockTransport(route)))
        try:
            task = asyncio.create_task(provider.chat_complete(state_messages()))
            await asyncio.wait_for(received.wait(), 5)
            current = store.message_adapter_settings(kind, owner)
            current["config"][lane] = config(state={"role": "user", "wrap": False})
            store.save_message_adapter(kind, owner, 0, current["config"])
            release.set()
            await task
            await provider.chat_complete(state_messages())
            assert requests[0]["messages"][-1]["role"] == "system"
            assert requests[1]["messages"][-1] == {"role": "user", "content": "V2"}
        finally:
            release.set()
            await provider.close()
    finally:
        await editor.close()


async def test_owner_api_allows_running_turn_and_preserves_story_revision(tmp_path):
    app = create_app(tmp_path)
    store = app.state.store
    card = store.import_card(b'{"name":"fixture","first_mes":"Opening"}', "fixture.json")
    session = store.new_session(SessionCreate(card_id=card["id"]))
    with store.connection() as db:
        db.execute("INSERT INTO turns(id,session,operation,request,status,created) VALUES ('active',?,'active','{}','running',0)", (session["id"],))
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://testserver") as client:
            path = f"/api/sessions/{session['id']}/message-adapter"
            old = (await client.get(path)).json()
            old["config"]["foreground"] = config(state={"role": "user", "wrap": False})
            saved = await client.put(path, json={"expected_adapter_revision": 0, "config": old["config"]})
            assert saved.status_code == 200 and saved.json()["revision"] == 1
            assert store.session(session["id"])["revision"] == 0
            assert (await client.put(path, json={"expected_adapter_revision": 0, "config": old["config"]})).status_code == 409
    finally:
        await app.state.editor.close()
        await app.state.service.close()


async def test_full_agent_tool_continuation_refreshes_adapter_and_commits_without_revision_conflict(tmp_path):
    from tavern.core.models import TurnRequest
    from tests.backend.test_full_agent import runtime
    store, session = story(tmp_path, "full_agent")
    document = store.settings_document()
    document.shared.providers[0].api_key = "local-fixture"
    document.shared.providers[0].base_url = "https://fixture.test"
    document.shared.network.api_proxy = False
    document.full_agent.model.model = "fixture"
    document.full_agent.tools.auto_retrieval = False
    store.save_settings(document)
    service = runtime(store)
    received, release = asyncio.Event(), asyncio.Event()
    requests = []
    async def route(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            received.set()
            await release.wait()
            message = {"role": "assistant", "content": None, "tool_calls": [{"id": "read", "type": "function",
                "function": {"name": "read_state", "arguments": "{}"}}]}
        else:
            message = {"role": "assistant", "content": "Narrative"}
        return httpx.Response(200, json={"id": "fixture", "model": "fixture", "choices": [{
            "message": message, "finish_reason": "tool_calls" if len(requests) == 1 else "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})
    def factory():
        cfg = bind_adapter(store.settings("full_agent").for_role("full_agent"), store, "story", session["id"])
        return ModelTransport(cfg, "local-fixture", http_client=httpx.AsyncClient(transport=httpx.MockTransport(route)))
    service.provider_factory = factory
    try:
        turn = service.submit(session["id"], TurnRequest(text="Continue", operation_id="hot-adapter", expected_revision=0))
        await asyncio.wait_for(received.wait(), 5)
        current = store.message_adapter_settings("story", session["id"])
        current["config"]["full_agent"] = config(context_status={"role": "user", "wrap": True})
        store.save_message_adapter("story", session["id"], 0, current["config"])
        release.set()
        await service.tasks[turn["id"]]
        assert store.turn(turn["id"])["status"] == "committed", store.turn(turn["id"])["error"]
        assert store.session(session["id"])["revision"] == 1
        statuses = [[m for m in request["messages"] if "上下文窗口：" in m.get("content", "")] for request in requests]
        assert statuses[0][0]["role"] == "system"
        assert all(m["role"] == "user" and m["content"].startswith("<tavern_context_status>") for m in statuses[1])
        assert [m["tool_call_id"] for m in requests[1]["messages"] if m["role"] == "tool"] == ["read"]
    finally:
        release.set()
        await service.close()
