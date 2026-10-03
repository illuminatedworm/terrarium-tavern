"""Manual retries discard failed exchanges at the persisted and native boundaries."""
import copy
import json
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from tavern.core.models import TurnRequest
from tavern.full_agent.activity import turn_items
from tavern.full_agent.context import load_trace
from tavern.full_agent.tools import AgentTools
from tavern.storage.store import Store
from tests.backend.test_full_agent import Model, runtime, tool_exchanges, turn
from tests.backend.test_full_agent_alignment import game, transport


def native_reply(api, calls=(), text="", marker=""):
    if api == "chat_completions":
        return {"model": "fixture", "choices": [{"finish_reason": "tool_calls" if calls else "stop",
            "message": {"content": text, "reasoning_content": marker,
                        "tool_calls": [{"id": cid, "type": "function", "function": {
                            "name": name, "arguments": json.dumps(args)}} for cid, name, args in calls]}}]}
    if api == "gemini":
        parts = ([{"text": marker, "thought": True}] if marker else [])
        parts += [{"functionCall": {"id": cid, "name": name, "args": args}}
                  for cid, name, args in calls]
        if text:
            parts.append({"text": text})
        return {"candidates": [{"content": {"role": "model", "parts": parts}, "finishReason": "STOP"}]}
    if api == "anthropic":
        content = ([{"type": "thinking", "thinking": marker, "signature": marker}] if marker else [])
        content += [{"type": "tool_use", "id": cid, "name": name, "input": args}
                    for cid, name, args in calls]
        if text:
            content.append({"type": "text", "text": text})
        return {"id": "msg_fixture", "model": "fixture", "content": content,
                "stop_reason": "tool_use" if calls else "end_turn", "usage": {}}
    output = ([{"type": "reasoning", "id": "rs_" + marker, "summary": [],
                "encrypted_content": marker}] if marker else [])
    output += [{"type": "function_call", "id": "fc_" + cid, "call_id": cid,
                "name": name, "arguments": json.dumps(args)} for cid, name, args in calls]
    if text:
        output.append({"type": "message", "id": "msg_fixture", "role": "assistant",
                       "content": [{"type": "output_text", "text": text, "annotations": []}]})
    return {"id": "resp_fixture", "model": "fixture", "output": output,
            "status": "completed", "usage": {}}


@pytest.mark.parametrize("api", ["chat_completions", "responses", "anthropic", "gemini"])
async def test_retry_filters_mixed_failures_from_native_wire_activity_and_committed_trace(tmp_path, monkeypatch, api):
    import tavern.full_agent.agent as agent_module

    store, service, session = game(tmp_path, api)
    if api == "gemini":
        settings = store.settings_document()
        settings.full_agent.model.model = "gemini-3-pro-preview"
        store.save_settings(settings)
    requests = []

    def route(request):
        requests.append(request)
        if len(requests) == 3:
            raise httpx.ReadTimeout("synthetic interruption", request=request)
        if len(requests) == 1:
            payload = native_reply(api, [
                ("bad", "read_history", {"reason": "BAD_ARGUMENT"}),
                ("accepted", "write_handoff", {"content": "MEMORY_ACCEPTED"}),
            ], marker="MIXED_NATIVE")
        elif len(requests) == 2:
            payload = native_reply(api, [("read", "read_state", {})], marker="DEPENDENT_NATIVE")
        else:
            payload = native_reply(api, text="完成正文")
        return httpx.Response(200, json=payload, headers={"x-codex-turn-state": "source-route"})

    async def factory(settings, capture=None, *, request_context=None):
        return transport(settings, route, request_context, capture)

    monkeypatch.setattr(agent_module, "make_provider", factory)
    try:
        failed = service.submit(session["id"], TurnRequest(text="继续", operation_id=uuid4().hex,
                                                           expected_revision=0))
        await service.tasks[failed["id"]]
        failed = store.turn(failed["id"])
        assert failed["status"] == "error", failed["error"]
        saved = store.full_agent_progress(failed["id"])
        assert "BAD_ARGUMENT" in requests[1].content.decode()  # same-turn correction still sees the error
        assert saved["snapshot"]["full_agent"]["handoff"]["revision"] == 1
        assert any(i["status"] == "error" for i in turn_items(store, session["id"], failed["id"]))
    finally:
        await service.close()

    store = Store(store.root)
    service = runtime(store)
    try:
        resumed = service.submit(session["id"], TurnRequest(text="继续", operation_id=uuid4().hex,
            expected_revision=0, resume_turn_id=failed["id"]))
        copied = store.full_agent_progress(resumed["id"])
        assert "BAD_ARGUMENT" not in json.dumps(copied["continuation"])
        assert "BAD_ARGUMENT" not in json.dumps(copied["new_trace"])
        assert store.full_agent_progress(failed["id"]) == saved
        await service.tasks[resumed["id"]]
        result = store.turn(resumed["id"])
        assert result["status"] == "committed", result["error"]
        assert len(requests) == 4
        body = requests[3].content.decode()
        assert "BAD_ARGUMENT" not in body and '"bad"' not in body
        assert "MIXED_NATIVE" not in body and "DEPENDENT_NATIVE" not in body
        assert "MEMORY_ACCEPTED" in body
        if api == "responses":
            assert requests[2].headers["x-codex-turn-state"] == "source-route"
            assert "x-codex-turn-state" not in requests[3].headers
        items = [i for i in turn_items(store, session["id"], resumed["id"]) if i["kind"] == "tool"]
        assert [(i["name"], i["status"]) for i in items] == [
            ("write_handoff", "returned"), ("read_state", "returned")]
        current = store.session(session["id"])
        memory = current["snapshot"]["full_agent"]["handoff"]
        assert memory["text"] == "MEMORY_ACCEPTED" and memory["revision"] == 1
        trace = tool_exchanges(load_trace(store, session["id"], current["messages"]))
        assert len(trace) == 4 and "BAD_ARGUMENT" not in json.dumps(trace)
        assert any(i["status"] == "error" for i in turn_items(store, session["id"], failed["id"]))
        following = service.submit(session["id"], TurnRequest(text="继续", operation_id=uuid4().hex,
                                                              expected_revision=current["revision"]))
        await service.tasks[following["id"]]
        assert store.turn(following["id"])["status"] == "committed"
        assert "BAD_ARGUMENT" not in requests[4].content.decode()
    finally:
        await service.close()


