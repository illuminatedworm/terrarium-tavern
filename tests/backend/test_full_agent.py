"""Behavioral tests for mode isolation, real native tool wire, and branch recovery."""
import asyncio
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import httpx2 as sdk_httpx
import pytest
from httpx import ASGITransport, AsyncClient

from tavern.application.api import create_app
from tavern.application.modes import ModeRuntime
from tavern.core.models import SessionCreate, TurnRequest
from tavern.core.provider import make_provider
from tavern.full_agent.context import compile_window, load_trace
from tavern.full_agent.lore import RetrievalLane
from tavern.full_agent.state import note_view
from tavern.storage.store import Store


class Model:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []
        self.last_tool_calls = []
        self.closed = False

    async def chat_complete(self, messages, **kwargs):
        self.requests.append(copy.deepcopy({"messages": messages, **kwargs}))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        self.last_tool_calls = []
        if isinstance(response, tuple):
            name, args = response
            self.last_tool_calls = [SimpleNamespace(id=f"call{len(self.requests)}", name=name,
                                                    arguments=json.dumps(args, ensure_ascii=False))]
            return SimpleNamespace(content="工作过程不会混入正文", usage={}, finish_reason="tool_calls")
        return SimpleNamespace(content=response, usage={}, finish_reason="stop")

    async def close(self):
        self.closed = True


class Script:
    """Mechanical IO double; actual MVU/Chromium covered by the compatibility suite."""
    def __init__(self, history, snapshot):
        self.mirror = {"messages": copy.deepcopy(history), "state": copy.deepcopy(snapshot["state"]),
                       "scopes": copy.deepcopy(snapshot["scopes"])}

    async def call(self, method, *args):
        if method == "snapshot":
            return copy.deepcopy(self.mirror)
        if method == "transform":
            return args[0]
        if method == "update":
            self.mirror = copy.deepcopy(args[0])
            return None
        if method == "process":
            return {**self.mirror, "text": args[0]}
        raise AssertionError(method)

    async def close(self):
        pass


class ScriptHost:
    async def open(self, document, history, snapshot, *args, **kwargs):
        return Script(history, snapshot)

    async def close(self):
        pass


@pytest.fixture
def story(tmp_path):
    store = Store(tmp_path)
    card = store.import_card(Path("samples/雾港来信.json").read_bytes(), "card.json")
    settings = store.settings()
    settings.full_agent_config.auto_retrieval = False
    settings.retrieval.channels = ["keyword"]
    store.save_settings(settings)
    session = store.new_session(SessionCreate(card_id=card["id"], agent_mode="full_agent"))
    return store, session


def runtime(store):
    service = ModeRuntime(store)
    service.full.runtime = ScriptHost()
    async def forbidden(*args, **kwargs):
        raise AssertionError("Full Agent must not invoke Half Agent")
    service.half.run = forbidden
    service.half.tracker.prepare = forbidden
    service.half.tracker.maintain = forbidden
    return service


async def turn(service, session, model, **kwargs):
    service.full.provider_factory = lambda: model
    request = TurnRequest(text="我答应明天在钟楼见面。", operation_id=f"test-{session['revision']}",
                          expected_revision=session["revision"], **kwargs)
    result = service.submit(session["id"], request)
    await service.tasks[result["id"]]
    return service.store.turn(result["id"]), service.store.session(session["id"])


