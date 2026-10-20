import copy
import json
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
from google.genai import types as genai_types
from pydantic import ValidationError

from tavern.conversation.cache_breakpoint import model_messages, prepare_context
from tavern.core.cache_policy import CacheSettings, endpoint, normalize_usage
from tavern.core.models import SessionCreate, Settings
from tavern.core.settings import SettingsDocument
from tavern.scripting.cloud_cache import DISABLED, LOCKS, OBJECTS
from tavern.scripting.model_stream import StreamState
from tavern.scripting.model_transport import ModelTransport
from tavern.scripting.protocols import native_messages
from tavern.storage.store import Store


@pytest.fixture(autouse=True)
def clear_caches():
    DISABLED.clear()
    OBJECTS.clear()
    LOCKS.clear()
    yield
    DISABLED.clear()
    OBJECTS.clear()
    LOCKS.clear()


def config(api="chat_completions", **kwargs):
    return Settings(
        api_key="test-key",
        model={"anthropic": "claude-fixture", "gemini": "gemini-3.8-flash"}.get(api, "gpt-fixture"),
        api_mode=api,
        base_url="https://example.test/proxy/v1",
        use_proxy=False,
        cache=CacheSettings(mode="runtime"),
        **kwargs,
    )


@asynccontextmanager
async def provider(cfg, route, capture=None):
    p = ModelTransport(
        cfg, cfg.api_key, capture, http_client=httpx.AsyncClient(transport=httpx.MockTransport(route))
    )
    try:
        yield p
    finally:
        await p.close()


def reply(api):
    if api == "responses":
        return {
            "status": "completed",
            "output": [{"type": "message", "content": [{"type": "output_text", "text": "OK"}]}],
            "usage": {"input_tokens": 10, "output_tokens": 1, "input_tokens_details": {"cached_tokens": 8}},
        }
    if api == "anthropic":
        return {
            "content": [{"type": "text", "text": "OK"}],
            "stop_reason": "end_turn",
            "usage": {
                "input_tokens": 2,
                "cache_read_input_tokens": 8,
                "cache_creation_input_tokens": 0,
                "output_tokens": 1,
            },
        }
    if api == "gemini":
        return {
            "candidates": [{"content": {"parts": [{"text": "OK"}]}, "finishReason": "STOP"}],
            "usageMetadata": {
                "promptTokenCount": 10,
                "cachedContentTokenCount": 8,
                "candidatesTokenCount": 1,
            },
        }
    return {
        "choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": 10,
            "completion_tokens": 1,
            "prompt_tokens_details": {"cached_tokens": 8, "cached_creation_tokens": 2},
        },
    }


@pytest.mark.parametrize(
    "api,field", [("chat_completions", "prompt_tokens_details"), ("responses", "input_tokens_details")]
)
def test_creation_alias_keeps_zero_and_skips_null(api, field):
    assert (
        normalize_usage({field: {"cache_write_tokens": None, "cache_creation_tokens": 72}}, api)[
            "cache_write_tokens"
        ]
        == 72
    )
    assert (
        normalize_usage({field: {"cache_write_tokens": 0, "cache_creation_tokens": 72}}, api)[
            "cache_write_tokens"
        ]
        == 0
    )
    assert normalize_usage({field: {"cache_creation_tokens": 0}}, api)["cache_write_tokens"] == 0


def compiled(mode, cfg):
    history = [{"id": "greeting", "parent": None, "role": "assistant", "raw": "Opening"}]
    for index in range(4):
        history.extend(
            [
                {"id": f"u{index}", "parent": history[-1]["id"], "role": "user", "raw": f"Question {index}"},
                {"id": f"a{index}", "parent": f"u{index}", "role": "assistant", "raw": f"Answer {index}"},
            ]
        )
    session = {
        "id": "branch",
        "agent_mode": mode,
        "cache_breakpoint_auto": True,
        "recipe": {"blocks": [{"source": "history", "enabled": True}]},
    }
    trace = [{"role": "system", "content": "stable ledger data " * 3000, "source": "character"}]
    trace.extend(
        {"role": m["role"], "content": m["raw"], "source": "history", "message_id": m["id"]} for m in history
    )
    trace.extend(
        [
            {"role": "user", "content": "latest", "source": "input"},
            {"role": "system", "content": "dynamic handoff", "source": "handoff"},
        ]
    )
    context = {"messages": [{k: m[k] for k in ("role", "content")} for m in trace], "trace": trace}
    snapshot = {"state": {}, "full_agent": {"window": {"start": 0, "id": "w1"}}}
    return prepare_context(context, session, history, snapshot, cfg), session, history, snapshot


