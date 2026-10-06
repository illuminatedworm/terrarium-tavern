"""Official-client request contracts, exercised against local HTTP mocks only."""
import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from tavern.core.models import SessionCreate, Settings, TurnRequest
from tavern.full_agent.context import compile_window, load_trace
from tavern.full_agent.tools import AgentTools
from tavern.scripting.model_transport import ModelTransport
from tavern.scripting.request_context import ProviderRequestContext
from tavern.storage.store import Store
from tests.backend.test_full_agent import Model, runtime, turn


def config(api="anthropic", mode="runtime", **kwargs):
    return Settings(api_mode=api, model="claude-opus-5-5" if api == "anthropic" else "gpt-6-sol",
                    api_key="local-audit-secret", base_url="https://fixture.test/v1", use_proxy=False,
                    reasoning_effort="high", cache={"mode": mode}, **kwargs)


def reply(api, items=None):
    if api == "anthropic":
        return {"id": "msg_fixture", "model": "claude-opus-5-5", "stop_reason": "end_turn",
                "content": items if items is not None else [{"type": "text", "text": "done"}],
                "usage": {"input_tokens": 2, "output_tokens": 3, "cache_read_input_tokens": 40,
                          "cache_creation_input_tokens": 5}}
    return {"id": "resp_fixture", "status": "completed", "model": "gpt-6-sol",
            "output": items if items is not None else [{"type": "message", "id": "msg_fixture",
                "role": "assistant", "content": [{"type": "output_text", "text": "done", "annotations": []}]}],
            "usage": {"input_tokens": 47, "output_tokens": 3, "input_tokens_details": {"cached_tokens": 40}}}


def transport(cfg, route, context=None, capture=None):
    return ModelTransport(cfg, cfg.api_key, capture, request_context=context,
                          http_client=httpx.AsyncClient(transport=httpx.MockTransport(route)))


def assistant(provider, response):
    return {"role": "assistant", "content": response.content, **provider.last_assistant_extra_fields,
            "tool_calls": [{"id": c.id, "function": {"name": c.name, "arguments": c.arguments}}
                           for c in provider.last_tool_calls],
            "_native_response": copy.deepcopy(provider.last_native_response)}


@pytest.mark.parametrize("api", ["anthropic", "responses"])
@pytest.mark.parametrize("mode", ["runtime", "cloud"])
async def test_identity_is_independent_of_cache_and_survives_resume(tmp_path, api, mode):
    store = Store(tmp_path)
    device = store.device_id()
    assert device == Store(tmp_path).device_id()
    sid, tid = uuid4(), uuid4().hex
    context = ProviderRequestContext(sid.hex, tid, "window1", device)
    captured, requests = [], []

    def route(request):
        requests.append(request)
        return httpx.Response(200, json=reply(api), headers={"x-codex-turn-state": "opaque-routing-token",
                                                            "request-id": "req_fixture"})

    cfg = config(api, mode)
    p = transport(cfg, route, context, captured.append)
    messages = [{"role": "user", "content": "fixture"}]
    try:
        await p.chat_complete(messages)
        await p.chat_complete(messages)
        saved = context.saved()
        restored = ProviderRequestContext(sid.hex, saved["turn_id"], "window1", store.device_id(), saved["routing"])
        resumed = transport(cfg, route, restored, captured.append)
        try:
            await resumed.chat_complete(messages)
        finally:
            await resumed.close()
        next_turn = transport(cfg, route, ProviderRequestContext(sid.hex, uuid4().hex, "window1", device))
        try:
            await next_turn.chat_complete(messages)
        finally:
            await next_turn.close()
    finally:
        await p.close()
    for request in requests:
        body = json.loads(request.content)
        assert not body.get("stream")
        if api == "anthropic":
            assert request.headers["X-Claude-Code-Session-Id"] == str(sid)
            assert json.loads(body["metadata"]["user_id"]) == {
                "device_id": device, "account_uuid": "", "session_id": str(sid)}
        else:
            assert request.headers["session-id"] == request.headers["thread-id"] == str(sid)
            assert body["prompt_cache_key"] == str(sid)
    if api == "responses":
        assert "x-codex-turn-state" not in requests[0].headers
        assert requests[1].headers["x-codex-turn-state"] == requests[2].headers["x-codex-turn-state"] == "opaque-routing-token"
        assert "x-codex-turn-state" not in requests[3].headers
    assert captured[0]["wire_sha256"] == hashlib.sha256(requests[0].content).hexdigest()
    assert "local-audit-secret" not in json.dumps(captured)
    assert "opaque-routing-token" not in json.dumps(captured)
    assert p.last_response_record["headers"] == {"request-id": "req_fixture"}


@pytest.mark.parametrize("change", ["model", "api_key", "base_url"])
async def test_routing_state_cannot_cross_provider_identity(change):
    cfg = config("responses")
    context = ProviderRequestContext(uuid4().hex, uuid4().hex, "w", "device")
    p = transport(cfg, lambda _: httpx.Response(200, json=reply("responses")), context)
    context.routing["turn_state"] = "old-route"
    await p.close()
    cfg = cfg.model_copy(update={change: "different-model" if change == "model" else
                                 "different-key" if change == "api_key" else "https://other.test/v1"})
    next_provider = transport(cfg, lambda _: httpx.Response(200, json=reply("responses")), context)
    try:
        assert "turn_state" not in context.routing
    finally:
        await next_provider.close()