async def test_tools_checkpoint_rollover_recovery_and_no_tracker(story):
    store, session = story
    service = runtime(store)
    model = Model(
        ("read_history", {"ref": "H2"}),
        ("write_note", {"key": "约定/钟楼", "content": "玩家约定明天见面", "kind": "fact", "sources": ["H2"]}),
        ("new_context", {"content": "正在约定明天钟楼见面，保持温和叙事。", "keep_recent": 0}),
        ("search_history", {"query": "工作过程", "scope": "tools"}),
        "她点头应下，钟声从远处传来。",
    )
    try:
        result, current = await turn(service, session, model)
        assert result["status"] == "committed", result["error"]
        assert len(current["messages"]) == 3
        assert current["messages"][-1]["raw"] == "她点头应下，钟声从远处传来。"
        state = current["snapshot"]["full_agent"]
        assert state["window"]["number"] == 2 and state["window"]["start"] == 1
        assert state["notes"]["约定/钟楼"]["sources"][0]["message_id"] == current["messages"][1]["id"]
        after_rollover = model.requests[3]["messages"]
        assert not any(m.get("tool_calls") for m in after_rollover)
        assert not any(m["role"] == "tool" for m in after_rollover)
        assert any("正在约定明天钟楼" in m.get("content", "") for m in after_rollover)
        assert any(m["role"] == "user" and "我答应明天" in m["content"] for m in after_rollover)
        assert any(m["role"] == "tool" and "工作过程" in m["content"] for m in model.requests[-1]["messages"])
        with store.connection() as db:
            assert db.execute("SELECT count(*) FROM full_agent_trace").fetchone()[0] == 8
        follow = Model("第二天如约而至。")
        result2, _ = await turn(service, current, follow)
        assert result2["status"] == "committed", result2["error"]
        names = [c["function"]["name"] for m in follow.requests[0]["messages"] for c in m.get("tool_calls", [])]
        assert "write_note" not in names  # old-window tools are searchable, not replayed
        assert "search_history" in names  # current-window native tool exchange survives a turn
        assert model.closed and follow.closed
    finally:
        await service.close()


async def test_failure_and_cancellation_never_commit_draft_memory(story):
    store, session = story
    service = runtime(store)
    try:
        result, current = await turn(service, session, Model(
            ("write_checkpoint", {"content": "不应该提交的交接笔记"}), RuntimeError("model disconnected")))
        assert result["status"] == "error"
        assert current["revision"] == 0 and current["snapshot"] == session["snapshot"]
        with store.connection() as db:
            assert db.execute("SELECT count(*) FROM full_agent_trace").fetchone()[0] == 0
        waiting = asyncio.Event()
        class WaitingModel(Model):
            async def chat_complete(self, *args, **kwargs):
                waiting.set()
                await asyncio.Event().wait()
        service.full.provider_factory = lambda: WaitingModel()
        pending = service.submit(session["id"], TurnRequest(text="继续", operation_id="cancel", expected_revision=0))
        await waiting.wait()
        await service.cancel(pending["id"])
        assert store.turn(pending["id"])["status"] == "cancelled"
        assert store.session(session["id"])["snapshot"] == session["snapshot"]
    finally:
        await service.close()


async def test_fork_regeneration_source_edits_and_delete(story):
    store, session = story
    service = runtime(store)
    try:
        result, first = await turn(service, session, Model(
            ("read_history", {"ref": "H2"}),
            ("write_note", {"key": "见面", "content": "钟楼见面", "kind": "fact", "sources": ["H2"]}),
            ("write_checkpoint", {"content": "约定已达成"}), "第一版正文"))
        assert result["status"] == "committed", result["error"]
        fork = store.fork_session(first["id"], first["head"], first["revision"])
        assert fork["agent_mode"] == "full_agent"
        note = fork["snapshot"]["full_agent"]["notes"]["见面"]
        assert note["sources"][0]["message_id"] == fork["messages"][1]["id"]
        assert not note_view(note, fork["messages"])["stale"]
        assert len(load_trace(store, fork["id"], fork["messages"])) == 6
        result, regenerated = await turn(service, first, Model("第二版正文"), mode="regenerate")
        assert result["status"] == "committed", result["error"]
        assert regenerated["snapshot"]["full_agent"]["notes"] == {}
        assert load_trace(store, first["id"], regenerated["messages"]) == []
        switched = store.update_session(first["id"], regenerated["revision"], head=first["head"])
        edited = store.edit_message(first["id"], first["messages"][1]["id"], "我们取消约定。", switched["revision"])
        assert edited["snapshot"]["full_agent"]["checkpoint"]["stale"]
        assert note_view(edited["snapshot"]["full_agent"]["notes"]["见面"], edited["messages"])["stale"]
        await service.delete_session(first["id"])
        assert len(load_trace(store, fork["id"], fork["messages"])) == 6
        with store.connection() as db:
            assert db.execute("SELECT count(*) FROM full_agent_trace WHERE session=?", (first["id"],)).fetchone()[0] == 0
    finally:
        await service.close()