@pytest.mark.parametrize("api", ["chat_completions", "responses", "anthropic", "gemini"])
@pytest.mark.parametrize("mode", ["full_agent", "half_agent"])
async def test_second_latest_reply_boundary_reaches_final_wire_for_all_modes(api, mode):
    cfg = config(api)
    context, *_ = compiled(mode, cfg)
    assert context["cache"]["message_id"] == "a2"
    messages = model_messages(context)
    assert all(not any(k.startswith("_cache") for k in m) for m in context["messages"])
    seen, captured = [], []

    def route(request):
        body = json.loads(request.content)
        seen.append((request.url.path, body))
        if request.url.path.endswith("cachedContents"):
            assert body["contents"][-1]["parts"][0]["text"] == "Answer 2"
            return httpx.Response(200, json={"name": "cachedContents/test"})
        return httpx.Response(200, json=reply(api))

    async with provider(cfg, route, captured.append) as p:
        result = await p.chat_complete(messages)
    assert result.content == "OK" and result.usage["cached_tokens"] == 8
    assert captured[-1]["cache"]["status"] == "sent"
    wire = seen[-1][1]
    if api == "gemini":
        assert wire["cachedContent"] == "cachedContents/test"
        assert wire["contents"][0]["parts"][0]["text"] == "Question 3"
        assert "systemInstruction" not in wire
    elif api == "anthropic":
        marked = [b for m in wire["messages"] for b in m["content"] if "cache_control" in b]
        assert len(marked) == 1 and marked[0]["text"] == "Answer 2"
        assert "dynamic handoff" in wire["messages"][-1]["content"][0]["text"]
        assert "dynamic handoff" not in str(wire["system"])
    else:
        inputs = wire["input" if api == "responses" else "messages"]
        marked = [
            b
            for m in inputs
            if isinstance(m.get("content"), list)
            for b in m["content"]
            if "prompt_cache_breakpoint" in b
        ]
        assert len(marked) == 1 and marked[0]["text"] == "Answer 2"
    assert all("test-key" not in json.dumps(item) for item in captured)


@pytest.mark.parametrize("api", ["chat_completions", "responses", "anthropic", "gemini"])
async def test_cloud_mode_never_injects_runtime_controls(api):
    cfg = config(api)
    cfg.cache = CacheSettings(mode="cloud", request={"prompt_cache_key": "ignored"})
    context, *_ = compiled("full_agent", cfg)
    seen = []

    def route(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=reply(api))

    async with provider(cfg, route) as p:
        await p.chat_complete(model_messages(context))
    assert len(seen) == 1
    assert not any(
        token in json.dumps(seen)
        for token in (
            "cache_control",
            "prompt_cache_breakpoint",
            "prompt_cache_key",
            "cachedContent",
            "_cache_breakpoint",
        )
    )
    assert seen[0].get("prompt_cache_retention") == (
        "in_memory" if api in ("responses", "chat_completions") else None
    )


async def test_explicit_rejection_falls_back_once_and_remembers_capability():
    cfg = config()
    context, *_ = compiled("full_agent", cfg)
    seen, events = [], []

    def route(request):
        body = json.loads(request.content)
        seen.append(body)
        if len(seen) == 1:
            return httpx.Response(
                400, json={"error": {"message": "prompt_cache_breakpoint is not supported"}}
            )
        assert "prompt_cache" not in json.dumps(body)
        return httpx.Response(200, json=reply("chat_completions"))

    for _ in range(2):
        async with provider(cfg, route, events.append) as p:
            await p.chat_complete(model_messages(context))
    assert len(seen) == 3 and len(seen[0]["messages"]) == len(seen[1]["messages"])
    assert events[-1]["cache"]["status"] == "fallback"


@pytest.mark.parametrize("failure", ["timeout", "server", "other400"])
async def test_never_retries_ambiguous_generation_failures(failure):
    cfg = config()
    context, *_ = compiled("full_agent", cfg)
    seen = []

    def route(request):
        seen.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("timeout", request=request)
        return httpx.Response(
            500 if failure == "server" else 400, json={"error": {"message": "temperature invalid"}}
        )

    async with provider(cfg, route) as p:
        with pytest.raises(httpx.HTTPError):
            await p.chat_complete(model_messages(context))
    assert len(seen) == 1


