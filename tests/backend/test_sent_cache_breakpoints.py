"""Sent endpoint retention through the real serializers and HTTP dispatch path."""

import asyncio
import copy
import json
from contextlib import asynccontextmanager
from uuid import uuid4

import httpx
import pytest

from tavern.core.cache_policy import CacheSettings
from tavern.core.models import SessionCreate, TurnRequest
from tavern.scripting.model_transport import ModelTransport
from tavern.scripting.native_response import attachment
from tavern.scripting.request_context import OperationContexts, response_origin
from tavern.storage.backup import backup, restore
from tavern.storage.store import Store
from tests.backend.test_unified_cache import config, reply


def messages(at, count=6):
    result = [
        {
            "role": "system" if i == 0 else "assistant" if i % 2 == 0 else "user",
            "content": f"block {i}",
            "_cache_static": i == 0,
        }
        for i in range(count)
    ]
    result[at]["_cache_breakpoint"] = True
    return result


def marked(body, api):
    return [
        part["text"]
        for message in body["input" if api == "responses" else "messages"]
        for part in message.get("content", [])
        if isinstance(part, dict) and "prompt_cache_breakpoint" in part
    ]


@asynccontextmanager
async def transport(api, seen, *, context=None, route=None, capture=None, cfg=None):
    cfg = cfg or config(api)
    cfg.model = "gpt-6-sol"

    def success(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=reply(api))

    provider = ModelTransport(
        cfg,
        cfg.api_key,
        capture,
        request_context=context,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(route or success)),
    )
    try:
        yield provider
    finally:
        await provider.close()


@pytest.mark.parametrize("api", ["responses", "chat_completions"])
async def test_only_dispatched_boundaries_accumulate_and_manual_rewind_keeps_them(api):
    seen, captured = [], []
    original = messages(0)
    async with transport(api, seen, capture=captured.append) as p:
        p._request(messages(1))  # A preview must not create a sent fact.
        await p._prepare(messages(3), None, False, {})
        await p.chat_complete(original)
        await p.chat_complete(messages(2))
        await p.chat_complete(messages(4))
        await p.chat_complete(messages(0))
    assert [marked(body, api) for body in seen] == [
        ["block 0"],
        ["block 0", "block 2"],
        ["block 0", "block 2", "block 4"],
        ["block 0", "block 2", "block 4"],
    ]
    assert original == messages(0)
    assert [point["message_index"] for point in captured[-1]["cache"]["sent_breakpoints"]] == [0, 2, 4]


@pytest.mark.parametrize("api", ["responses", "chat_completions"])
async def test_sent_markers_replay_even_without_a_new_planned_boundary(api):
    seen = []
    current = messages(2)
    current[0]["role"] = "user"
    following = copy.deepcopy(current)
    following[2].pop("_cache_breakpoint")
    async with transport(api, seen) as p:
        _, diagnostic, _ = await p._prepare(following, None, False, {})
        assert diagnostic["status"] == "fallback"
        await p.chat_complete(current)
        assert marked(p._request(following), api) == ["block 2"]
        await p.chat_complete(following)
        assert marked(seen[-1], api) == ["block 2"]


@pytest.mark.parametrize("api", ["responses", "chat_completions"])
async def test_all_sent_markers_are_retained_without_a_local_lookup_limit(api):
    seen = []
    async with transport(api, seen) as p:
        for at in range(55):
            await p.chat_complete(messages(at, 56))
    assert marked(seen[-1], api) == [f"block {at}" for at in range(55)]


@pytest.mark.parametrize("api", ["responses", "chat_completions"])
async def test_changed_prefix_and_window_do_not_relocate_old_markers(api):
    seen = []
    async with transport(api, seen) as p:
        await p.chat_complete(messages(0))
        await p.chat_complete(messages(2))
        edited = messages(4)
        edited[1]["content"] = "edited input"
        await p.chat_complete(edited)
        # The static prefix remains reusable; the old narrative prefix does not.
        assert marked(seen[-1], api) == ["block 0", "block 4"]
        await p.chat_complete(messages(4)[2:])
        assert marked(seen[-1], api) == ["block 4"]
        await p.chat_complete(messages(4))
        assert marked(seen[-1], api) == ["block 0", "block 2", "block 4"]
        await p.chat_complete(
            messages(4),
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "new_tool",
                        "description": "changed tools",
                        "parameters": {"type": "object"},
                    },
                }
            ],
        )
        assert marked(seen[-1], api) == ["block 4"]