def native_blocks(api):
    if api == "anthropic":
        return [{"type": "thinking", "thinking": "analysis", "signature": "signed-original"},
                {"type": "tool_use", "id": "tool_1", "name": "read_state", "input": {}},
                {"type": "text", "text": "between calls"},
                {"type": "redacted_thinking", "data": "opaque-original"},
                {"type": "tool_use", "id": "tool_2", "name": "read_state", "input": {"path": "x"}}]
    return [{"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "encrypted-original"},
            {"type": "function_call", "id": "fc_1", "call_id": "tool_1", "name": "read_state", "arguments": "{}"},
            {"type": "message", "id": "msg_1", "role": "assistant", "content": [
                {"type": "output_text", "text": "between calls", "annotations": []}]},
            {"type": "function_call", "id": "fc_2", "call_id": "tool_2", "name": "read_state",
             "arguments": '{"path":"x"}'}]


@pytest.mark.parametrize("api", ["anthropic", "responses"])
async def test_native_items_round_trip_in_original_order_and_invalidate_on_edit(api):
    cfg, original = config(api), native_blocks(api)
    p = transport(cfg, lambda _: httpx.Response(200, json=reply(api, original)))
    try:
        result = await p.chat_complete([{"role": "user", "content": "fixture"}])
        message = assistant(p, result)
        body = p._request([message])
        items = body["messages"][0]["content"] if api == "anthropic" else body["input"]
        assert items == original
        assert p.last_response_record["native"]["items"] == original
        message["content"] = "edited text"
        body = p._request([message])
        assert "edited text" in json.dumps(body)
        assert "between calls" not in json.dumps(body)
        assert "signed-original" not in json.dumps(body)
        assert "encrypted-original" not in json.dumps(body)
        assert "tool_1" in json.dumps(body) and "tool_2" in json.dumps(body)
    finally:
        await p.close()


async def test_explicit_cache_key_wins_and_half_agent_gets_no_identity():
    cfg = config("responses", prompt_cache_key="custom-key")
    p = transport(cfg, lambda _: httpx.Response(200, json=reply("responses")),
                  ProviderRequestContext(uuid4().hex, "turn", "window", "device"))
    plain = transport(cfg, lambda _: httpx.Response(200, json=reply("responses")))
    try:
        assert p._request([])["prompt_cache_key"] == "custom-key"
        assert "prompt_cache_key" not in plain._request([])
    finally:
        await p.close()
        await plain.close()


def game(tmp_path, api="anthropic"):
    store = Store(tmp_path)
    card = store.import_card(Path("samples/雾港来信.json").read_bytes(), "card.json")
    document = store.settings_document()
    document.full_agent.tools.auto_retrieval = False
    document.full_agent.model.model = "claude-opus-5-5" if api == "anthropic" else "gpt-6-sol"
    document.full_agent.model.reasoning_effort = "high"
    document.full_agent.model.cache.retention = "1h" if api == "anthropic" else "default"
    document.full_agent.model.api_mode = api
    document.half_agent.foreground.api_mode = api
    document.half_agent.background.api_mode = api
    document.shared.card_editor.api_mode = api
    document.shared.providers[0].api_key = "local-audit-secret"
    document.shared.providers[0].base_url = "https://fixture.test"
    document.shared.network.api_proxy = False
    store.save_settings(document)
    session = store.new_session(SessionCreate(card_id=card["id"], agent_mode="full_agent"))
    return store, runtime(store), session


def strip_local(messages):
    return [{k: v for k, v in m.items() if not k.startswith("_")} for m in messages]


def handoff_frames(messages):
    return [m for m in messages if m.get("content", "").startswith("<tavern_memory>\n")]


def set_handoff(store, session, text):
    snapshot = copy.deepcopy(session["snapshot"])
    memory = snapshot["full_agent"]["handoff"]
    memory.update(text=text, revision=memory["revision"] + 1, stale=False)
    with store.connection() as db:
        db.execute("UPDATE messages SET snapshot=? WHERE id=?", (json.dumps(snapshot), session["head"]))
    return store.session(session["id"])


async def test_runtime_frames_append_and_handoff_tool_does_not_duplicate_memory(tmp_path):
    store, service, session = game(tmp_path)
    session = set_handoff(store, session, "MEMORY_A")
    model = Model(("read_state", {}), ("write_handoff", {"content": "MEMORY_B"}),
                  ("write_handoff", {"content": "MEMORY_B"}), "narrative")
    try:
        result, current = await turn(service, session, model)
        assert result["status"] == "committed", result["error"]
        requests = [strip_local(r["messages"]) for r in model.requests]
        for first, second in zip(requests, requests[1:]):
            assert first == second[:len(first)]
        for messages in requests:
            assert len(handoff_frames(messages)) == 1
            assert "MEMORY_A" in handoff_frames(messages)[0]["content"]
            assert sum("<tavern_context_status>" in m.get("content", "") for m in messages) == 1
            assert not any("还可调用模型" in m.get("content", "") for m in messages)
        assert "MEMORY_B" in json.dumps(requests[-1])
        assert current["snapshot"]["full_agent"]["handoff"]["text"] == "MEMORY_B"
        baseline_epoch = result["preview"]["context_ledger"]["epoch"]
        for status_count in (2, 3):
            next_model = Model("next narrative")
            result, current = await turn(service, current, next_model)
            assert result["status"] == "committed", result["error"]
            assert result["preview"]["context_ledger"]["epoch"] == baseline_epoch
            assert len(handoff_frames(next_model.requests[0]["messages"])) == 1
            assert sum("<tavern_context_status>" in m.get("content", "")
                       for m in next_model.requests[0]["messages"]) == status_count
        assert len([r for r in load_trace(store, current["id"], current["messages"])
                    if r["message"].get("source") == "handoff"]) == 1
    finally:
        await service.close()


async def test_window_inventory_tracks_note_changes_and_rollover(tmp_path):
    store, service, session = game(tmp_path)
    model = Model(("read_state", {}),
                  ("write_note", {"key": "plan", "content": "下一步计划", "kind": "plan"}),
                  ("archive_note", {"key": "plan"}),
                  ("new_context", {"content": "已归档计划，继续当前故事", "keep_recent": 0}),
                  ("read_state", {}), "narrative")
    try:
        result, current = await turn(service, session, model)
        assert result["status"] == "committed", result["error"]
        notices = [[m["content"] for m in request["messages"]
                    if "<tavern_context_status>" in m.get("content", "")] for request in model.requests]
        assert [len(items) for items in notices] == [1, 1, 2, 3, 1, 1]
        assert "有效 1 条，已归档 0 条" in notices[2][-1]
        assert "有效 0 条，已归档 1 条" in notices[3][-1]
        assert "当前第 2 个" in notices[4][0]
        assert current["snapshot"]["full_agent"]["notes"]["plan"]["status"] == "archived"
    finally:
        await service.close()


@pytest.mark.parametrize("fail_last_request", [False, True])
async def test_call_notifications_are_absent_even_on_last_request_and_resume(tmp_path, fail_last_request):
    store, service, session = game(tmp_path)
    document = store.settings_document()
    document.full_agent.tools.max_steps = 3
    store.save_settings(document)
    model = Model(("read_state", {}), ("read_state", {}),
                  RuntimeError("last request disconnected") if fail_last_request else "narrative")
    try:
        result, current = await turn(service, session, model)
        assert result["status"] == ("error" if fail_last_request else "committed"), result["error"]
        for request in model.requests:
            assert not any("tavern_call_budget" in m.get("content", "")
                           or "tavern_last_call_notice" in m.get("content", "")
                           or "最后一次模型请求" in m.get("content", "") for m in request["messages"])
        next_model = Model("next narrative")
        if fail_last_request:
            assert current["revision"] == 0
            service.full.provider_factory = lambda: next_model
            resumed = service.submit(current["id"], TurnRequest(
                text=result["request"]["text"], operation_id="resume-last-call", expected_revision=0,
                resume_turn_id=result["id"]))
            await service.tasks[resumed["id"]]
            following = store.turn(resumed["id"])
        else:
            following, _ = await turn(service, current, next_model)
        assert following["status"] == "committed", following["error"]
        assert not any("最后一次模型请求" in m.get("content", "") for m in next_model.requests[0]["messages"])
        assert "tavern_call_budget" not in str(next_model.requests[0]["messages"])
    finally:
        await service.close()


async def test_memory_updates_compare_effective_version_and_empty_is_explicit(tmp_path):
    store, service, session = game(tmp_path)
    session = set_handoff(store, session, "MEMORY_A")
    try:
        _, session = await turn(service, session, Model("first"))
        expected = ["MEMORY_A"]
        for value in ("MEMORY_A", "MEMORY_B", "MEMORY_A", ""):
            previous = session["snapshot"]["full_agent"]["handoff"]["text"]
            session = set_handoff(store, session, value)
            model = Model("continue")
            result, session = await turn(service, session, model)
            assert result["status"] == "committed", result["error"]
            if previous != value:
                expected.append(value)
            frames = [m for m in result["preview"]["trace"] if m.get("source") == "handoff"]
            assert [m["_runtime_context"]["value"] for m in frames] == expected
            assert len(handoff_frames(model.requests[0]["messages"])) == len(expected)
        assert "当前 handoff 为空" in frames[-1]["content"]
    finally:
        await service.close()


async def test_failed_memory_write_and_explicit_repeated_reads_keep_tool_pairs(tmp_path):
    store, service, session = game(tmp_path)
    session = set_handoff(store, session, "MEMORY_A")
    model = Model(("write_handoff", {"content": ""}), ("read_state", {}), ("read_state", {}), "done")
    try:
        result, current = await turn(service, session, model)
        assert result["status"] == "committed", result["error"]
        assert current["snapshot"]["full_agent"]["handoff"]["text"] == "MEMORY_A"
        messages = model.requests[-1]["messages"]
        assert len(handoff_frames(messages)) == 1
        results = [m for m in messages if m["role"] == "tool"]
        assert len(results) == 3 and "error" in json.loads(results[0]["content"])
        assert results[1]["content"] == results[2]["content"]
        assert results[1]["tool_call_id"] != results[2]["tool_call_id"]
    finally:
        await service.close()


async def test_interrupted_tool_batch_resumes_only_unfinished_call(tmp_path, monkeypatch):
    store, service, session = game(tmp_path)

    class Batch(Model):
        async def chat_complete(self, messages, **kwargs):
            self.requests.append(copy.deepcopy(messages))
            self.last_tool_calls = [SimpleNamespace(id=f"call-{index}", name="write_handoff",
                arguments=json.dumps({"content": value})) for index, value in enumerate(("MEMORY_A", "MEMORY_B"))]
            return SimpleNamespace(content="", usage={}, finish_reason="stop")

    original = AgentTools.execute
    interrupted, executed = False, []

    async def execute(tools, name, arguments):
        nonlocal interrupted
        value = json.loads(arguments)["content"]
        if value == "MEMORY_B" and not interrupted:
            interrupted = True
            raise RuntimeError("synthetic interruption before second tool")
        executed.append(value)
        return await original(tools, name, arguments)

    monkeypatch.setattr(AgentTools, "execute", execute)
    try:
        failed, _ = await turn(service, session, Batch())
        assert failed["status"] == "error"
        assert executed == ["MEMORY_A"]
        progress = store.full_agent_progress(failed["id"])
        assert progress["snapshot"]["full_agent"]["handoff"]["text"] == "MEMORY_A"
    finally:
        await service.close()
    store = Store(store.root)
    service = runtime(store)
    model = Model("done after restart")
    service.full.provider_factory = lambda: model
    try:
        resumed = service.submit(session["id"], TurnRequest(text=failed["request"]["text"],
            operation_id=uuid4().hex, expected_revision=0, resume_turn_id=failed["id"]))
        await service.tasks[resumed["id"]]
        result = store.turn(resumed["id"])
        assert result["status"] == "committed", result["error"]
        assert executed == ["MEMORY_A", "MEMORY_B"]
        assert len(model.requests) == 1
        assert store.session(session["id"])["snapshot"]["full_agent"]["handoff"]["text"] == "MEMORY_B"
    finally:
        await service.close()


@pytest.mark.parametrize("api", ["anthropic", "responses"])
async def test_native_full_agent_http_replay_final_reply_and_cache_anchor(tmp_path, monkeypatch, api):
    import tavern.full_agent.agent as agent_module

    store, service, session = game(tmp_path, api)
    session = store.set_cache_breakpoint(session["id"], session["revision"], True, None, 1, 1)
    requests, responses = [], []
    tool_items = native_blocks(api)
    final_items = ([{"type": "thinking", "thinking": "final thought", "signature": "final-signature"},
                    {"type": "text", "text": "FINAL_NARRATIVE"}] if api == "anthropic" else
                   [{"type": "reasoning", "id": "rs_final", "summary": [], "encrypted_content": "final-encrypted"},
                    {"type": "message", "id": "msg_final", "role": "assistant", "content": [
                        {"type": "output_text", "text": "FINAL_NARRATIVE", "annotations": []}]}])

    def route(request):
        requests.append(request)
        payload = reply(api, tool_items if len(requests) == 1 else final_items)
        responses.append(payload)
        return httpx.Response(200, json=payload, headers={"x-codex-turn-state": "route-state"})

    async def factory(settings, capture=None, *, request_context=None):
        return transport(settings, route, request_context, capture)

    monkeypatch.setattr(agent_module, "make_provider", factory)
    try:
        for _ in range(2):
            result = service.submit(session["id"], TurnRequest(text="continue", operation_id=uuid4().hex,
                                                               expected_revision=session["revision"]))
            await service.tasks[result["id"]]
            result = store.turn(result["id"])
            assert result["status"] == "committed", result["error"]
            session = store.session(session["id"])
        bodies = [json.loads(r.content) for r in requests]
        assert len(bodies) == 3
        assert all(not b.get("stream") for b in bodies)
        if api == "anthropic":
            echoed = next(m["content"] for m in bodies[1]["messages"]
                          if m["role"] == "assistant" and any(p.get("type") == "tool_use" for p in m["content"]))
            assert echoed == tool_items
            final = next(m["content"] for m in bodies[2]["messages"]
                         if any(p.get("text") == "FINAL_NARRATIVE" for p in m["content"]))
            assert final[0] == final_items[0]
            assert final[-1]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
            assert json.dumps(bodies[2]).count('"text": "FINAL_NARRATIVE"') == 1
        else:
            at = next(i for i, m in enumerate(bodies[1]["input"]) if m.get("id") == "rs_1")
            assert bodies[1]["input"][at:at + len(tool_items)] == tool_items
            final = next(m for m in bodies[2]["input"] if m.get("id") == "msg_final")
            assert final["content"][-1]["prompt_cache_breakpoint"] == {"mode": "explicit"}
            assert any(m.get("encrypted_content") == "final-encrypted" for m in bodies[2]["input"])
            assert requests[1].headers["x-codex-turn-state"] == "route-state"
            assert "x-codex-turn-state" not in requests[2].headers
        events = store.events(result["id"])
        assert next(e for e in events if e["kind"] == "provider_request")["payload"]["headers"]
        assert next(e for e in events if e["kind"] == "full_agent_response")["payload"]["provider_response"]["native"]
        assert result["preview"]["cache"]["message_id"] == session["messages"][-3]["id"]
    finally:
        await service.close()


async def test_preview_and_committed_replay_use_same_frames_without_new_memory(tmp_path):
    from httpx import ASGITransport, AsyncClient

    from tavern.application.api import create_app
    from tavern.full_agent.tools import tool_definitions

    store, service, session = game(tmp_path)
    session = set_handoff(store, session, "MEMORY_A")
    try:
        _, session = await turn(service, session, Model("first"), allow_content_edit=True)
        app = create_app(store.root)
        payload = {"text": "next", "update_variables": True, "allow_content_edit": True}
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
            response = await client.post(f"/api/sessions/{session['id']}/preview", json=payload)
            assert response.status_code == 200, response.text
            preview = response.json()
        settings = store.settings().for_role("full_agent")
        request = TurnRequest(operation_id="direct", expected_revision=session["revision"], **payload)
        actual = compile_window(store.session_card(session["id"])["document"], session, session["messages"],
            session["snapshot"], "next", settings,
            tool_definitions(request, session, settings.full_agent_config, settings=settings,
                             document=store.session_card(session["id"])["document"]),
            trace=load_trace(store, session["id"], session["messages"]),
            require_state_patch=True, allow_content_edit=True)
        assert preview["messages"] == actual["messages"]
        assert [m["content"] for m in actual["runtime_trace"] if m["source"] == "turn_instruction"] == ["允许修改设定：True。"]
        assert [m["sdk_role"] for m in preview["trace"] if m["source"] == "turn_instruction"] == ["system"]
        assert len(handoff_frames(preview["messages"])) == 1
        assert "handoff" not in [u["source"] for u in preview["context_ledger"]["updates"]]
    finally:
        await service.close()


async def test_model_switch_rebuilds_portable_history_and_does_not_resurrect_old_thinking(tmp_path, monkeypatch):
    import tavern.full_agent.agent as agent_module

    store, service, session = game(tmp_path)
    requests = []

    async def factory(settings, capture=None, *, request_context=None):
        def route(request):
            requests.append(request)
            api = settings.api_mode
            items = ([{"type": "thinking", "thinking": "hidden", "signature": "MODEL_PRIVATE_SIGNATURE"},
                      {"type": "text", "text": "portable narrative"}] if api == "anthropic" else
                     [{"type": "reasoning", "id": "rs_private", "summary": [], "encrypted_content": "MODEL_PRIVATE_SIGNATURE"},
                      {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "portable narrative"}]}])
            return httpx.Response(200, json=reply(api, items), headers={"x-codex-turn-state": "old-route"})
        return transport(settings, route, request_context, capture)

    monkeypatch.setattr(agent_module, "make_provider", factory)
    session = set_handoff(store, session, "MEMORY_A")
    try:
        epochs = []
        for api, model in [("anthropic", "claude-opus-5-5"), ("anthropic", "claude-sonnet-4-6"),
                           ("anthropic", "claude-opus-5-5"), ("responses", "gpt-6-sol")]:
            doc = store.settings_document()
            doc.full_agent.model.api_mode = api
            doc.half_agent.foreground.api_mode = api
            doc.half_agent.background.api_mode = api
            doc.shared.card_editor.api_mode = api
            doc.full_agent.model.model = model
            store.save_settings(doc)
            pending = service.submit(session["id"], TurnRequest(text="next", operation_id=uuid4().hex,
                                                                expected_revision=session["revision"]))
            await service.tasks[pending["id"]]
            result = store.turn(pending["id"])
            assert result["status"] == "committed", result["error"]
            session = store.session(session["id"])
            audit = result["preview"]["context_ledger"]
            epochs.append(audit["epoch"])
            assert len(handoff_frames(result["preview"]["messages"])) == 1
            assert session["snapshot"]["full_agent"]["handoff"]["text"] == "MEMORY_A"
            assert "MODEL_PRIVATE_SIGNATURE" not in requests[-1].content.decode()
            if len(epochs) > 1:
                assert audit["rebuild_reason"] == "provider_or_model_changed"
                assert "portable narrative" in requests[-1].content.decode()
        assert len(set(epochs)) == 4
        assert len({r.headers.get("x-claude-code-session-id", r.headers.get("session-id")) for r in requests}) == 1
    finally:
        await service.close()