async def test_gemini_reuse_rollover_expiry_and_complete_fallback():
    cfg = config("gemini")
    context, *_ = compiled("full_agent", cfg)
    messages = model_messages(context)
    created, deleted, generated = [], [], []
    expire = False

    def route(request):
        if request.method == "DELETE":
            deleted.append(request.url.path)
            return httpx.Response(200, json={})
        body = json.loads(request.content)
        if request.url.path.endswith("cachedContents"):
            created.append(body)
            return httpx.Response(200, json={"name": f"cachedContents/{len(created)}"})
        generated.append(body)
        if expire and body.get("cachedContent"):
            return httpx.Response(404, json={"error": {"message": "cachedContent not found"}})
        return httpx.Response(200, json=reply("gemini"))

    async with provider(cfg, route) as p:
        await p.chat_complete(messages)
        await p.chat_complete(messages)
        assert len(created) == 1
        changed = copy.deepcopy(messages)
        next(m for m in changed if m.get("_cache_breakpoint"))["_cache_window"] = "w2"
        await p.chat_complete(changed)
        assert len(created) == 2 and len(deleted) == 1
        expire = True
        await p.chat_complete(changed)
    assert "cachedContent" not in generated[-1]
    assert "stable ledger data" in str(generated[-1]["systemInstruction"])
    assert "Question 0" in str(generated[-1]["contents"])
    assert len(deleted) == 2


@pytest.mark.parametrize(
    "api,model,retention,expected",
    [
        ("anthropic", "claude-opus-5-5", "1h", {"type": "ephemeral", "ttl": "1h"}),
        ("chat_completions", "claude-opus-5-5", "1h", {"type": "ephemeral", "ttl": "1h"}),
        ("anthropic", "claude-opus-5-5", "default", {"type": "ephemeral"}),
        ("chat_completions", "gpt-5.4", "24h", "24h"),
        ("responses", "gpt-5.4", "default", "in_memory"),
        ("responses", "gpt-5.5", "default", "24h"),
        ("responses", "gpt-6-sol", "default", "30m"),
        ("chat_completions", "gpt-5.6-sol", "default", "30m"),
    ],
)
async def test_retention_reaches_final_http_request(api, model, retention, expected):
    cfg = config(api)
    cfg.model, cfg.cache.retention = model, retention
    cfg.cache.request = {"prompt_cache_key": "preserved"}
    seen = []

    def route(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=reply(api))

    messages = [
        {"role": "system", "content": "fixed", "_cache_breakpoint": True},
        {"role": "user", "content": "suffix"},
    ]
    async with provider(cfg, route) as p:
        await p.chat_complete(messages)
    body = seen[0]
    assert body["prompt_cache_key"] == "preserved"
    if "claude" in model:
        marker = body["system"][-1] if api == "anthropic" else body["messages"][0]["content"][0]
        assert marker["cache_control"] == expected
    elif expected == "30m":
        assert body["prompt_cache_options"] == {"mode": "explicit", "ttl": "30m"}
        assert "prompt_cache_retention" not in body
    else:
        assert body["prompt_cache_retention"] == expected
        assert "prompt_cache_breakpoint" not in json.dumps(body)
        assert "prompt_cache_options" not in body


@pytest.mark.parametrize("api", ["responses", "chat_completions"])
async def test_cloud_retention_without_explicit_breakpoints(api):
    cfg = config(api)
    cfg.model = "gpt-6-sol"
    cfg.cache = CacheSettings(mode="cloud")
    seen = []

    def route(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=reply(api))

    async with provider(cfg, route) as p:
        await p.chat_complete([{"role": "user", "content": "complete content"}])
    assert seen[0]["prompt_cache_options"] == {"ttl": "30m"}
    assert "prompt_cache_breakpoint" not in json.dumps(seen)


async def test_native_claude_cloud_uses_automatic_retention_without_local_breakpoint():
    cfg = config("anthropic")
    cfg.cache = CacheSettings(mode="cloud", retention="1h")
    seen = []

    def route(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=reply("anthropic"))

    async with provider(cfg, route) as p:
        await p.chat_complete(
            [
                {"role": "system", "content": "complete fixed rules"},
                {"role": "user", "content": "complete user text"},
            ]
        )
    assert seen[0].pop("cache_control") == {"type": "ephemeral", "ttl": "1h"}
    assert "cache_control" not in json.dumps(seen[0])