async def test_budget_step_limit_and_invalid_tools_roll_back(story):
    store, session = story
    service = runtime(store)
    config = store.settings()
    config.full_agent_config.max_steps = 2
    store.save_settings(config)
    try:
        model = Model(("patch_state", {"operations": [], "reason": "unauthorized"}),
                      ("new_context", {"content": "不能重置本回合调用额度"}))
        result, current = await turn(service, session, model)
        assert result["status"] == "error" and "上限" in result["error"]
        assert "不允许" in json.dumps(model.requests[1]["messages"], ensure_ascii=False)
        assert current["revision"] == 0
        assert current["snapshot"] == session["snapshot"]
    finally:
        await service.close()


async def test_retrieval_lane_agent_priority_and_no_overlap():
    started, release = asyncio.Event(), asyncio.Event()
    order, active = [], 0
    class Engine:
        async def prepare(self, value):
            nonlocal active
            active += 1
            assert active == 1
            order.append(value)
            if value == "auto1":
                started.set()
                await release.wait()
            active -= 1
            return value
    lane = RetrievalLane(Engine())
    first = asyncio.create_task(lane.prepare("auto1"))
    await started.wait()
    second = asyncio.create_task(lane.prepare("auto2"))
    explicit = asyncio.create_task(lane.prepare("agent", source="agent"))
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(first, second, explicit)
    assert order == ["auto1", "agent", "auto2"]


@pytest.mark.parametrize("api_mode", ["chat_completions", "responses"])
async def test_native_wire_and_atomic_checkpoint_on_invalid_arguments(story, monkeypatch, api_mode):
    for variable in ("ALL_PROXY", "HTTPS_PROXY", "HTTP_PROXY", "all_proxy", "https_proxy", "http_proxy"):
        monkeypatch.delenv(variable, raising=False)  # Only local MockTransport is used.
    store, session = story
    service = runtime(store)
    settings = store.settings().for_role("full_agent")
    settings.model, settings.api_key, settings.use_proxy = "test-model", "test-key", False
    settings.api_mode = api_mode
    provider = await make_provider(settings)
    calls = []
    def transport(request):
        payload = json.loads(request.content)
        calls.append(payload)
        if api_mode == "responses":
            if len(calls) == 1:
                output = [{"type": "function_call", "id": "fc_x", "call_id": "x", "name": "new_context",
                           "arguments": '{"content":"checkpoint", "keep_recent":99}', "status": "completed"}]
            else:
                output = [{"type": "message", "id": "msg_x", "role": "assistant", "status": "completed",
                           "content": [{"type": "output_text", "text": "正文", "annotations": []}]}]
            return sdk_httpx.Response(200, json={"id": "resp_x", "object": "response", "created_at": 1,
                "model": "test-model", "status": "completed", "output": output,
                "usage": {"input_tokens": 100, "output_tokens": 10, "total_tokens": 110}})
        if len(calls) == 1:
            message = {"role": "assistant", "content": None, "tool_calls": [{"id": "x", "type": "function",
                "function": {"name": "new_context", "arguments": '{"content":"checkpoint", "keep_recent":99}'}}]}
            finish = "tool_calls"
        else:
            message, finish = {"role": "assistant", "content": "正文"}, "stop"
        return sdk_httpx.Response(200, json={"id": "reply", "object": "chat.completion", "created": 1,
            "model": "test-model", "choices": [{"index": 0, "message": message, "finish_reason": finish}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110}})
    await provider._client._client.aclose()
    provider._client._client = sdk_httpx.AsyncClient(transport=sdk_httpx.MockTransport(transport), trust_env=False)
    try:
        result, current = await turn(service, session, provider)
        assert result["status"] == "committed", result["error"]
        assert calls[0]["tools"] and calls[0]["parallel_tool_calls"] is False
        if api_mode == "responses":
            assert any(m.get("call_id") == "x" and "error" in m.get("output", "") for m in calls[1]["input"])
        else:
            assert any(m.get("tool_call_id") == "x" and "error" in m["content"] for m in calls[1]["messages"])
        assert current["snapshot"]["full_agent"]["window"]["number"] == 1
        assert not current["snapshot"]["full_agent"]["checkpoint"]["text"]
    finally:
        await service.close()