async def test_rollover_and_history_edits_rebase_without_reviving_old_runtime_memory(tmp_path):
    store, service, session = game(tmp_path)
    session = set_handoff(store, session, "MEMORY_A")
    try:
        _, session = await turn(service, session, Model("first"))
        model = Model(("new_context", {"content": "MEMORY_B", "keep_recent": 0}), "after rollover")
        result, session = await turn(service, session, model)
        assert result["status"] == "committed", result["error"]
        messages = model.requests[-1]["messages"]
        assert len(handoff_frames(messages)) == 1
        assert "MEMORY_B" in handoff_frames(messages)[0]["content"]
        assert "MEMORY_A" not in json.dumps(messages)
        edited = store.edit_message(session["id"], session["head"], "edited narrative", session["revision"])
        model = Model("continue")
        result, edited = await turn(service, edited, model)
        assert result["status"] == "committed", result["error"]
        assert result["preview"]["context_ledger"]["rebuild_reason"]
        assert "edited narrative" in json.dumps(model.requests[0]["messages"])
        assert "after rollover" not in json.dumps(model.requests[0]["messages"])
        assert len(handoff_frames(model.requests[0]["messages"])) == 1
    finally:
        await service.close()


async def test_fork_remaps_runtime_references_and_uses_new_session_identity(tmp_path):
    store, service, session = game(tmp_path)
    try:
        result, session = await turn(service, session, Model(("read_state", {}), ("patch_state", {"operations": [], "reason": "none"}),
            ("patch_state", {"operations": [{"op": "add", "path": "/audit", "value": True}], "reason": "audit"}), "first"),
            update_variables=True)
        assert result["status"] == "committed", result["error"]
        fork = store.fork_session(session["id"], session["head"], session["revision"])
        original_trace = load_trace(store, session["id"], session["messages"])
        fork_trace = load_trace(store, fork["id"], fork["messages"])
        old_ids = {m["id"] for m in session["messages"]}
        for item in fork_trace:
            value = item["message"].get("_runtime_context", {}).get("value")
            if isinstance(value, dict) and value.get("input_id"):
                assert value["input_id"] not in old_ids
                assert value["input_id"] in {m["id"] for m in fork["messages"]}
        assert [m["message"].get("tool_call_id") for m in original_trace] == [
            m["message"].get("tool_call_id") for m in fork_trace]
        result, fork = await turn(service, fork, Model("fork continuation"))
        assert result["status"] == "committed", result["error"]
        assert result["preview"]["cache"]["scope"] == fork["id"] != session["id"]
    finally:
        await service.close()