@pytest.mark.parametrize("api", ["gemini", "chat_completions"])
async def test_gemini_retention_replaces_old_object_and_honors_server_expiry(api):
    import time
    from datetime import datetime, timezone

    cfg = config(api)
    cfg.model = "gemini-3.8-flash"
    context, *_ = compiled("full_agent", cfg)
    created, deleted = [], []

    def route(request):
        if request.method == "DELETE":
            deleted.append(request.url.path)
            return httpx.Response(200, json={})
        body = json.loads(request.content)
        if request.url.path.endswith("cachedContents"):
            created.append(body)
            return httpx.Response(
                200,
                json={
                    "name": f"cachedContents/{len(created)}",
                    "expireTime": datetime.fromtimestamp(time.time() + 1800, timezone.utc).isoformat(),
                },
            )
        return httpx.Response(200, json=reply(api))

    async with provider(cfg, route) as p:
        await p.chat_complete(model_messages(context))
        await p.chat_complete(model_messages(context))
    assert len(created) == 1 and created[0]["ttl"] == "300s"
    cfg.cache.retention = "1h"
    async with provider(cfg, route) as p:
        await p.chat_complete(model_messages(context))
        await p.chat_complete(model_messages(context))
    assert len(created) == 2 and created[-1]["ttl"] == "3600s"
    assert len(deleted) == 1 and deleted[0].endswith("cachedContents/1")
    assert len(OBJECTS) == 1
    remaining = next(iter(OBJECTS.values()))["expires"] - time.time()
    assert 1700 < remaining <= 1800


async def test_gemini_create_failure_uses_complete_context():
    cfg = config("gemini")
    context, *_ = compiled("full_agent", cfg)
    seen, events = [], []

    def route(request):
        body = json.loads(request.content)
        seen.append(body)
        if request.url.path.endswith("cachedContents"):
            return httpx.Response(404, text="missing endpoint")
        assert "cachedContent" not in body
        assert "Answer 0" in str(body)
        return httpx.Response(200, json=reply("gemini"))

    async with provider(cfg, route, events.append) as p:
        await p.chat_complete(model_messages(context))
    assert len(seen) == 2 and events[-1]["cache"]["status"] == "fallback"


def test_native_tool_signatures_survive_replay():
    cfg = config("gemini")
    p = ModelTransport(cfg, "test")
    raw_parts = [
        {
            "functionCall": {"name": "lookup", "args": {"q": "hello"}, "id": "native-id"},
            "thoughtSignature": "opaque-signature",
        }
    ]
    p._finish({"candidates": [{"content": {"parts": raw_parts}, "finishReason": "STOP"}]})
    call = p.last_tool_calls[0]
    messages = [
        {
            "role": "assistant",
            "content": "",
            **p.last_assistant_extra_fields,
            "tool_calls": [{"id": call.id, "function": {"name": call.name, "arguments": call.arguments}}],
        },
        {"role": "tool", "tool_call_id": call.id, "content": '{"found":true}'},
    ]
    _, wire, _ = native_messages(messages, "gemini")
    assert wire[0]["parts"] == raw_parts
    assert wire[1]["parts"][0]["functionResponse"] == {
        "name": "lookup",
        "id": "native-id",
        "response": {"found": True},
    }


@pytest.mark.parametrize("api", ["chat_completions", "responses", "anthropic", "gemini"])
def test_streams_require_completion_and_preserve_usage(api):
    state = StreamState(api)
    with pytest.raises(ValueError):
        state.finish()
    if api == "responses":
        assert state.feed({"type": "response.output_text.delta", "delta": "OK"}) == "OK"
        state.feed({"type": "response.completed", "response": reply(api)})
    elif api == "chat_completions":
        assert state.feed({"choices": [{"delta": {"content": "OK"}, "finish_reason": "stop"}]}) == "OK"
        state.feed({"usage": reply(api)["usage"]})
    elif api == "anthropic":
        state.feed({"type": "message_start", "message": {"usage": {"input_tokens": 2}}})
        state.feed({"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}})
        assert state.feed({"type": "content_block_delta", "index": 0, "delta": {"text": "OK"}}) == "OK"
        state.feed(
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 1}}
        )
        state.feed({"type": "message_stop"})
    else:
        assert state.feed(reply(api)) == "OK"
    assert state.finish()