async def test_full_api_preview_recovery_and_mode_boundaries(story):
    store, session = story
    app = create_app(store.root)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
            preview = await client.post(f"/api/sessions/{session['id']}/preview", json={"text": "继续"})
            assert preview.status_code == 200
            assert preview.json()["agent_mode"] == "full_agent"
            exported = await client.post(f"/api/sessions/{session['id']}/messages-preview", json={"text": "继续"})
            assert exported.status_code == 200
            assert exported.json() == preview.json()["messages"]
            assert {"role": "user", "content": "继续"} in exported.json()
            assert not any(x["source"] in ("memory", "retrieval_protocol") for x in preview.json()["trace"])
            result = await client.put(f"/api/sessions/{session['id']}/full-agent/context",
                json={"content": "人工交接", "new_window": True, "expected_revision": 0})
            assert result.status_code == 200
            assert result.json()["snapshot"]["full_agent"]["window"]["start"] == 1
            assert (await client.put(f"/api/sessions/{session['id']}/full-agent/context",
                json={"content": "过期请求", "expected_revision": 0})).status_code == 409
            assert (await client.get(f"/api/sessions/{session['id']}/memory/rebuild-estimate")).status_code == 400
            assert (await client.put(f"/api/sessions/{session['id']}/recipe", json={})).status_code == 400
            half = store.new_session(SessionCreate(card_id=session["card"]))
            assert half["agent_mode"] == "half_agent"
            assert (await client.get(f"/api/sessions/{half['id']}/full-agent")).status_code == 400
    finally:
        await app.state.service.close()


def test_notes_and_runtime_metadata_are_not_implicitly_in_context(story):
    store, session = story
    snapshot = copy.deepcopy(session["snapshot"])
    snapshot["full_agent"]["notes"]["secret-note"] = {"content": "只能通过工具读取的长笔记"}
    snapshot["state"] = {"initialized_openings": {"opaque-identity": "ledger-only"}}
    snapshot["memory_document"] = {"version": 1, "records": [{"content": "legacy-memory"}]}
    result = compile_window(store.session_card(session["id"])["document"], session, session["messages"],
                            snapshot, "继续", store.settings().for_role("full_agent"), [])
    wire = json.dumps(result["messages"], ensure_ascii=False)
    for hidden in ("只能通过工具读取", "ledger-only", "opaque-identity", "legacy-memory", session["id"], session["head"]):
        assert hidden not in wire


async def test_real_index_auto_and_explicit_query_share_injection_lifecycle(tmp_path):
    store = Store(tmp_path)
    document = {"name": "向导", "first_mes": "钟楼的门开了。", "description": "你是向导。",
                "character_book": {"entries": [
                    {"id": "tower", "keys": ["钟楼"], "content": "钟楼每晚九点关门。", "comment": "钟楼", "enabled": True},
                    {"id": "port", "keys": ["旧港"], "content": "旧港的船只明晨出发。", "comment": "旧港", "enabled": True},
                ]}}
    card = store.import_card(json.dumps(document, ensure_ascii=False).encode(), "card.json")
    settings = store.settings()
    settings.retrieval.channels = ["keyword"]
    store.save_settings(settings)
    session = store.new_session(SessionCreate(card_id=card["id"], agent_mode="full_agent"))
    service = runtime(store)
    try:
        model = Model(("search_lore", {"query": "旧港"}), ("search_lore", {"query": "钟楼"}), "向导指向旧港。")
        result, current = await turn(service, session, model)
        assert result["status"] == "committed", result["error"]
        requests = [r["messages"] for r in model.requests]
        assert sum(m.get("content") == "钟楼每晚九点关门。" for m in requests[0]) == 1
        assert sum(m.get("content") == "旧港的船只明晨出发。" for m in requests[1]) == 1
        assert sum(m.get("content") == "钟楼每晚九点关门。" for m in requests[2]) == 1
        records = current["snapshot"]["lore_injections"]
        assert len(records) == 2 and {r["anchor_id"] for r in records} == {session["head"]}
        retrievals = [e["payload"] for e in store.events(result["id"]) if e["kind"] == "retrieval"]
        assert [r["source"] for r in retrievals] == ["auto", "agent", "agent"]
        assert retrievals[1]["query"] == "旧港"
    finally:
        await service.close()