async def test_responses_routing_state_and_successful_memory_survive_failed_request(tmp_path, monkeypatch):
    import tavern.full_agent.agent as agent_module

    store, service, session = game(tmp_path, "responses")
    requests = []

    def route(request):
        requests.append(request)
        if len(requests) == 2:
            raise httpx.ReadTimeout("synthetic timeout", request=request)
        items = ([{"type": "reasoning", "id": "rs_memory", "summary": [], "encrypted_content": "opaque-memory"},
                  {"type": "function_call", "id": "fc_memory", "call_id": "call_memory", "name": "write_handoff",
                   "arguments": '{"content":"MEMORY_B"}'}] if len(requests) == 1 else None)
        return httpx.Response(200, json=reply("responses", items), headers={"x-codex-turn-state": "persisted-route"})

    async def factory(settings, capture=None, *, request_context=None):
        return transport(settings, route, request_context, capture)

    monkeypatch.setattr(agent_module, "make_provider", factory)
    try:
        failed = service.submit(session["id"], TurnRequest(text="next", operation_id=uuid4().hex, expected_revision=0))
        await service.tasks[failed["id"]]
        failed = store.turn(failed["id"])
        assert failed["status"] == "error"
        saved = store.full_agent_progress(failed["id"])
        assert saved["request_context"]["routing"]["turn_state"] == "persisted-route"
    finally:
        await service.close()
    store = Store(store.root)
    service = runtime(store)
    try:
        resumed = service.submit(session["id"], TurnRequest(text="next", operation_id=uuid4().hex,
            expected_revision=0, resume_turn_id=failed["id"]))
        await service.tasks[resumed["id"]]
        result = store.turn(resumed["id"])
        assert result["status"] == "committed", result["error"]
        assert len(requests) == 3
        assert requests[2].headers["session-id"] == requests[0].headers["session-id"]
        assert requests[2].headers["x-codex-turn-state"] == "persisted-route"
        state = store.session(session["id"])["snapshot"]["full_agent"]["handoff"]
        assert state["text"] == "MEMORY_B" and state["revision"] == 1
        body = json.loads(requests[2].content)
        assert any(i.get("encrypted_content") == "opaque-memory" for i in body["input"])
        assert sum(i.get("type") == "function_call_output" for i in body["input"]) == 1
        request_event = next(e["payload"] for e in store.events(result["id"]) if e["kind"] == "provider_request")
        assert request_event["request_context"]["turn_id"] == saved["request_context"]["turn_id"]
        assert "persisted-route" not in json.dumps(store.events(result["id"]))
    finally:
        await service.close()