def test_settings_migration_backs_up_old_settings_once(tmp_path):
    store = Store(tmp_path)
    raw = store.settings_document().model_dump()
    for scope, key in (
        ("full_agent", "model"),
        ("half_agent", "foreground"),
        ("half_agent", "background"),
        ("shared", "card_author"),
    ):
        raw[scope][key].pop("cache")
        raw[scope][key].update(
            prompt_cache_mode="no_write", prompt_cache_key="retained-key", prompt_cache_retention="24h"
        )
    with store.connection() as db:
        db.execute("UPDATE meta SET value=? WHERE key='settings'", (json.dumps(raw),))
    migrated = Store(tmp_path).settings_document()
    assert migrated.full_agent.model.cache.mode == migrated.half_agent.foreground.cache.mode == "runtime"
    assert migrated.half_agent.background.cache.mode == migrated.shared.card_author.cache.mode == "cloud"
    assert migrated.full_agent.model.cache.request == {
        "prompt_cache_key": "retained-key",
        "prompt_cache_retention": "24h",
    }
    backups = list(tmp_path.glob("before-upgrade-*.sqlite3"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as db:
        before = json.loads(db.execute("SELECT value FROM meta WHERE key='settings'").fetchone()[0])
    assert before == raw
    Store(tmp_path)
    assert len(list(tmp_path.glob("before-upgrade-*.sqlite3"))) == 1


def test_custom_fields_validation_urls_and_usage():
    for key in ("messages", "model", "tools", "max_tokens", "headers", "output_config"):
        with pytest.raises(ValidationError):
            CacheSettings(request={key: "override"})
    with pytest.raises(ValidationError):
        CacheSettings(field="text")
    assert endpoint("https://host.test", "anthropic") == "https://host.test/v1/messages"
    assert endpoint("https://host.test/x/v1", "responses") == "https://host.test/x/v1/responses"
    assert (
        endpoint("https://host.test/x/v1beta", "gemini", "gemini-3.8-flash-high")
        == "https://host.test/x/v1beta/models/gemini-3.8-flash-high:generateContent"
    )
    assert normalize_usage({}, "responses")["cached_tokens"] is None
    assert (
        normalize_usage({"prompt_tokens_details": {"cached_creation_tokens": 19}}, "chat_completions")[
            "cache_write_tokens"
        ]
        == 19
    )
    assert (
        normalize_usage(
            {"promptTokenCount": 100, "thoughtsTokenCount": 7, "candidatesTokenCount": 3}, "gemini"
        )["completion_tokens"]
        == 10
    )


def test_partial_json_profiles_keep_story_defaults_without_forcing_background():
    settings = SettingsDocument(
        full_agent={"model": {"model": "new-full"}},
        half_agent={"foreground": {"model": "new-writer"}, "background": {"model": "new-background"}},
    )
    assert settings.full_agent.model.cache.mode == settings.half_agent.foreground.cache.mode == "runtime"
    assert settings.half_agent.background.cache.mode == settings.shared.card_author.cache.mode == "cloud"


@pytest.mark.parametrize("mode", ["full_agent", "half_agent"])
def test_branch_local_manual_and_regenerate_boundaries(mode):
    cfg = config()
    context, session, history, snapshot = compiled(mode, cfg)
    prepare_context(context, session, history[:-2], snapshot, cfg)
    assert context["cache"]["message_id"] == "a1"
    session.update(cache_breakpoint_auto=False, cache_breakpoint_message="a2")
    prepare_context(context, session, history, snapshot, cfg)
    assert context["cache"]["message_id"] == "a2"
    session["cache_breakpoint_message"] = "other-branch"
    prepare_context(context, session, history, snapshot, cfg)
    assert context["cache"]["source"] == "character"
    if mode == "full_agent":
        session["cache_breakpoint_message"] = "a1"
        snapshot["full_agent"]["window"]["start"] = 7
        prepare_context(context, session, history, snapshot, cfg)
        assert context["cache"]["message_id"] is None


@pytest.mark.parametrize("api", ["chat_completions", "responses", "anthropic", "gemini"])
@pytest.mark.parametrize("mode", ["full_agent", "half_agent"])
async def test_held_follow_anchor_keeps_final_wire_prefix_stable(api, mode):
    cfg = config(api)
    context, session, history, snapshot = compiled(mode, cfg)
    session["cache_breakpoint_follow"] = {"interval": 3, "offset": 2, "anchor": "a0", "progress": 1}
    captured, bodies, creations = [], [], []

    def route(request):
        body = json.loads(request.content)
        if request.url.path.endswith("cachedContents"):
            creations.append(body)
            assert body["contents"][-1]["parts"][0]["text"] == "Answer 0"
            return httpx.Response(200, json={"name": "cachedContents/held"})
        bodies.append(body)
        return httpx.Response(200, json=reply(api))

    async with provider(cfg, route, captured.append) as transport:
        for tail in ("first tail", "changed tail"):
            context["messages"][-2]["content"] = tail
            prepare_context(context, session, history, snapshot, cfg)
            assert context["cache"]["message_id"] == "a0"
            await transport.chat_complete(model_messages(context))
    assert captured[0]["cache"]["prefix_sha256"] == captured[1]["cache"]["prefix_sha256"]
    if api == "gemini":
        assert len(creations) == 1
        assert all(body["cachedContent"] == "cachedContents/held" for body in bodies)
        assert all(body["contents"][0]["parts"][0]["text"] == "Question 1" for body in bodies)
    else:
        field = "cache_control" if api == "anthropic" else "prompt_cache_breakpoint"
        for body in bodies:
            marked = [
                part
                for message in body.get("input", body.get("messages", []))
                if isinstance(message.get("content"), list)
                for part in message["content"]
                if field in part
            ]
            assert len(marked) == 1 and marked[0]["text"] == "Answer 0"


async def test_gemini_compatible_reference_contains_only_suffix_and_no_dynamic_system():
    cfg = config()
    cfg.model = "gemini-3.8-flash"
    context, *_ = compiled("half_agent", cfg)

    def route(request):
        body = json.loads(request.content)
        if request.url.path.endswith("cachedContents"):
            return httpx.Response(200, json={"name": "cachedContents/test"})
        assert body["extra_body"]["google"]["cached_content"] == "cachedContents/test"
        assert body["messages"][0]["content"] == "Question 3"
        assert all(m["role"] != "system" for m in body["messages"])
        assert "tavern_runtime_context" in body["messages"][-1]["content"]
        return httpx.Response(200, json=reply("chat_completions"))

    async with provider(cfg, route) as p:
        await p.chat_complete(model_messages(context))


async def test_undersized_gemini_prefix_does_not_disable_larger_future_prefix():
    cfg = config("gemini")
    seen = []

    def route(request):
        seen.append(request.url.path)
        if request.url.path.endswith("cachedContents"):
            return httpx.Response(200, json={"name": "cachedContents/large"})
        return httpx.Response(200, json=reply("gemini"))

    async with provider(cfg, route) as p:
        await p.chat_complete(
            [
                {"role": "system", "content": "small", "_cache_breakpoint": True},
                {"role": "user", "content": "hello"},
            ]
        )
        assert len(seen) == 1 and p.capability_key not in DISABLED
        context, *_ = compiled("full_agent", cfg)
        await p.chat_complete(model_messages(context))
    assert len(seen) == 3


async def test_gemini_never_caches_system_content_beyond_selected_boundary():
    cfg = config("gemini")
    seen, events = [], []

    def route(request):
        seen.append(json.loads(request.content))
        assert not request.url.path.endswith("cachedContents")
        return httpx.Response(200, json=reply("gemini"))

    messages = [
        {"role": "system", "content": "stable text " * 5000, "_cache_breakpoint": True},
        {"role": "system", "content": "later fixed character data"},
        {"role": "user", "content": "input"},
    ]
    async with provider(cfg, route, events.append) as p:
        await p.chat_complete(messages)
    assert len(seen) == 1 and "later fixed character data" in str(seen[0])
    assert events[-1]["cache"]["status"] == "fallback"


@pytest.mark.parametrize("model", ["claude-opus-5-5", "claude-opus-4-6"])
async def test_claude_requested_rollover_tool_preserves_permission_boundary(model):
    cfg = config("anthropic")
    cfg.model = model
    tools = [
        {"type": "function", "function": {"name": name, "parameters": {"type": "object"}}}
        for name in ("read_history", "new_context")
    ]

    def route(request):
        body = json.loads(request.content)
        if model == "claude-opus-5-5":
            assert body["tool_choice"]["type"] == "auto"
            assert [t["name"] for t in body["tools"]] == ["new_context"]
        else:
            assert body["tool_choice"]["type"] == "tool"
            assert body["tool_choice"]["name"] == "new_context"
            assert len(body["tools"]) == 2
        return httpx.Response(200, json=reply("anthropic"))

    async with provider(cfg, route) as p:
        await p.chat_complete(
            [{"role": "user", "content": "save handoff"}],
            extra_body={
                "tools": tools,
                "tool_choice": {"type": "function", "function": {"name": "new_context"}},
            },
        )


@pytest.mark.parametrize(
    "model,effort,expected,adaptive",
    [
        ("claude-opus-4-6", "high", "high", True),
        ("claude-opus-4-5", "high", "high", False),
    ],
)
async def test_anthropic_reasoning_protocol(model, effort, expected, adaptive):
    cfg = config("anthropic", reasoning_effort=effort, reply_tokens=8192)
    cfg.model = model

    def route(request):
        body = json.loads(request.content)
        assert body["output_config"] == {"effort": expected}
        assert "budget_tokens" not in json.dumps(body)
        if adaptive:
            assert body["thinking"] == {"type": "adaptive"}
        else:
            assert "thinking" not in body
        return httpx.Response(200, json=reply("anthropic"))

    async with provider(cfg, route) as p:
        await p.chat_complete([{"role": "user", "content": "hello"}])


@pytest.mark.parametrize("model", ["claude-sonnet-4-5"])
async def test_legacy_claude_without_effort_requires_default_before_sending(model):
    cfg = config("anthropic", reasoning_effort="high")
    cfg.model = model

    def route(request):
        pytest.fail("unsupported legacy effort must not make a paid request")

    async with provider(cfg, route) as p:
        with pytest.raises(ValueError, match="不支持文字思考档位"):
            await p.chat_complete([{"role": "user", "content": "hello"}])


@pytest.mark.parametrize("model,expected", [("claude-opus-4-6", {"type": "disabled"})])
async def test_claude_none_explicitly_disables_thinking_where_supported(model, expected):
    cfg = config("anthropic", reasoning_effort="none")
    cfg.model = model

    def route(request):
        body = json.loads(request.content)
        assert body["thinking"] == expected
        assert "output_config" not in body
        return httpx.Response(200, json=reply("anthropic"))

    async with provider(cfg, route) as p:
        await p.chat_complete([{"role": "user", "content": "hello"}])


@pytest.mark.parametrize(
    "api,style,effort",
    [
        ("chat_completions", "reasoning_effort", "high"),
        ("chat_completions", "reasoning", "medium"),
        ("responses", "reasoning_effort", "high"),
        ("responses", "reasoning_effort", "default"),
    ],
)
async def test_openai_protocols_keep_selected_text_effort(api, style, effort):
    cfg = config(api, reasoning_effort=effort, reasoning_format=style)

    def route(request):
        body = json.loads(request.content)
        assert "thinking" not in body and "output_config" not in body
        if effort == "default":
            assert "reasoning" not in body and "reasoning_effort" not in body
        elif api == "responses" or style == "reasoning":
            assert body["reasoning"] == {"effort": effort}
            assert "reasoning_effort" not in body
        else:
            assert body["reasoning_effort"] == effort
            assert "reasoning" not in body
        return httpx.Response(200, json=reply(api))

    async with provider(cfg, route) as p:
        await p.chat_complete([{"role": "user", "content": "hello"}])


@pytest.mark.parametrize(
    "model,effort,thinking",
    [
        ("gemini-3.8-flash", "high", {"thinkingLevel": "HIGH"}),
        ("gemini-2.5-flash", "none", {"thinkingBudget": 0}),
    ],
)
async def test_gemini_native_uses_model_supported_thinking_control(model, effort, thinking):
    cfg = config("gemini", reasoning_effort=effort)
    cfg.model = model

    def route(request):
        body = json.loads(request.content)
        # Google's SDK may use protobuf snake_case aliases inside nested fields.
        actual = genai_types.ThinkingConfig.model_validate(body["generationConfig"]["thinkingConfig"])
        assert actual.model_dump(by_alias=True, exclude_none=True) == {**thinking, "includeThoughts": True}
        return httpx.Response(200, json=reply("gemini"))

    async with provider(cfg, route) as p:
        await p.chat_complete([{"role": "user", "content": "hello"}])


@pytest.mark.parametrize("api,model", [("gemini", "gemini-3.8-flash")])
async def test_gemini_rejects_unsupported_thinking_off_before_sending(api, model):
    cfg = config(api, reasoning_effort="none")
    cfg.model = model

    def route(request):
        pytest.fail("unsupported thinking mode must not make a paid request")

    async with provider(cfg, route) as p:
        with pytest.raises(ValueError, match="不支持关闭思考"):
            await p.chat_complete([{"role": "user", "content": "hello"}])


@pytest.mark.parametrize("marked_result", [None, 0, 1])
def test_anthropic_parallel_results_keep_batches_content_and_cache_markers(marked_result):
    messages = [{"role": "system", "content": "fixed"}]
    for batch in range(2):
        ids = [f"batch-{batch}-{i}" for i in range(2)]
        messages.append(
            {
                "role": "assistant",
                "content": "inspect",
                "tool_calls": [
                    {"id": id_, "function": {"name": "read_history", "arguments": '{"ref":"H2"}'}}
                    for id_ in ids
                ],
            }
        )
        for i, id_ in enumerate(ids):
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": id_,
                    "content": f"result-{id_}",
                    "_tool_error": i == 0,
                    "_cache_breakpoint": batch == 0 and i == marked_result,
                }
            )
        messages.append(
            {"role": "system", "content": f"chronological context {batch}", "_cache_static": False}
        )
    original = copy.deepcopy(messages)
    marker = {"field": "cache_control", "value": {"type": "ephemeral"}}
    _, body, boundary = native_messages(messages, "anthropic", marker)
    assert messages == original
    assert len(body) == 6
    for batch in range(2):
        assistant, results, context = body[batch * 3 : batch * 3 + 3]
        calls = [b["id"] for b in assistant["content"] if b["type"] == "tool_use"]
        assert results["role"] == "user"
        assert [b["tool_use_id"] for b in results["content"]] == calls
        assert [b["content"] for b in results["content"]] == [f"result-{id_}" for id_ in calls]
        assert results["content"][0]["is_error"] is True
        assert "is_error" not in results["content"][1]
        assert f"chronological context {batch}" in context["content"][0]["text"]
    assert boundary == (None if marked_result is None else 2)
    marked = [b for m in body for b in m["content"] if "cache_control" in b]
    assert len(marked) == (0 if marked_result is None else 1)
    if marked:
        assert marked[0]["tool_use_id"] == f"batch-0-{marked_result}"