async def test_real_card_runtime_uses_agent_mvu_tool_and_preview_review(tmp_path):
    store = Store(tmp_path)
    card = store.import_card(Path("samples/星航状态卡.json").read_bytes(), "card.json")
    with store.connection() as db:
        db.execute("UPDATE cards SET permission=1 WHERE id=?", (card["id"],))
    settings = store.settings()
    settings.full_agent_config.auto_retrieval = False
    store.save_settings(settings)
    session = store.new_session(SessionCreate(card_id=card["id"], agent_mode="full_agent", policy="preview"))
    service = ModeRuntime(store)
    def forbidden():
        raise AssertionError("No background model in Full Agent")
    service.memory_provider_factory = service.variable_provider_factory = forbidden
    model = Model(("read_state", {}),
                  ("patch_state", {"operations": [{"op": "replace", "path": "/能量", "value": 4}], "reason": "本轮变化"}),
                  ("write_checkpoint", {"content": "目前能量为4"}), "洛伊带你来到观测窗。")
    try:
        session = await service.initialize_session(session["id"])
        result, pending = await turn(service, session, model, update_variables=True)
        assert result["status"] == "awaiting_review", result["error"]
        assert pending["head"] == session["head"]
        service.review(result["id"], True)
        current = store.session(session["id"])
        assert current["snapshot"]["state"]["stat_data"]["能量"] == 4
        assert "能量" in json.dumps(model.requests[1]["messages"], ensure_ascii=False)
        assert "initialized_openings" not in json.dumps(model.requests[1]["messages"], ensure_ascii=False)
        assert len(current["messages"]) == 3
    finally:
        await service.close()


async def test_long_history_is_pageable_and_fact_sources_must_be_read(story):
    from tavern.full_agent.agent import FullAgent
    from tavern.full_agent.tools import Read
    store, session = story
    settings = store.settings().for_role("full_agent")
    settings.full_agent_config.tool_result_tokens = 256
    user = {"id": "pending-input", "role": "user", "raw": "当前输入"}
    req = TurnRequest(text="当前输入", operation_id="paging", expected_revision=0)
    tid, _ = store.create_turn(session["id"], req)
    owner = FullAgent(store, tid, store.session_card(session["id"])["document"], session, session["messages"],
        copy.deepcopy(session["snapshot"]), req, None, settings, user, None)
    text = "精确原文不可丢失。" * 300
    parts, offset = [], 0
    while True:
        page = owner.tools.page(text, Read(ref="H1", offset=offset), ref="H1")
        assert page["text"]
        parts.append(page["text"])
        if page["next_offset"] is None:
            break
        offset = page["next_offset"]
    assert "".join(parts) == text
    with pytest.raises(ValueError, match="已搜索或读取"):
        await owner.tools.execute("write_note", json.dumps({"key": "事实", "content": "未读", "kind": "fact", "sources": ["H1"]}))
    assert owner.state["notes"] == {}


async def test_empty_story_can_continue_without_half_agent_state(story):
    store, session = story
    session = store.update_session(session["id"], 0, head=None)
    service = runtime(store)
    try:
        result, current = await turn(service, session, Model("全新的故事开始。"))
        assert result["status"] == "committed", result["error"]
        assert len(current["messages"]) == 2
        assert current["messages"][0]["parent"] is None
        assert "memory_document" not in current["snapshot"]
    finally:
        await service.close()


def test_tool_references_stay_stable_when_documents_are_reordered(story):
    from tavern.full_agent.agent import FullAgent
    store, session = story
    document = {"name": "角色", "description": "literal @D1", "character_book": {"entries": [
        {"id": "a" * 32, "keys": ["一"], "content": "一"},
        {"id": "b" * 32, "keys": ["二"], "content": "二"}]}}
    req = TurnRequest(text="当前输入", operation_id="refs", expected_revision=0)
    tid, _ = store.create_turn(session["id"], req)
    snapshot = copy.deepcopy(session["snapshot"])
    def agent(doc):
        return FullAgent(store, tid, doc, session, session["messages"], snapshot, req, None,
                         store.settings().for_role("full_agent"), {"id": "input", "role": "user", "raw": "输入"}, None)
    first = agent(document)
    handle = first.model_view.forward["a" * 32]
    assert handle != "@D1"
    assert first.lore_refs()["L1"] == "a" * 32
    changed = copy.deepcopy(document)
    changed["character_book"]["entries"].reverse()
    second = agent(changed)
    assert second.model_view.restore(handle) == "a" * 32
    assert second.lore_refs()["L1"] == "a" * 32