async def test_retry_of_only_failures_starts_without_tools_and_survives_another_failure(tmp_path):
    store, service, session = game(tmp_path)
    try:
        failed, _ = await turn(service, session, Model(
            ("write_handoff", {"content": "FAILED_PROPOSAL", "reason": "extra"}),
            ("read_history", {"reason": "missing ref"}), RuntimeError("disconnected")))
        saved = store.full_agent_progress(failed["id"])
        retry_model = Model(RuntimeError("second interruption"))
        service.full.provider_factory = lambda: retry_model
        retry = service.submit(session["id"], TurnRequest(text=failed["request"]["text"],
            operation_id=uuid4().hex, expected_revision=0, resume_turn_id=failed["id"]))
        await service.tasks[retry["id"]]
        assert store.turn(retry["id"])["status"] == "error"
        assert not tool_exchanges(store.full_agent_progress(retry["id"])["new_trace"])
        assert not any(m.get("tool_calls") or m["role"] == "tool" for m in retry_model.requests[0]["messages"])
        assert "FAILED_PROPOSAL" not in json.dumps(retry_model.requests)
        assert store.full_agent_progress(failed["id"]) == saved
        final_model = Model("新的正文")
        service.full.provider_factory = lambda: final_model
        final = service.submit(session["id"], TurnRequest(text=failed["request"]["text"],
            operation_id=uuid4().hex, expected_revision=0, resume_turn_id=retry["id"]))
        await service.tasks[final["id"]]
        assert store.turn(final["id"])["status"] == "committed"
        assert not any(m.get("tool_calls") or m["role"] == "tool" for m in final_model.requests[0]["messages"])
    finally:
        await service.close()


async def test_retry_keeps_unfinished_call_but_drops_its_failed_batch_sibling(tmp_path, monkeypatch):
    store, service, session = game(tmp_path)

    class Batch(Model):
        async def chat_complete(self, messages, **kwargs):
            self.requests.append(copy.deepcopy(messages))
            self.last_tool_calls = [SimpleNamespace(id=cid, name=name, arguments=json.dumps(args))
                for cid, name, args in [
                    ("bad", "read_history", {"reason": "missing ref"}),
                    ("accepted", "write_handoff", {"content": "MEMORY_A"}),
                    ("unfinished", "write_handoff", {"content": "MEMORY_B"}),
                ]]
            return SimpleNamespace(content="", usage={}, finish_reason="stop")

    original = AgentTools.execute
    interrupted, executed = False, []

    async def execute(tools, name, arguments):
        nonlocal interrupted
        content = json.loads(arguments).get("content")
        if content == "MEMORY_B" and not interrupted:
            interrupted = True
            raise RuntimeError("interrupted before execution")
        if content:
            executed.append(content)
        return await original(tools, name, arguments)

    monkeypatch.setattr(AgentTools, "execute", execute)
    try:
        failed, _ = await turn(service, session, Batch())
        assert failed["status"] == "error"
        assert executed == ["MEMORY_A"]
        model = Model("续接正文")
        service.full.provider_factory = lambda: model
        resumed = service.submit(session["id"], TurnRequest(text=failed["request"]["text"],
            operation_id=uuid4().hex, expected_revision=0, resume_turn_id=failed["id"]))
        await service.tasks[resumed["id"]]
        result = store.turn(resumed["id"])
        assert result["status"] == "committed", result["error"]
        assert executed == ["MEMORY_A", "MEMORY_B"]
        assert len(model.requests) == 1
        sent = model.requests[0]["messages"]
        assert [c["id"] for m in sent for c in m.get("tool_calls", [])] == ["accepted", "unfinished"]
        assert [m["tool_call_id"] for m in sent if m["role"] == "tool"] == ["accepted", "unfinished"]
        assert store.session(session["id"])["snapshot"]["full_agent"]["handoff"]["text"] == "MEMORY_B"
    finally:
        await service.close()