async def test_legacy_tool_trace_is_upgraded_without_inventing_native_data(tmp_path):
    store, service, session = game(tmp_path)
    try:
        _, session = await turn(service, session, Model(("write_handoff", {"content": "MEMORY_B"}), "old narrative"))
        with store.connection() as db:
            rows = db.execute("SELECT seq,payload FROM full_agent_trace WHERE message_id=?", (session["head"],)).fetchall()
            for row in rows:
                message = json.loads(row["payload"])
                if message.get("tool_calls") or message["role"] == "tool":
                    message = {k: v for k, v in message.items() if not k.startswith("_")}
                    db.execute("UPDATE full_agent_trace SET payload=? WHERE message_id=? AND seq=?",
                               (json.dumps(message), session["head"], row["seq"]))
                else:
                    db.execute("DELETE FROM full_agent_trace WHERE message_id=? AND seq=?", (session["head"], row["seq"]))
        model = Model("new narrative")
        result, session = await turn(service, session, model)
        assert result["status"] == "committed", result["error"]
        assert result["preview"]["context_ledger"]["rebuild_reason"] == "legacy_context"
        assert "old narrative" in json.dumps(model.requests[0]["messages"])
        assert "MEMORY_B" in json.dumps(model.requests[0]["messages"])
        assert any(m["role"] == "tool" for m in model.requests[0]["messages"])
        assert not any(m.get("_native_response") for m in model.requests[0]["messages"])
    finally:
        await service.close()