@pytest.mark.parametrize("api", ["anthropic", "gemini"])
@pytest.mark.parametrize("call_count", [1, 2])
async def test_full_runtime_persists_native_signature_and_returns_tool_result(tmp_path, api, call_count):
    from tests.backend.test_full_agent import runtime, turn

    store = Store(tmp_path)
    card = store.import_card(Path("samples/雾港来信.json").read_bytes(), "card.json")
    settings = store.settings()
    settings.full_agent.api_mode = api
    settings.providers[0].api_key = "test-key"
    settings.full_agent.model = "claude-fixture" if api == "anthropic" else "gemini-3.8-flash"
    settings.full_agent_config.auto_retrieval = False
    store.save_settings(settings)
    session = store.new_session(SessionCreate(card_id=card["id"], agent_mode="full_agent"))
    seen = []

    def route(request):
        if request.url.path.endswith("cachedContents"):
            return httpx.Response(404)
        body = json.loads(request.content)
        seen.append(body)
        if len(seen) == 1:
            if api == "anthropic":
                return httpx.Response(
                    200,
                    json={
                        "stop_reason": "tool_use",
                        "content": [
                            {"type": "thinking", "thinking": "inspect", "signature": "opaque-signature"},
                            *[
                                {
                                    "type": "tool_use",
                                    "id": f"native-id-{i}",
                                    "name": "read_history",
                                    "input": {"ref": "H2"},
                                }
                                for i in range(call_count)
                            ],
                        ],
                    },
                )
            return httpx.Response(
                200,
                json={
                    "candidates": [
                        {
                            "finishReason": "STOP",
                            "content": {
                                "parts": [
                                    {
                                        "functionCall": {
                                            "id": f"native-id-{i}",
                                            "name": "read_history",
                                            "args": {"ref": "H2"},
                                        },
                                        "thoughtSignature": "opaque-signature",
                                    }
                                    for i in range(call_count)
                                ]
                            },
                        }
                    ]
                },
            )
        assert "opaque-signature" in json.dumps(body)
        assert (
            "tool_result" in json.dumps(body)
            if api == "anthropic"
            else "functionResponse" in json.dumps(body)
        )
        if api == "anthropic":
            messages = body["messages"]
            at = next(i for i, m in enumerate(messages) if any(b["type"] == "tool_use" for b in m["content"]))
            calls = [b["id"] for b in messages[at]["content"] if b["type"] == "tool_use"]
            results = messages[at + 1]
            assert results["role"] == "user"
            assert [b["tool_use_id"] for b in results["content"]] == calls
        return httpx.Response(200, json=reply(api))

    service = runtime(store)
    try:
        async with provider(store.settings().for_role("full_agent"), route) as model:
            result, _ = await turn(service, session, model)
        assert result["status"] == "committed", result["error"]
        assert len(seen) == 2
        with store.connection() as db:
            payload = " ".join(row[0] for row in db.execute("SELECT payload FROM full_agent_trace"))
        assert "opaque-signature" in payload
        async with provider(store.settings().for_role("full_agent"), route) as model:
            result, _ = await turn(service, store.session(session["id"]), model)
        assert result["status"] == "committed", result["error"]
        assert len(seen) == 3
    finally:
        await service.close()