@pytest.mark.parametrize("api", ["responses", "chat_completions"])
async def test_model_cloud_and_fallback_do_not_leak_markers(api):
    seen = []
    async with transport(api, seen) as p:
        await p.chat_complete(messages(0))
        clone = p.with_model("gpt-6.1-sol")
        await clone.chat_complete(messages(2))
        assert marked(seen[-1], api) == ["block 2"]
        p.settings.cache = CacheSettings(mode="cloud")
        await p.chat_complete(messages(4))
        assert marked(seen[-1], api) == []
        p.settings.cache = CacheSettings(mode="runtime")
        body, _, _ = await p._prepare(messages(4), None, False, {}, fallback="unsupported")
        assert marked(body, api) == []
        await p.chat_complete(messages(2))
        assert marked(seen[-1], api) == ["block 0", "block 2"]


@pytest.mark.parametrize("api", ["responses", "chat_completions"])
@pytest.mark.parametrize("failure", ["connect", "read", "cancel", "status", "malformed"])
async def test_send_facts_survive_failures_but_not_unsent_connection_attempts(api, failure):
    seen = []

    async def route(request):
        if failure == "connect":
            raise httpx.ConnectError("not sent", request=request)
        await request.extensions["trace"]("http11.send_request_body.complete", {})
        if failure == "read":
            raise httpx.ReadTimeout("sent, no response", request=request)
        if failure == "cancel":
            raise asyncio.CancelledError()
        return httpx.Response(500 if failure == "status" else 200, content=b"invalid JSON")

    async with transport(api, seen, route=route) as p:
        with pytest.raises((httpx.HTTPError, json.JSONDecodeError, asyncio.CancelledError)):
            await p.chat_complete(messages(0))
        # Request building does not need a successful response or story commit.
        body, _, _ = await p._prepare(messages(2), None, False, {})
        assert marked(body, api) == (["block 2"] if failure == "connect" else ["block 0", "block 2"])


async def test_native_responses_replays_exact_content_block_endpoint():
    api, seen = "responses", []
    async with transport(api, seen) as p:
        current = messages(2)
        payload = reply(api)
        payload["output"] = [
            {"type": "reasoning", "id": "rs_1", "encrypted_content": "opaque", "summary": []},
            {
                "type": "message",
                "id": "msg_1",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "block "}, {"type": "output_text", "text": "2"}],
            },
        ]
        current[2]["_native_response"] = attachment(payload, response_origin(p.settings, p.key), current[2])
        await p.chat_complete(current)
        later = copy.deepcopy(current)
        later[2].pop("_cache_breakpoint")
        later[4]["_cache_breakpoint"] = True
        await p.chat_complete(later)
        wire = seen[-1]["input"]
        assert next(item for item in wire if item.get("id") == "rs_1") == payload["output"][0]
        native = next(item for item in wire if item.get("id") == "msg_1")
        assert "prompt_cache_breakpoint" not in native["content"][0]
        assert native["content"][1]["prompt_cache_breakpoint"] == {"mode": "explicit"}
        assert marked(seen[-1], api) == ["2", "block 4"]
        assert current[2]["_native_response"]["items"] == payload["output"]


async def test_real_http_body_transmission_persists_before_any_response(tmp_path):
    received, release, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def handle(reader, writer):
        try:
            headers = await reader.readuntil(b"\r\n\r\n")
            length = next(
                int(line.split(b":", 1)[1])
                for line in headers.lower().split(b"\r\n")
                if line.startswith(b"content-length:")
            )
            await reader.readexactly(length)
            received.set()
            await release.wait()  # No response headers or body are sent.
        finally:
            writer.close()
            await writer.wait_closed()
            closed.set()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    cfg = config("responses")
    cfg.base_url = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
    store, sid = Store(tmp_path), uuid4().hex
    context = OperationContexts(store, sid, "turn").new()
    provider = ModelTransport(
        cfg, cfg.api_key, request_context=context, http_client=httpx.AsyncClient(trust_env=False)
    )
    task = asyncio.create_task(provider.chat_complete(messages(0)))
    try:
        await asyncio.wait_for(received.wait(), 5)
        assert not task.done()
        with store.connection() as db:
            assert db.execute("SELECT count(*) FROM sent_cache_breakpoints").fetchone()[0] == 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        provider.request_context = OperationContexts(Store(tmp_path), sid, "retry").new()
        assert marked(provider._request(messages(2)), "responses") == ["block 0", "block 2"]
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        release.set()
        await provider.close()
        server.close()
        await server.wait_closed()
        if received.is_set():
            await asyncio.wait_for(closed.wait(), 5)