@pytest.mark.parametrize("version", [1, 2, 3, 4, 5, 6, 7])
def test_legacy_permission_frames_are_removed_when_rebuilding_context(monkeypatch, version):
    from tavern.full_agent import context_ledger
    from tavern.full_agent.state import new_state

    session = {"id": uuid4().hex, "persona": "user", "agent_mode": "full_agent"}
    snapshot = {"state": {}, "full_agent": new_state()}
    args = ({"name": "fixture"}, session, [], snapshot, "next", config(), [])
    with monkeypatch.context() as legacy:
        legacy.setattr(context_ledger, "VERSION", version)
        first = compile_window(*args, input_id="u1", allow_content_edit=True)
    second = compile_window(*args, input_id="u1", continuation=first["context_additions"])
    assert second["context_ledger"]["rebuild_reason"] == "context_version_changed"
    assert second["context_ledger"]["epoch"] != first["context_ledger"]["epoch"]
    assert not any(m["source"] == "turn_instruction" for m in second["trace"])
    assert not any("允许修改设定" in m["content"] for m in second["messages"])
    third = compile_window(*args, input_id="u1",
                           continuation=first["context_additions"] + second["context_additions"])
    assert third["context_ledger"]["rebuild_reason"] is None
    assert third["messages"] == second["messages"]


@pytest.mark.parametrize("version", [5, 7])
def test_rebuild_retires_legacy_countdowns_and_keeps_tool_exchanges(monkeypatch, version):
    from tavern.full_agent import context_ledger
    from tavern.full_agent.state import new_state

    session = {"id": uuid4().hex, "persona": "user", "agent_mode": "full_agent"}
    snapshot = {"state": {}, "full_agent": new_state()}
    args = ({"name": "fixture"}, session, [], snapshot, "next", config(), [])
    with monkeypatch.context() as legacy:
        legacy.setattr(context_ledger, "VERSION", version)
        first = compile_window(*args, input_id="u1")
    records = first["context_additions"]
    old_status = next(m for m in records if m.get("source") == "context_status")
    for remaining in (24, 23, 22):
        notice = copy.deepcopy(old_status)
        notice["content"] = f"本回合还可调用模型 {remaining} 次。"
        notice["_runtime_context"]["value"] = notice["content"]
        records.append(notice)
    for source, content in (("call_budget", "本次运行模型请求上限：24；包含本次请求的剩余次数：1。"),
                            ("last_call_notice", "这是本回合最后一次模型请求。")):
        notice = copy.deepcopy(old_status)
        notice.update(source=source, message_type=source, content=content)
        notice["_runtime_context"]["value"] = content
        records.append(notice)
    records.extend([
        {"role": "assistant", "content": "", "tool_calls": [{"id": "read", "type": "function",
            "function": {"name": "read_state", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "read", "content": "原有完整工具结果"},
    ])
    rebuilt = compile_window(*args, input_id="u1", continuation=records)
    assert rebuilt["context_ledger"]["rebuild_reason"] == "context_version_changed"
    assert not any("还可调用模型" in m.get("content", "") for m in rebuilt["messages"])
    assert not any(m["source"] in ("call_budget", "last_call_notice") for m in rebuilt["trace"])
    assert "剩余次数" not in str(rebuilt["messages"])
    assert "最后一次模型请求" not in str(rebuilt["messages"])
    assert sum(m["source"] == "context_status" for m in rebuilt["trace"]) == 1
    assert [m["content"] for m in rebuilt["messages"] if m["role"] == "tool"] == ["原有完整工具结果"]
    again = compile_window(*args, input_id="u1", continuation=records + rebuilt["context_additions"])
    assert again["messages"] == rebuilt["messages"]


def test_history_macro_change_rebuilds_once_and_records_why():
    from tavern.full_agent.state import new_state

    settings = config()
    session = {"id": uuid4().hex, "persona": "user", "agent_mode": "full_agent"}
    snapshot = {"state": {"audit": 0}, "full_agent": new_state()}
    history = [{"id": "u1", "parent": None, "role": "user", "raw": "Value {{getvar::audit}}"}]
    args = ({"name": "fixture"}, session, history, snapshot, "next", settings, [])
    first = compile_window(*args, input_id="u2")
    snapshot["state"]["audit"] = 1
    second = compile_window(*args, input_id="u2", continuation=first["context_additions"])
    assert second["context_ledger"]["rebuild_reason"] == "history_content_changed"
    assert second["context_ledger"]["epoch"] != first["context_ledger"]["epoch"]
    third = compile_window(*args, input_id="u2", continuation=first["context_additions"] + second["context_additions"])
    assert third["context_ledger"]["rebuild_reason"] is None
    assert third["messages"] == second["messages"]


@pytest.mark.parametrize("previous", ["before_ledger", "before_read_guards"])
def test_only_exact_old_default_memory_prompt_is_migrated(previous):
    from tavern.core.settings import SettingsDocument, migrate_settings
    from tavern.full_agent.config import FULL_AGENT_PROMPT

    raw = SettingsDocument().model_dump()
    if previous == "before_ledger":
        old = Path("tests/fixtures/full_agent_prompt_before_ledger.txt").read_text(encoding="utf-8")
    else:
        old = FULL_AGENT_PROMPT.replace("不要只留下当前状态的快照。",
            "不要只留下当前状态的快照。覆盖长期笔记前，先用 read_note 读取当前版本。")
    raw["full_agent"]["system_prompt"] = old
    migrated = migrate_settings(raw)
    assert migrated.full_agent.system_prompt == FULL_AGENT_PROMPT != old
    assert migrated.revisions["full_agent"] == raw["revisions"]["full_agent"] + 1
    assert migrate_settings(migrated.model_dump()) == migrated
    raw["full_agent"]["system_prompt"] = old + "\nA custom instruction."
    assert migrate_settings(raw).full_agent.system_prompt == raw["full_agent"]["system_prompt"]
    legacy = Settings(full_agent_system_prompt=old).model_dump()
    assert migrate_settings(legacy).full_agent.system_prompt == FULL_AGENT_PROMPT
    legacy["full_agent_system_prompt"] = old + "\nA custom instruction."
    assert migrate_settings(legacy).full_agent.system_prompt == legacy["full_agent_system_prompt"]


@pytest.mark.parametrize("api", ["anthropic", "responses"])
async def test_explicit_identity_overrides_survive_cloud_mode(api):
    cfg = config(api, "cloud")
    cfg.cache.request = {"metadata": {"user_id": "custom-user"}, "prompt_cache_key": "custom-affinity"}
    context = ProviderRequestContext(uuid4().hex, "turn", "window", "device")
    p = transport(cfg, lambda _: httpx.Response(200, json=reply(api)), context)
    try:
        body = p._request([{"role": "user", "content": "hello"}])
        if api == "anthropic":
            assert body["metadata"]["user_id"] == "custom-user"
        else:
            assert body["prompt_cache_key"] == "custom-affinity"
    finally:
        await p.close()


@pytest.mark.parametrize("api", ["anthropic", "responses"])
async def test_source_rebase_preserves_unchanged_native_replies_from_same_model(tmp_path, monkeypatch, api):
    import tavern.full_agent.agent as agent_module

    store, service, session = game(tmp_path, api)
    requests, original = [], native_blocks(api)

    def route(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=reply(api, original if len(requests) == 1 else None))

    async def factory(settings, capture=None, *, request_context=None):
        return transport(settings, route, request_context, capture)

    monkeypatch.setattr(agent_module, "make_provider", factory)
    try:
        for allow in (False, True):
            pending = service.submit(session["id"], TurnRequest(text="next", operation_id=uuid4().hex,
                expected_revision=session["revision"], allow_content_edit=allow))
            await service.tasks[pending["id"]]
            result = store.turn(pending["id"])
            assert result["status"] == "committed", result["error"]
            session = store.session(session["id"])
        assert result["preview"]["context_ledger"]["rebuild_reason"] == "context_sources_changed"
        if api == "anthropic":
            echoed = next(m["content"] for m in requests[-1]["messages"]
                          if any(p.get("type") == "tool_use" for p in m["content"]))
            assert echoed == original
        else:
            at = next(i for i, m in enumerate(requests[-1]["input"]) if m.get("id") == "rs_1")
            assert requests[-1]["input"][at:at + len(original)] == original
    finally:
        await service.close()


async def test_rejected_last_response_and_correction_survive_step_limit(tmp_path):
    store, service, session = game(tmp_path)
    doc = store.settings_document()
    doc.full_agent.tools.max_steps = 2
    store.save_settings(doc)
    try:
        result, current = await turn(service, session, Model("rejected one", "rejected two"), update_variables=True)
        assert result["status"] == "error" and current["revision"] == 0
        progress = store.full_agent_progress(result["id"])
        rejected = [i["message"] for i in progress["new_trace"] if i["message"].get("_full_agent_rejected")]
        assert [m["content"] for m in rejected] == ["rejected one", "rejected two"]
        assert "未提交" in progress["continuation"][-1]["content"]
    finally:
        await service.close()


@pytest.mark.parametrize("api,field", [("anthropic", "metadata.user_id"), ("responses", "prompt_cache_key")])
async def test_rejected_identity_does_not_trigger_a_cache_fallback_retry(api, field):
    cfg = config(api)
    cfg.cache.request = {"metadata": {"user_id": "explicit"}} if api == "anthropic" else {}
    requests = []

    def route(request):
        requests.append(request)
        return httpx.Response(400, json={"error": {"message": f"Unsupported parameter: {field}", "param": field}})

    context = ProviderRequestContext(uuid4().hex, "turn", "window", "device")
    p = transport(cfg, route, context)
    try:
        with pytest.raises(httpx.HTTPStatusError):
            await p.chat_complete([{"role": "user", "content": "fixture", "_cache_breakpoint": True}])
        assert len(requests) == 1
    finally:
        await p.close()