@pytest.mark.parametrize("api", ["responses", "chat_completions"])
async def test_durable_history_survives_restart_backup_and_fork_without_cross_session_leaks(tmp_path, api):
    store = Store(tmp_path / "original")
    card = store.import_card(b'{"name":"fixture","first_mes":"opening"}', "card.json")
    session = store.new_session(SessionCreate(card_id=card["id"]))
    sid, seen = session["id"], []
    context = OperationContexts(store, sid, "turn1").new()
    async with transport(api, seen, context=context) as p:
        await p.chat_complete(messages(0))
    backup(store, tmp_path / "backup.zip")
    store = restore(tmp_path / "backup.zip", tmp_path / "restored")
    context = OperationContexts(store, sid, "turn2").new()
    async with transport(api, seen, context=context) as p:
        await p.chat_complete(messages(2))
    assert marked(seen[-1], api) == ["block 0", "block 2"]
    other = store.new_session(SessionCreate(card_id=card["id"]))
    for context in (
        OperationContexts(store, other["id"], "turn").new(),
        OperationContexts(store, sid, "turn").new("tracker"),
    ):
        async with transport(api, seen, context=context) as p:
            await p.chat_complete(messages(4))
        assert marked(seen[-1], api) == ["block 4"]
    fork = store.fork_session(sid, session["head"], session["revision"])
    context = OperationContexts(store, fork["id"], "turn3").new()
    async with transport(api, seen, context=context) as p:
        await p.chat_complete(messages(4))
    assert marked(seen[-1], api) == ["block 0", "block 2", "block 4"]
    store.delete_session(fork["id"])
    with store.connection() as db:
        assert (
            db.execute("SELECT count(*) FROM sent_cache_breakpoints WHERE owner=?", (fork["id"],)).fetchone()[
                0
            ]
            == 0
        )
        assert (
            db.execute("SELECT count(*) FROM sent_cache_breakpoints WHERE owner=?", (sid,)).fetchone()[0] == 3
        )


@pytest.mark.parametrize("api", ["responses", "chat_completions"])
@pytest.mark.parametrize("mode", ["half_agent", "full_agent"])
async def test_story_requests_keep_old_breakpoints_across_committed_turns(tmp_path, monkeypatch, api, mode):
    from tavern.application.modes import ModeRuntime
    from tests.backend.test_full_agent import ScriptHost
    from tests.backend.test_half_agent_alignment import sse

    store = Store(tmp_path)
    document = store.settings_document()
    document.full_agent.tools.auto_retrieval = False
    document.half_agent.chunk_mode = "off"
    document.half_agent.memory_interval_turns = 100
    document.shared.providers[0].api_key = "fixture-secret"
    document.shared.providers[0].base_url = "https://fixture.test"
    document.shared.network.api_proxy = False
    for model in (document.full_agent.model, document.half_agent.foreground):
        model.api_mode, model.model = api, "gpt-6-sol"
    store.save_settings(document)
    card = store.import_card(
        b'{"name":"fixture","first_mes":"opening","description":"fixed rules"}', "card.json"
    )
    session = store.new_session(SessionCreate(card_id=card["id"], agent_mode=mode))
    session = store.set_cache_breakpoint(session["id"], session["revision"], True, None, 1, 1)
    service = ModeRuntime(store)
    service.full.runtime = ScriptHost()
    seen = []

    def route(request):
        body = json.loads(request.content)
        seen.append(body)
        payload = reply(api)
        if not body.get("stream"):
            return httpx.Response(200, json=payload)
        if api == "responses":
            return sse(api, payload)
        events = [
            {"choices": [{"delta": {"content": "OK"}, "finish_reason": None}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}], "usage": payload["usage"]},
        ]
        return httpx.Response(
            200,
            content="".join("data: " + json.dumps(e) + "\n\n" for e in events) + "data: [DONE]\n\n",
            headers={"content-type": "text/event-stream"},
        )

    async def factory(settings, capture=None, *, request_context=None):
        return ModelTransport(
            settings,
            settings.api_key,
            capture,
            request_context=request_context,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(route)),
        )

    monkeypatch.setattr("tavern.full_agent.agent.make_provider", factory)
    monkeypatch.setattr("tavern.scripting.agent.make_provider", factory)
    try:
        for _ in range(3):
            turn = service.submit(
                session["id"],
                TurnRequest(text="continue", operation_id=uuid4().hex, expected_revision=session["revision"]),
            )
            await service.tasks[turn["id"]]
            result = store.turn(turn["id"])
            assert result["status"] == "committed", result["error"]
            session = store.session(session["id"])
        assert [len(marked(body, api)) for body in seen] == [1, 2, 3]
        assert marked(seen[0], api)[0] == marked(seen[-1], api)[0]
    finally:
        await service.close()
