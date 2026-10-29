"""Behavioral tests for mode isolation, real native tool wire, and branch recovery."""

import asyncio
import copy
import json
import re
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
from tavern.full_agent.handoff import render_text
from tavern.full_agent.lore import RetrievalLane
from tavern.full_agent.state import note_view
from tavern.full_agent.tools import RESULT_FIELDS, AgentTools, result_field
from tavern.storage.store import Store

LAST = "H_LAST"  # Model resolves it to the last committed H from the request's status line.


def handoff(text, *, through=LAST):
    """Chaptered handoff arguments: a chapter through ``through`` (None: none) and one current point."""
    chapters = (
        [
            {
                "op": "add",
                "through": through,
                "title": "此前经过",
                "points": [{"text": text, "refs": [through]}],
            }
        ]
        if through
        else []
    )
    return {"chapters": chapters, "current": [{"text": text, "refs": ["H1"]}]}


def current_text(state):
    """Joined current-handoff point texts of a Full Agent state."""
    return "\n".join(p["text"] for p in state["handoff"]["current"])


def written(history, text, refs=("H1",)):
    """A stored handoff whose current part is ``text`` citing ``refs`` (empty text: empty handoff)."""
    from tavern.full_agent.handoff import HandoffWrite, apply_write

    empty = {"chapters": [], "current": [], "revision": 0}
    if not text:
        return empty
    return apply_write(
        empty, HandoffWrite.model_validate({"current": [{"text": text, "refs": list(refs)}]}), history
    )


def prepared(request_text, text):
    """Summarize the requested batch without computing or repeating H references."""
    chapters = (
        [{"op": "add", "title": "此前经过", "points": [{"text": text}]}]
        if re.search(r"覆盖到 H\d+", request_text)
        else []
    )
    return {"chapters": chapters, "current": [{"text": text}]}


def last_committed(messages):
    text = "\n".join(str(m.get("content", "")) for m in messages)
    found = re.findall(r"原始历史 H1(?:-H(\d+))?", text)
    return f"H{found[-1] or 1}" if found else "H1"


class Model:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []
        self.last_tool_calls = []
        self.last_completion = None
        self.closed = False

    async def chat_complete(self, messages, **kwargs):
        from tavern.scripting.native_response import ResponseCompletion

        self.requests.append(copy.deepcopy({"messages": messages, **kwargs}))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        self.last_tool_calls = []
        if isinstance(response, dict):
            self.last_tool_calls = [
                SimpleNamespace(id=f"call{len(self.requests)}-{i}", name=name, arguments=json.dumps(args))
                for i, (name, args) in enumerate(response.get("calls", []))
            ]
            finish = response.get("finish_reason", "tool_calls" if self.last_tool_calls else "stop")
            self.last_completion = ResponseCompletion("end" if finish == "stop" else "tools", finish)
            return SimpleNamespace(content=response.get("content", ""), usage={}, finish_reason=finish)
        if isinstance(response, tuple):
            name, args = response
            arguments = json.dumps(args, ensure_ascii=False).replace(LAST, last_committed(messages))
            self.last_tool_calls = [
                SimpleNamespace(id=f"call{len(self.requests)}", name=name, arguments=arguments)
            ]
            self.last_completion = ResponseCompletion("tools", "tool_calls")
            return SimpleNamespace(content="", usage={}, finish_reason="tool_calls")
        self.last_completion = ResponseCompletion("end", "stop")
        return SimpleNamespace(content=response, usage={}, finish_reason="stop")

    async def close(self):
        self.closed = True


def native_reply(api, calls=(), text="", marker=""):
    if api == "chat_completions":
        return {
            "model": "fixture",
            "choices": [
                {
                    "finish_reason": "tool_calls" if calls else "stop",
                    "message": {
                        "content": text,
                        "reasoning_content": marker,
                        "tool_calls": [
                            {
                                "id": cid,
                                "type": "function",
                                "function": {"name": name, "arguments": json.dumps(args)},
                            }
                            for cid, name, args in calls
                        ],
                    },
                }
            ],
        }
    if api == "gemini":
        parts = [{"text": marker, "thought": True}] if marker else []
        parts += [{"functionCall": {"id": cid, "name": name, "args": args}} for cid, name, args in calls]
        if text:
            parts.append({"text": text})
        return {"candidates": [{"content": {"role": "model", "parts": parts}, "finishReason": "STOP"}]}
    if api == "anthropic":
        content = [{"type": "thinking", "thinking": marker, "signature": marker}] if marker else []
        content += [{"type": "tool_use", "id": cid, "name": name, "input": args} for cid, name, args in calls]
        if text:
            content.append({"type": "text", "text": text})
        return {
            "id": "msg_fixture",
            "model": "fixture",
            "content": content,
            "stop_reason": "tool_use" if calls else "end_turn",
            "usage": {},
        }
    output = (
        [{"type": "reasoning", "id": "rs_" + marker, "summary": [], "encrypted_content": marker}]
        if marker
        else []
    )
    output += [
        {
            "type": "function_call",
            "id": "fc_" + cid,
            "call_id": cid,
            "name": name,
            "arguments": json.dumps(args),
        }
        for cid, name, args in calls
    ]
    if text:
        output.append(
            {
                "type": "message",
                "id": "msg_fixture",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text, "annotations": []}],
            }
        )
    return {"id": "resp_fixture", "model": "fixture", "output": output, "status": "completed", "usage": {}}


def story_reply(api, text="正文", *, calls=()):
    return native_reply(api, calls, text=text)


def tool_exchanges(trace):
    # The opt-in experimental text transport is not a business operation.
    protocol_ids = {
        call["id"]
        for item in trace
        for call in item["message"].get("tool_calls", [])
        if call["function"]["name"] == "kemini_emit_story"
    }
    return [
        item
        for item in trace
        if any(call["id"] not in protocol_ids for call in item["message"].get("tool_calls", []))
        or (item["message"]["role"] == "tool" and item["message"].get("tool_call_id") not in protocol_ids)
    ]


def test_tool_result_field_options(story):
    store, _ = story
    owner = SimpleNamespace(settings=store.settings())
    tools = AgentTools(owner)
    rows = [{"ref": "H1", "role": "user", "text": "旧港的船只出发"}]
    assert tools.search(rows, "旧港", 5, include_preview=False)["matches"] == [{"ref": "H1", "role": "user"}]
    assert tools.search(rows, "旧港", 5)["matches"][0]["preview"] == "旧港的船只出发"
    assert result_field({}, "recall_lore", "include_ref") is False
    assert result_field({}, "recall_lore", "include_title") is False
    assert result_field({}, "search_history", "include_preview") is True
    assert result_field({}, "read_history", "include_metadata") is True
    assert (
        result_field(
            {"tool_result_options": {"recall_lore": {"include_ref": True}}}, "recall_lore", "include_ref"
        )
        is True
    )
    assert set(RESULT_FIELDS["recall_lore"]) == {"include_ref", "include_title"}


class Script:
    """Mechanical IO double; actual MVU/Chromium covered by the compatibility suite."""

    def __init__(self, history, snapshot):
        self.mirror = {
            "messages": copy.deepcopy(history),
            "state": copy.deepcopy(snapshot["state"]),
            "scopes": copy.deepcopy(snapshot["scopes"]),
        }

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
    service.full.provider_factory = (lambda: model) if model is not None else None
    request = TurnRequest(
        text="我答应明天在钟楼见面。",
        operation_id=f"test-{session['revision']}",
        expected_revision=session["revision"],
        **kwargs,
    )
    result = service.submit(session["id"], request)
    await service.tasks[result["id"]]
    return service.store.turn(result["id"]), service.store.session(session["id"])


async def test_tools_handoff_rollover_recovery_and_no_tracker(story):
    store, session = story
    service = runtime(store)
    model = Model(
        ("read_history", {"ref": "H2"}),
        (
            "write_note",
            {"key": "约定/钟楼", "content": "玩家约定明天见面", "kind": "fact", "sources": ["H2"]},
        ),
        ("new_context", {"handoff": handoff("正在约定明天钟楼见面，保持温和叙事。"), "keep_recent": 0}),
        ("search_history", {"query": "我答应明天"}),
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
        assert any(m["role"] == "tool" and "H2" in m["content"] for m in model.requests[-1]["messages"])
        for request in model.requests:
            assert request["messages"][0]["content"] == store.settings().full_agent_system_prompt
            assert request["messages"][1]["content"].startswith("# Full Agent 工具协议")
            assert request["messages"][2]["role"] == "system"
            assert request["messages"][2]["content"].startswith("本次上下文窗口上限为")
            assert (
                sum(m.get("content", "").startswith("本次上下文窗口上限为") for m in request["messages"]) == 1
            )
        with store.connection() as db:
            assert (
                db.execute(
                    "SELECT count(*) FROM full_agent_trace WHERE json_extract(payload,'$.role')='tool'"
                ).fetchone()[0]
                == 4  # Four maintenance results; ordinary prose is not a tool.
            )
        follow = Model("第二天如约而至。")
        result2, _ = await turn(service, current, follow)
        assert result2["status"] == "committed", result2["error"]
        names = [
            c["function"]["name"] for m in follow.requests[0]["messages"] for c in m.get("tool_calls", [])
        ]
        assert "write_note" not in names  # old-window tools are retained in the trace, not replayed
        assert "search_history" in names  # current-window native tool exchange survives a turn
        assert model.closed and follow.closed
    finally:
        await service.close()


async def test_failure_and_cancellation_never_commit_draft_memory(story):
    store, session = story
    service = runtime(store)
    try:
        result, current = await turn(
            service,
            session,
            Model(
                ("write_handoff", handoff("不应该提交的交接笔记", through=None)),
                RuntimeError("model disconnected"),
            ),
        )
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
        pending = service.submit(
            session["id"], TurnRequest(text="继续", operation_id="cancel", expected_revision=0)
        )
        await waiting.wait()
        await service.cancel(pending["id"])
        assert store.turn(pending["id"])["status"] == "cancelled"
        assert store.session(session["id"])["snapshot"] == session["snapshot"]
    finally:
        await service.close()


async def test_mvu_tool_schema_stays_stable_and_runtime_rejects_unrequested_call(story):
    store, session = story
    service = runtime(store)
    operations = [{"op": "add", "path": "/energy", "value": 2}]
    try:
        model = Model(("patch_state", {"operations": operations, "comment": "未获授权"}), "正文")
        result, current = await turn(service, session, model, update_variables=False)
        assert result["status"] == "committed", result["error"]
        assert current["snapshot"]["state"] == session["snapshot"]["state"]
        names = {tool["function"]["name"] for tool in model.requests[0]["extra_body"]["tools"]}
        assert "patch_state" in names
        result_text = next(m["content"] for m in model.requests[1]["messages"] if m["role"] == "tool")
        assert "本轮未要求更新" in result_text
        assert not any(
            m["content"].startswith("本轮须至少成功调用一次 patch_state")
            for m in model.requests[0]["messages"]
            if isinstance(m.get("content"), str)
        )

        required = Model(
            {"content": "先交付的正文", "calls": [("read_state", {})]},
            ("patch_state", {"operations": operations, "comment": "能量变化"}),
            "",
        )
        result, current = await turn(service, current, required, update_variables=True)
        assert result["status"] == "committed", result["error"]
        assert len(required.requests) == 3
        assert {tool["function"]["name"] for tool in required.requests[0]["extra_body"]["tools"]} == names
        messages = required.requests[0]["messages"]
        input_index = max(
            i for i, m in enumerate(messages) if m["role"] == "user" and "我答应明天" in m["content"]
        )
        assert any(
            m["content"].startswith("本轮须至少成功调用一次 patch_state") for m in messages[input_index + 1 :]
        )
        assert current["messages"][-1]["raw"] == "先交付的正文"

        repeated = Model(
            ("read_state", {}),
            ("patch_state", {"operations": operations, "comment": "首次更新"}),
            ("patch_state", {"operations": operations, "comment": "重复更新"}),
            "重复后的正文",
        )
        result, _ = await turn(service, current, repeated, update_variables=True)
        assert result["status"] == "committed", result["error"]
        returns = [
            json.loads(m["content"]) for m in repeated.requests[-1]["messages"] if m["role"] == "tool"
        ][-2:]
        assert returns[0]["data"]["saved_to_draft"] is True
        assert returns[1]["data"]["saved_to_draft"] is True
    finally:
        await service.close()


async def test_failed_model_request_resumes_complete_tool_exchanges_after_reopen(story):
    store, session = story
    service = runtime(store)
    try:
        failed, unchanged = await turn(
            service,
            session,
            Model(
                ("read_history", {"ref": "H2"}),
                ("write_note", {"key": "钟楼", "content": "明天赴约", "kind": "fact", "sources": ["H2"]}),
                RuntimeError("model disconnected"),
            ),
        )
        assert failed["status"] == "error"
        assert unchanged["revision"] == 0
        progress = store.full_agent_progress(failed["id"])
        assert [item["message"]["role"] for item in tool_exchanges(progress["new_trace"])] == [
            "assistant",
            "tool",
            "assistant",
            "tool",
        ]
        assert progress["snapshot"]["full_agent"]["notes"]["钟楼"]["content"] == "明天赴约"
    finally:
        await service.close()

    reopened = Store(store.root)
    service = runtime(reopened)
    try:
        model = Model("她在钟楼下答应了。")
        service.full.provider_factory = lambda: model
        resumed = service.submit(
            session["id"],
            TurnRequest(
                text=failed["request"]["text"],
                operation_id="resume-1",
                expected_revision=0,
                resume_turn_id=failed["id"],
            ),
        )
        await service.tasks[resumed["id"]]
        assert reopened.turn(resumed["id"])["status"] == "committed"
        assert len(model.requests) == 1
        sent = model.requests[0]["messages"]
        exchanges = [m for m in sent if m["role"] == "tool" or m.get("tool_calls")]
        assert [m["role"] for m in exchanges] == ["assistant", "tool", "assistant", "tool"]
        recorded = tool_exchanges(progress["new_trace"])
        assert exchanges[0]["tool_calls"][0] == recorded[0]["message"]["tool_calls"][0]
        assert exchanges[1]["content"] == recorded[1]["message"]["content"]
        assert exchanges[3]["content"] == recorded[3]["message"]["content"]
        current = reopened.session(session["id"])
        assert current["snapshot"]["full_agent"]["notes"]["钟楼"]["content"] == "明天赴约"
        assert len(tool_exchanges(load_trace(reopened, session["id"], current["messages"]))) == 4
        from tavern.full_agent.activity import turn_items

        assert [
            item["name"]
            for item in turn_items(reopened, session["id"], resumed["id"])
            if item["kind"] == "tool"
        ] == ["read_history", "write_note"]
    finally:
        await service.close()


async def test_failed_request_after_rollover_resumes_new_window_without_replaying_tool(story):
    store, session = story
    service = runtime(store)
    current_point = {"text": "钟楼约定来自本轮输入", "refs": ["H2", "H1-H2"]}
    proposal = {
        "chapters": [{"op": "add", "title": "开场", "points": [{"text": "开场剧情", "refs": ["H1"]}]}],
        "current": [current_point],
    }
    try:
        failed, unchanged = await turn(
            service,
            session,
            Model(
                ("write_handoff", {"current": [current_point]}),
                ("new_context", {"handoff": proposal, "keep_recent": 0}),
                RuntimeError("model disconnected"),
            ),
        )
        assert failed["status"] == "error" and "model disconnected" in failed["error"]
        assert unchanged["snapshot"] == session["snapshot"]
        progress = store.full_agent_progress(failed["id"])
        assert [
            json.loads(item["message"]["content"])["ok"]
            for item in progress["new_trace"]
            if item["message"]["role"] == "tool"
        ] == [True, True]
        input_id = progress["user"]["id"]
        saved = progress["snapshot"]["full_agent"]
        assert saved["window"]["start"] == 1
        assert saved["handoff"]["chapters"][0]["end"] == session["messages"][0]["id"]
        assert saved["handoff"]["current"][0]["refs"] == [
            {"start": input_id, "end": input_id},
            {"start": session["messages"][0]["id"], "end": input_id},
        ]
        model = Model("第二天，她如约而至。")
        service.full.provider_factory = lambda: model
        resumed = service.submit(
            session["id"],
            TurnRequest(
                text=failed["request"]["text"],
                operation_id="resume-rollover",
                expected_revision=0,
                resume_turn_id=failed["id"],
            ),
        )
        await service.tasks[resumed["id"]]
        assert store.turn(resumed["id"])["status"] == "committed"
        sent = model.requests[0]["messages"]
        assert not any(m.get("tool_calls") or m["role"] == "tool" for m in sent)
        rendered_point = "钟楼约定来自本轮输入 [H2, H1-H2]"
        assert any(rendered_point in m.get("content", "") for m in sent)
        assert any(m["role"] == "user" and m["content"].startswith("[H2]\n") for m in sent)
        current = store.session(session["id"])
        assert current["messages"][1]["id"] == input_id
        assert current["snapshot"]["full_agent"]["window"]["number"] == 2
        assert current["snapshot"]["full_agent"]["handoff"] == saved["handoff"]
        assert render_text(saved["handoff"], current["messages"]).endswith(rendered_point)
        assert len(tool_exchanges(load_trace(store, session["id"], current["messages"]))) == 4
        fork = store.fork_session(current["id"], current["head"], current["revision"])
        assert fork["snapshot"]["full_agent"]["handoff"]["current"][0]["refs"] == [
            {"start": fork["messages"][1]["id"], "end": fork["messages"][1]["id"]},
            {"start": fork["messages"][0]["id"], "end": fork["messages"][1]["id"]},
        ]
    finally:
        await service.close()


async def test_repeated_failure_keeps_prior_tool_results_and_fresh_input_starts_over(story):
    store, session = story
    service = runtime(store)
    try:
        failed, _ = await turn(
            service, session, Model(("read_history", {"ref": "H2"}), RuntimeError("first failure"))
        )
        service.full.provider_factory = lambda: Model(
            ("write_note", {"key": "钟楼", "content": "明天赴约", "kind": "fact", "sources": ["H2"]}),
            RuntimeError("second failure"),
        )
        retried = service.submit(
            session["id"],
            TurnRequest(
                text=failed["request"]["text"],
                operation_id="resume-twice-1",
                expected_revision=0,
                resume_turn_id=failed["id"],
            ),
        )
        await service.tasks[retried["id"]]
        assert store.turn(retried["id"])["status"] == "error"
        retried_progress = store.full_agent_progress(retried["id"])
        assert len(tool_exchanges(retried_progress["new_trace"])) == 4
        assert retried_progress["snapshot"]["full_agent"]["notes"]["钟楼"]["content"] == "明天赴约"
        assert len(tool_exchanges(store.full_agent_progress(failed["id"])["new_trace"])) == 2

        model = Model("她看向新的方向。")
        service.full.provider_factory = lambda: model
        fresh = service.submit(
            session["id"],
            TurnRequest(text="改成另一个行动。", operation_id="resume-edited", expected_revision=0),
        )
        await service.tasks[fresh["id"]]
        assert store.turn(fresh["id"])["status"] == "committed"
        assert not any(m.get("tool_calls") or m["role"] == "tool" for m in model.requests[0]["messages"])
        assert store.session(session["id"])["snapshot"]["full_agent"]["notes"] == {}
    finally:
        await service.close()


async def test_fork_regeneration_and_delete(story):
    store, session = story
    service = runtime(store)
    try:
        result, first = await turn(
            service,
            session,
            Model(
                ("read_history", {"ref": "H2"}),
                ("write_note", {"key": "见面", "content": "钟楼见面", "kind": "fact", "sources": ["H2"]}),
                ("write_handoff", handoff("约定已达成", through=None)),
                "第一版正文",
            ),
        )
        assert result["status"] == "committed", result["error"]
        fork = store.fork_session(first["id"], first["head"], first["revision"])
        assert fork["agent_mode"] == "full_agent"
        note = fork["snapshot"]["full_agent"]["notes"]["见面"]
        assert note["sources"][0]["message_id"] == fork["messages"][1]["id"]
        assert note_view(note, fork["messages"])["sources"] == ["H2"]
        assert len(tool_exchanges(load_trace(store, fork["id"], fork["messages"]))) == 6
        result, regenerated = await turn(
            service, first, Model("第二版正文"), mode="regenerate", regenerate_note="保留钟楼约定，缩短正文。"
        )
        assert result["status"] == "committed", result["error"]
        assert not any(m["source"] == "turn_instruction" for m in result["preview"]["trace"])
        [instruction] = [m for m in result["preview"]["trace"] if m["source"] == "regenerate_instruction"]
        assert instruction["content"] == store.settings().regenerate_prompt + "\n保留钟楼约定，缩短正文。"
        assert regenerated["snapshot"]["full_agent"]["notes"] == {}
        assert tool_exchanges(load_trace(store, first["id"], regenerated["messages"])) == []
        await service.delete_session(first["id"])
        assert len(tool_exchanges(load_trace(store, fork["id"], fork["messages"]))) == 6
        with store.connection() as db:
            assert (
                db.execute(
                    "SELECT count(*) FROM full_agent_trace WHERE session=?", (first["id"],)
                ).fetchone()[0]
                == 0
            )
    finally:
        await service.close()


async def test_budget_step_limit_and_invalid_tools_roll_back(story):
    store, session = story
    service = runtime(store)
    config = store.settings()
    config.full_agent_config.max_steps = 2
    store.save_settings(config)
    try:
        model = Model(
            ("patch_state", {"operations": [], "comment": "unauthorized"}),
            ("new_context", {"handoff": handoff("不能重置本回合调用额度")}),
        )
        result, current = await turn(service, session, model)
        assert result["status"] == "error" and "上限" in result["error"]
        for request in model.requests:
            assert "最后一次模型请求" not in str(request["messages"])
            assert "tavern_call_budget" not in str(request["messages"])
        assert any(
            e["kind"] == "error" and e["payload"].get("error_code") == "BUDGET_EXHAUSTED"
            for e in store.events(result["id"])
        )
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
async def test_native_wire_and_atomic_handoff_on_invalid_arguments(story, monkeypatch, api_mode):
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
                output = [
                    {
                        "type": "function_call",
                        "id": "fc_x",
                        "call_id": "x",
                        "name": "new_context",
                        "arguments": '{"handoff":"handoff", "keep_recent":99}',
                        "status": "completed",
                    }
                ]
            else:
                output = story_reply("responses")["output"]
            return sdk_httpx.Response(
                200,
                json={
                    "id": "resp_x",
                    "object": "response",
                    "created_at": 1,
                    "model": "test-model",
                    "status": "completed",
                    "output": output,
                    "usage": {"input_tokens": 100, "output_tokens": 10, "total_tokens": 110},
                },
            )
        if len(calls) == 1:
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "x",
                        "type": "function",
                        "function": {
                            "name": "new_context",
                            "arguments": '{"handoff":"handoff", "keep_recent":99}',
                        },
                    }
                ],
            }
            finish = "tool_calls"
        else:
            message, finish = story_reply("chat_completions")["choices"][0]["message"], "stop"
        return sdk_httpx.Response(
            200,
            json={
                "id": "reply",
                "object": "chat.completion",
                "created": 1,
                "model": "test-model",
                "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110},
            },
        )

    await provider._client._client.aclose()
    provider._client._client = sdk_httpx.AsyncClient(
        transport=sdk_httpx.MockTransport(transport), trust_env=False
    )
    try:
        result, current = await turn(service, session, provider)
        assert result["status"] == "committed", result["error"]
        assert calls[0]["tools"] and calls[0]["parallel_tool_calls"] is True
        if api_mode == "responses":
            assert any(m.get("call_id") == "x" and "error" in m.get("output", "") for m in calls[1]["input"])
        else:
            assert any(m.get("tool_call_id") == "x" and "error" in m["content"] for m in calls[1]["messages"])
        assert current["snapshot"]["full_agent"]["window"]["number"] == 1
        assert not current_text(current["snapshot"]["full_agent"])
    finally:
        await service.close()


async def test_empty_provider_error_keeps_diagnostic_type(story):
    store, session = story
    service = runtime(store)
    try:
        failed, _ = await turn(service, session, Model(TimeoutError()))
        assert failed["status"] == "error" and failed["error"] == "TimeoutError"
        error = next(e["payload"] for e in store.events(failed["id"]) if e["kind"] == "error")
        assert error["error_type"] == "TimeoutError"
    finally:
        await service.close()


async def test_full_api_preview_recovery_and_mode_boundaries(story):
    from tavern.full_agent.handoff import HandoffWrite, apply_write

    store, session = story
    app = create_app(store.root)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
            preview = await client.post(f"/api/sessions/{session['id']}/preview", json={"text": "继续"})
            assert preview.status_code == 200
            assert preview.json()["agent_mode"] == "full_agent"
            requested = await client.post(
                f"/api/sessions/{session['id']}/preview",
                json={"text": "继续", "update_variables": True, "local_estimate": True},
            )
            assert requested.status_code == 200
            # The local reference is opt-in for the context panel only; the
            # provider observation is never replaced or filled by it.
            assert "local_estimate" not in preview.json()
            assert requested.json()["local_estimate"]["tokens"] > 0
            assert requested.json()["context_usage"]["tokens"] is None
            trace = requested.json()["trace"]
            assert all("local_tokens" in item for item in trace)
            input_index = next(i for i, item in enumerate(trace) if item["source"] == "input")
            assert trace[input_index + 1]["source"] == "turn_variable_instruction"
            assert trace[-1]["source"] == "context_status"
            assert not any(item["source"] == "handoff" for item in trace)  # Empty handoff has no frame.
            assert {tool["function"]["name"] for tool in requested.json()["tools"]} == {
                tool["function"]["name"] for tool in preview.json()["tools"]
            }
            exported = await client.post(
                f"/api/sessions/{session['id']}/messages-preview", json={"text": "继续"}
            )
            assert exported.status_code == 200
            assert exported.json() == preview.json()["messages"]
            assert {"role": "user", "content": "[H2]\n继续"} in exported.json()
            assert not any(x["source"] in ("memory", "retrieval_protocol") for x in preview.json()["trace"])
            removed = await client.put(
                f"/api/sessions/{session['id']}/full-agent/context",
                json={"content": "## 当前交接\n- 用户正文", "expected_revision": 0},
            )
            assert removed.status_code in (404, 405)
            endpoint = f"/api/sessions/{session['id']}/full-agent/new-context"
            assert (
                await client.post(endpoint, json={"expected_revision": 0, "content": "不能写入交接"})
            ).status_code == 422
            uncovered = await client.post(endpoint, json={"keep_recent": 0, "expected_revision": 0})
            assert uncovered.status_code == 400
            assert "H1" in uncovered.text and "Agent" in uncovered.text
            assert store.session(session["id"])["revision"] == 0
            # The manual operation only reuses the stored model-written handoff.
            snapshot = copy.deepcopy(session["snapshot"])
            original = apply_write(
                snapshot["full_agent"]["handoff"],
                HandoffWrite.model_validate(handoff("已保存交接", through="H1")),
                session["messages"],
            )
            snapshot["full_agent"]["handoff"] = original
            with store.connection() as db:
                db.execute(
                    "UPDATE messages SET snapshot=? WHERE id=?", (json.dumps(snapshot), session["head"])
                )
            current = store.session(session["id"])
            tid, _ = store.create_turn(
                session["id"],
                TurnRequest(operation_id="busy-window", expected_revision=current["revision"], text="继续"),
            )
            assert (
                await client.post(endpoint, json={"expected_revision": current["revision"]})
            ).status_code == 409
            store.set_turn(tid, status="cancelled")
            result = await client.post(
                endpoint, json={"keep_recent": 0, "expected_revision": current["revision"]}
            )
            assert result.status_code == 200
            assert result.json()["snapshot"]["full_agent"]["window"]["start"] == 1
            assert result.json()["snapshot"]["full_agent"]["handoff"] == original
            view = (await client.get(f"/api/sessions/{session['id']}/full-agent")).json()
            assert view["handoff"] == {"text": render_text(original, current["messages"])}
            assert (
                await client.post(endpoint, json={"expected_revision": current["revision"]})
            ).status_code == 409
            assert (
                await client.get(f"/api/sessions/{session['id']}/memory/rebuild-estimate")
            ).status_code == 400
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
    snapshot["state"] = {"initialized_lorebooks": {"opaque-identity": "ledger-only"}}
    snapshot["memory_document"] = {"version": 2, "records": [{"content": "legacy-memory"}]}
    result = compile_window(
        store.session_card(session["id"])["document"],
        session,
        session["messages"],
        snapshot,
        "继续",
        store.settings().for_role("full_agent"),
        [],
    )
    wire = json.dumps(result["messages"], ensure_ascii=False)
    for hidden in (
        "只能通过工具读取",
        "ledger-only",
        "opaque-identity",
        "legacy-memory",
        session["id"],
        session["head"],
    ):
        assert hidden not in wire


def test_cloud_checkpoint_survives_directory_and_ineffective_variable_changes():
    from tavern.conversation.compaction import completed_context, hashes
    from tavern.core.models import Settings
    from tavern.full_agent.state import full_state
    from tavern.scripting.request_context import response_origin

    cfg = Settings(model="fixture", api_mode="responses", context_strategy="openai_compact")
    session = {"id": "directory", "revision": 0, "persona": "玩家"}
    snapshot = {"state": {"place": 1, "unreferenced": 0}, "scopes": {}}
    state = full_state(snapshot)
    document = {"name": "向导", "description": "固定位置 {{getvar::place}}"}
    records = []
    history = [
        {"id": "old-user", "role": "user", "raw": "此前输入"},
        {"id": "old-reply", "role": "assistant", "raw": "此前正文"},
    ]
    user_input = ""

    def compile_current():
        return compile_window(document, session, history, snapshot, user_input, cfg, [], continuation=records)

    first = compile_current()
    completed = completed_context(first)
    assert len(completed["_compact_body"]) == 2
    records.extend(first["context_additions"])
    state["compaction"] = {
        "version": 1,
        "origin": response_origin(cfg),
        "signature": first["_compact_signature"],
        "covered": hashes(completed["_compact_body"]),
        "items": [{"type": "compaction", "encrypted_content": "old-directory"}],
    }
    user_input = "继续"
    restored = compile_current()
    assert restored["compaction_opaque"]
    assert any(row["source"] == "note_list" for row in restored["context_additions"])
    records.extend(restored["context_additions"])
    assert compile_current()["messages"] == restored["messages"]

    state["notes"]["线索"] = {"content": "笔记正文不自动注入", "status": "active"}
    snapshot["state"].update(place="1", unreferenced=999)
    added = compile_current()
    assert added["compaction_opaque"] and not added["_compact_stale"]
    assert added["messages"][: len(restored["messages"])] == restored["messages"]
    directory = next(row for row in added["context_additions"] if row["source"] == "note_list")
    assert "线索" in directory["content"] and "取代此前目录" in directory["content"]
    assert "笔记正文不自动注入" not in json.dumps(added["messages"], ensure_ascii=False)
    records.extend(added["context_additions"])
    state["notes"]["线索"]["status"] = "archived"
    archived = compile_current()
    assert archived["compaction_opaque"]
    assert archived["messages"][: len(added["messages"])] == added["messages"]
    assert "当前没有有效笔记" in archived["context_additions"][-1]["content"]
    records.extend(archived["context_additions"])

    snapshot["state"]["place"] = 2
    before = copy.deepcopy(snapshot)
    changed = compile_current()
    assert changed["_compact_stale"] and not changed["compaction_opaque"]
    assert "固定位置 2" in json.dumps(changed["messages"], ensure_ascii=False)
    assert snapshot == before, "compilation/preview must not rebuild or change the saved state"


async def test_real_index_tool_lore_shares_presence_and_cooldown_without_duplicate_injection(tmp_path):
    store = Store(tmp_path)
    document = {
        "name": "向导",
        "first_mes": "钟楼的门开了。",
        "description": "你是向导。",
        "character_book": {
            "entries": [
                {
                    "id": "tower",
                    "keys": ["钟楼"],
                    "content": "钟楼每晚九点关门。",
                    "comment": "钟楼",
                    "enabled": True,
                },
                {
                    "id": "port",
                    "keys": ["旧港"],
                    "content": "旧港的船只明晨出发。",
                    "comment": "旧港",
                    "enabled": True,
                },
            ]
        },
    }
    card = store.import_card(json.dumps(document, ensure_ascii=False).encode(), "card.json")
    settings = store.settings()
    settings.retrieval.channels = ["keyword"]
    store.save_settings(settings)
    session = store.new_session(SessionCreate(card_id=card["id"], agent_mode="full_agent"))
    service = runtime(store)
    try:
        model = Model(
            ("recall_lore", {"query": "旧港"}),
            ("recall_lore", {"query": "钟楼"}),
            ("recall_lore", {"query": "旧港"}),
            "向导指向旧港。",
        )
        result, current = await turn(service, session, model)
        assert result["status"] == "committed", result["error"]
        requests = [r["messages"] for r in model.requests]
        assert any(
            "<card_description>\n你是向导。\n</card_description>" in m.get("content", "") for m in requests[0]
        )
        assert (
            sum(m.get("content") == "<lorebook>\n钟楼每晚九点关门。\n</lorebook>" for m in requests[0]) == 1
        )
        assert not any(
            "旧港的船只明晨出发。" in m.get("content", "")
            for messages in requests
            for m in messages
            if m["role"] == "system"
        )
        tool_results = [json.loads(m["content"]) for m in requests[-1] if m["role"] == "tool"]
        assert (
            tool_results[0]["data"]["matches"][0]["text"] == "<lorebook>\n旧港的船只明晨出发。\n</lorebook>"
        )
        assert set(tool_results[0]["data"]) == {"matches"}
        assert set(tool_results[0]["data"]["matches"][0]) == {"text"}
        assert tool_results[2]["data"]["matches"] == []
        assert (
            sum(m.get("content") == "<lorebook>\n钟楼每晚九点关门。\n</lorebook>" for m in requests[2]) == 1
        )
        records = current["snapshot"]["lore_injections"]
        assert len(records) == 1 and records[0]["title"] == "钟楼"
        assert records[0]["anchor_id"] == session["head"]
        assert current["snapshot"]["lore"]["port"]["present"] is True
        trace = load_trace(store, session["id"], current["messages"])
        recalled = [r["message"] for r in trace if r["message"].get("lore_ids")]
        assert len(recalled) == 1 and recalled[0]["lore_ids"] == ["port"]
        assert all("lore_ids" not in m for messages in requests for m in messages)
        retrievals = [e["payload"] for e in store.events(result["id"]) if e["kind"] == "retrieval"]
        assert [r["source"] for r in retrievals] == ["auto", "agent", "agent", "agent"]
        assert all(not r["injected"] for r in retrievals[1:])
        assert retrievals[1]["query"] == "旧港"

        async def follow(text, model):
            service.full.provider_factory = lambda: model
            result = service.submit(
                session["id"],
                TurnRequest(
                    text=text,
                    operation_id=f"follow-{current['revision']}",
                    expected_revision=current["revision"],
                ),
            )
            await service.tasks[result["id"]]
            result = store.turn(result["id"])
            assert result["status"] == "committed", result["error"]
            return store.session(session["id"])

        # The next automatic query must reuse the tool body still in history.
        model = Model(("recall_lore", {"query": "旧港"}), "船只仍在旧港。")
        current = await follow("我走进旧港。", model)
        messages = model.requests[-1]["messages"]
        assert not any(
            "旧港的船只明晨出发。" in m.get("content", "") for m in messages if m["role"] == "system"
        )
        outputs = [json.loads(m["content"]) for m in messages if m["role"] == "tool"]
        assert sum(bool(r["data"].get("matches")) for r in outputs) == 1
        assert outputs[-1]["data"]["matches"] == []
        assert current["snapshot"]["lore"]["port"]["present"] is True

        # Exiting the native exchange starts cooldown, including the same-request
        # exclusion even with cooldown=0 and a direct mention.
        model = Model(
            (
                "new_context",
                {
                    "keep_recent": 0,
                    "handoff": {
                        "chapters": [
                            {"op": "add", "title": "抵达旧港", "points": [{"text": "向导带玩家前往旧港。"}]}
                        ],
                        "current": [{"text": "玩家在旧港等候。"}],
                    },
                },
            ),
            ("recall_lore", {"query": "旧港"}),
            "向导继续等候。",
        )
        current = await follow("我在旧港等候。", model)
        assert current["snapshot"]["lore"]["port"] == {"present": False, "removed_at": 3}
        assert current["snapshot"]["lore_injections"] == []
        outputs = [json.loads(m["content"]) for m in model.requests[-1]["messages"] if m["role"] == "tool"]
        assert outputs[-1]["data"]["matches"] == []

        model = Model("新一轮的旧港钟声响起。")
        current = await follow("我查看旧港的船只。", model)
        assert current["snapshot"]["lore"]["port"]["present"] is True
        assert [r["id"] for r in current["snapshot"]["lore_injections"]] == ["port"]
        assert (
            sum(
                m.get("content") == "<lorebook>\n旧港的船只明晨出发。\n</lorebook>"
                for m in model.requests[0]["messages"]
            )
            == 1
        )
    finally:
        await service.close()


async def test_real_card_runtime_uses_agent_mvu_tool_and_preview_review(tmp_path):
    store = Store(tmp_path)
    document = json.loads(Path("samples/星航状态卡.json").read_bytes())
    document["data"]["extensions"]["tavern_helper"]["scripts"].append(
        {
            "id": "message-counter",
            "enabled": True,
            "content": """
        eventOn('MESSAGE_RECEIVED', async () => {
            await Promise.resolve();
            const data = getVariables({type:'chat'});
            data.message_callbacks = (data.message_callbacks || 0) + 1;
            data.energy_at_message = data.stat_data.能量;
            replaceVariables(data, {type:'chat'});
        });
        """,
        }
    )
    card = store.import_card(json.dumps(document, ensure_ascii=False).encode(), "card.json")
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
    model = Model(
        ("read_state", {}),
        (
            "patch_state",
            {"operations": [{"op": "replace", "path": "/能量", "value": 4}], "comment": "本轮变化"},
        ),
        ("write_handoff", handoff("目前能量为4", through=None)),
        "洛伊带你来到观测窗。",
    )
    try:
        session = await service.initialize_session(session["id"])
        result, pending = await turn(service, session, model, update_variables=True)
        assert result["status"] == "awaiting_review", result["error"]
        assert pending["head"] == session["head"]
        service.review(result["id"], True)
        current = store.session(session["id"])
        assert current["snapshot"]["state"]["stat_data"]["能量"] == 4
        assert current["snapshot"]["state"]["message_callbacks"] == 1
        assert current["snapshot"]["state"]["energy_at_message"] == 4
        assert "能量" in json.dumps(model.requests[1]["messages"], ensure_ascii=False)
        assert "initialized_lorebooks" not in json.dumps(model.requests[1]["messages"], ensure_ascii=False)
        assert len(current["messages"]) == 3
    finally:
        await service.close()


async def test_real_mvu_state_survives_failed_request_resume(tmp_path):
    store = Store(tmp_path)
    card = store.import_card(Path("samples/星航状态卡.json").read_bytes(), "card.json")
    with store.connection() as db:
        db.execute("UPDATE cards SET permission=1 WHERE id=?", (card["id"],))
    settings = store.settings()
    settings.full_agent_config.auto_retrieval = False
    store.save_settings(settings)
    session = store.new_session(SessionCreate(card_id=card["id"], agent_mode="full_agent", policy="preview"))
    service = ModeRuntime(store)
    try:
        session = await service.initialize_session(session["id"])
        failed, unchanged = await turn(
            service,
            session,
            Model(
                {"content": "洛伊带你来到观测窗。", "calls": [("read_history", {"ref": "H3"})]},
                ("read_state", {}),
                (
                    "patch_state",
                    {"operations": [{"op": "replace", "path": "/能量", "value": 4}], "comment": "本轮变化"},
                ),
                (
                    "write_note",
                    {
                        "key": "本轮场景",
                        "content": "洛伊带用户来到观测窗。",
                        "kind": "fact",
                        "sources": ["H3"],
                    },
                ),
                ("new_context", {"handoff": handoff("目前能量为4"), "keep_recent": 0}),
                RuntimeError("model disconnected"),
            ),
            update_variables=True,
        )
        assert failed["status"] == "error"
        assert unchanged["snapshot"]["state"]["stat_data"]["能量"] != 4
        assert store.full_agent_progress(failed["id"])["patch_state_done"] is True
        progress = store.full_agent_progress(failed["id"])
        delivered = next(
            row["message"]["_story_received"]
            for row in progress["new_trace"]
            if row["message"].get("_story_received")
        )
        assert delivered["input_id"] == progress["user"]["id"]
        assert progress["snapshot"]["full_agent"]["notes"]["本轮场景"]["sources"] == [
            {"message_id": progress["output_id"]}
        ]
        assert store.full_agent_progress(failed["id"])["story"]["content"] == "洛伊带你来到观测窗。"
        model = Model(
            ("read_note", {"key": "本轮场景"}),
            "",
        )
        service.full.provider_factory = lambda: model
        resumed = service.submit(
            session["id"],
            TurnRequest(
                text=failed["request"]["text"],
                operation_id="resume-mvu",
                expected_revision=0,
                update_variables=True,
                resume_turn_id=failed["id"],
            ),
        )
        await service.tasks[resumed["id"]]
        assert store.turn(resumed["id"])["status"] == "awaiting_review"
        assert any(
            "<roleplay_draft>\n洛伊带你来到观测窗。" in m.get("content", "")
            for m in model.requests[0]["messages"]
        )
        service.review(resumed["id"], True)
        current = store.session(session["id"])
        assert current["snapshot"]["state"]["stat_data"]["能量"] == 4
        assert current["messages"][-1]["raw"] == "洛伊带你来到观测窗。"
        assert current["snapshot"]["full_agent"]["notes"]["本轮场景"]["sources"] == [
            {"message_id": current["head"]}
        ]
        assert any('"sources":["H3"]' in m.get("content", "") for m in model.requests[-1]["messages"])
        from tavern.full_agent.tools import tool_definitions

        document = store.session_card(current["id"])["document"]
        settings = store.session_settings(current).for_role("full_agent")
        definitions = tool_definitions(
            TurnRequest(operation_id="preview", expected_revision=current["revision"]),
            current,
            settings.full_agent_config,
            settings=settings,
            document=document,
        )
        replayed = compile_window(
            document,
            current,
            current["messages"],
            current["snapshot"],
            "继续",
            settings,
            definitions,
            trace=load_trace(store, current["id"], current["messages"]),
        )
        assert replayed["context_ledger"]["rebuild_reason"] is None
        # The retained draft frame already carries the complete story after
        # rollover; the committed history must not add a second prose copy.
        assert (
            "\n".join(m.get("content", "") for m in replayed["messages"]).count("洛伊带你来到观测窗。") == 1
        )
        fork = store.fork_session(current["id"], current["head"], current["revision"])
        assert fork["snapshot"]["full_agent"]["notes"]["本轮场景"]["sources"] == [
            {"message_id": fork["head"]}
        ]
    finally:
        await service.close()


async def test_long_lore_tool_result_reaches_model_complete(tmp_path):
    store = Store(tmp_path)
    content = "loreword " * 2500 + "ENDMARK"
    document = {
        "name": "向导",
        "first_mes": "开场。",
        "character_book": {
            "entries": [
                {"id": "long", "keys": ["loreword"], "content": content, "comment": "长条目", "enabled": True}
            ]
        },
    }
    card = store.import_card(json.dumps(document, ensure_ascii=False).encode(), "card.json")
    settings = store.settings()
    settings.retrieval.channels = ["keyword"]
    settings.full_agent_config.auto_retrieval = False
    store.save_settings(settings)
    session = store.new_session(SessionCreate(card_id=card["id"], agent_mode="full_agent"))
    service = runtime(store)
    model = Model(("recall_lore", {"query": "loreword"}), "继续。")
    try:
        result, _ = await turn(service, session, model)
        assert result["status"] == "committed", result["error"]
        tool_result = next(
            json.loads(m["content"]) for m in model.requests[1]["messages"] if m["role"] == "tool"
        )
        assert tool_result["data"]["matches"] == [{"text": f"<lorebook>\n{content}\n</lorebook>"}]
    finally:
        await service.close()


async def test_long_history_is_complete_and_fact_sources_need_only_be_valid(story):
    from tavern.full_agent.agent import FullAgent

    store, session = story
    settings = store.settings().for_role("full_agent")
    user = {"id": "pending-input", "role": "user", "raw": "当前输入"}
    req = TurnRequest(text="当前输入", operation_id="paging", expected_revision=0)
    tid, _ = store.create_turn(session["id"], req)
    owner = FullAgent(
        store,
        tid,
        store.session_card(session["id"])["document"],
        session,
        session["messages"],
        copy.deepcopy(session["snapshot"]),
        req,
        None,
        settings,
        user,
        None,
        output_id="pending-output",
    )
    text = "精确原文不可丢失。" * 300
    owner.source_history[0]["raw"] = text
    assert await owner.tools.execute("read_history", '{"ref":"H1"}') == {
        "ok": True,
        "data": {"ref": "H1", "role": owner.source_history[0]["role"], "text": text},
    }
    with pytest.raises(ValueError, match="没有此历史引用"):
        await owner.tools.execute("read_history", '{"ref":"T1.1"}')
    await owner.tools.execute(
        "write_note", json.dumps({"key": "事实", "content": "未读", "kind": "fact", "sources": ["H2"]})
    )
    note = copy.deepcopy(owner.state["notes"]["事实"])
    assert note["sources"][0]["message_id"] == user["id"]
    for sources in ([], ["H0"], ["H999"], ["T1.1"]):
        with pytest.raises(ValueError):
            await owner.tools.execute(
                "write_note",
                json.dumps({"key": "事实", "content": "无效", "kind": "fact", "sources": sources}),
            )
        assert owner.state["notes"]["事实"] == note


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


@pytest.mark.parametrize("channels", [["keyword"], ["bm25"], ["embedding"], ["keyword", "bm25", "embedding"]])
@pytest.mark.parametrize("rerank", [False, True])
def test_lore_tool_description_matches_enabled_retrieval(story, channels, rerank):
    from tavern.full_agent.tools import tool_definitions

    store, session = story
    settings = store.settings()
    settings.retrieval.channels = channels
    settings.local_models.reranker_enabled = rerank
    definitions = tool_definitions(
        TurnRequest(operation_id="description", expected_revision=0),
        session,
        settings.full_agent_config,
        settings=settings,
    )
    description = next(
        t["function"]["description"] for t in definitions if t["function"]["name"] == "recall_lore"
    )
    for channel, marker in (("keyword", "keyword"), ("bm25", "BM25"), ("embedding", "semantic")):
        assert (marker in description) == (channel in channels)
    assert ("Reranking orders" in description) == rerank
    assert "Previous-turn retrieval clues" not in description


async def test_empty_note_and_history_search_results_are_minimal(story):
    store, session = story
    service = runtime(store)
    model = Model(
        ("search_notes", {"query": "不存在的笔记"}),
        ("search_history", {"query": "绝不匹配的历史文本"}),
        "继续正文",
    )
    try:
        result, _ = await turn(service, session, model)
        assert result["status"] == "committed", result["error"]
        results = [json.loads(m["content"]) for m in model.requests[-1]["messages"] if m["role"] == "tool"]
        assert results == [{"ok": True, "data": {"matches": []}}] * 2
    finally:
        await service.close()


@pytest.mark.parametrize("status", ["active", "archived"])
async def test_note_update_and_reactivation_require_visible_version_after_rollover(story, status):
    from tavern.full_agent.agent import FullAgent
    from tavern.full_agent.state import rollover

    store, session = story
    req = TurnRequest(text="继续", operation_id="note-read", expected_revision=0)
    tid, _ = store.create_turn(session["id"], req)
    owner = FullAgent(
        store,
        tid,
        store.session_card(session["id"])["document"],
        session,
        session["messages"],
        copy.deepcopy(session["snapshot"]),
        req,
        None,
        store.settings().for_role("full_agent"),
        {"id": "input", "role": "user", "raw": "继续"},
        None,
        output_id="pending-output",
    )
    old = {
        "key": "线索",
        "content": "保留旧线索和未决事项",
        "kind": "hypothesis",
        "status": status,
        "revision": 1,
        "sources": [],
    }
    owner.state["notes"]["线索"] = copy.deepcopy(old)

    async def call(name, **args):
        return await owner.tools.execute(name, json.dumps(args, ensure_ascii=False))

    with pytest.raises(ValueError, match="read_note"):
        await call("write_note", key="线索", content=old["content"] + "，新增猜想", kind="hypothesis")
    await call("read_note", key="线索")
    await call("write_note", key="线索", content=old["content"] + "，新增猜想", kind="hypothesis")
    assert owner.state["notes"]["线索"]["revision"] == 2
    assert owner.state["notes"]["线索"]["status"] == "active"
    await call("write_note", key="线索", content=old["content"] + "，再次修订", kind="hypothesis")
    rollover(owner.state, owner.history, written(owner.history, "保留交接"), 0)
    with pytest.raises(ValueError, match="read_note"):
        await call("write_note", key="线索", content=old["content"] + "，修正猜想", kind="hypothesis")
    await call("read_note", key="线索")
    await call("write_note", key="线索", content=old["content"] + "，修正猜想", kind="hypothesis")
    assert owner.state["notes"]["线索"]["revision"] == 4


async def test_repeated_regeneration_updates_notes_from_same_saved_baseline(story):
    store, session = story
    service = runtime(store)
    try:
        result, base = await turn(
            service,
            session,
            Model(
                ("write_note", {"key": "线索", "content": "旧线索", "kind": "hypothesis"}),
                ("write_note", {"key": "待办", "content": "旧待办", "kind": "plan"}),
                ("write_handoff", handoff("旧交接", through=None)),
                "前一回合",
            ),
        )
        assert result["status"] == "committed", result["error"]
        baseline = copy.deepcopy(base["snapshot"]["full_agent"])
        current = base
        versions = []
        for index in range(3):
            model = Model(
                ("read_note", {"key": "线索"}),
                ("write_note", {"key": "线索", "content": f"旧线索，加候选{index}", "kind": "hypothesis"}),
                ("archive_note", {"key": "待办"}),
                ("write_handoff", handoff(f"旧交接，加候选{index}", through=None)),
                f"正文候选{index}",
            )
            result, current = await turn(service, current, model, mode="send" if index == 0 else "regenerate")
            assert result["status"] == "committed", result["error"]
            versions.append(current)
            state = current["snapshot"]["full_agent"]
            assert state["notes"]["线索"]["revision"] == 2
            assert state["notes"]["待办"]["revision"] == 2
            assert state["handoff"]["revision"] == 2
            assert current["messages"][-2]["snapshot"]["full_agent"] == baseline
            read_result = json.loads(
                next(m["content"] for m in reversed(model.requests[1]["messages"]) if m["role"] == "tool")
            )
            assert read_result["data"]["text"] == "旧线索"
            assert "旧交接" in json.dumps(model.requests[0]["messages"], ensure_ascii=False)
            assert f"候选{index - 1}" not in json.dumps(model.requests[0]["messages"], ensure_ascii=False)
        for version in versions:
            current = store.update_session(current["id"], current["revision"], head=version["head"])
            assert current["snapshot"]["full_agent"] == version["snapshot"]["full_agent"]
    finally:
        await service.close()


def test_tool_references_stay_stable_when_documents_are_reordered(story):
    from tavern.full_agent.agent import FullAgent

    store, session = story
    document = {
        "name": "角色",
        "description": "literal @D1",
        "character_book": {
            "entries": [
                {"id": "a" * 32, "keys": ["一"], "content": "一"},
                {"id": "b" * 32, "keys": ["二"], "content": "二"},
            ]
        },
    }
    req = TurnRequest(text="当前输入", operation_id="refs", expected_revision=0)
    tid, _ = store.create_turn(session["id"], req)
    snapshot = copy.deepcopy(session["snapshot"])

    def agent(doc):
        return FullAgent(
            store,
            tid,
            doc,
            session,
            session["messages"],
            snapshot,
            req,
            None,
            store.settings().for_role("full_agent"),
            {"id": "input", "role": "user", "raw": "输入"},
            None,
            output_id="pending-output",
        )

    first = agent(document)
    handle = first.model_view.forward["a" * 32]
    assert handle != "@D1"
    assert first.lore_refs()["L1"] == "a" * 32
    changed = copy.deepcopy(document)
    changed["character_book"]["entries"].reverse()
    second = agent(changed)
    assert second.model_view.restore(handle) == "a" * 32
    assert second.lore_refs()["L1"] == "a" * 32


async def test_prompt_tags_and_tool_contracts_reach_model(story):
    from tavern.full_agent.config import FULL_AGENT_PROMPT
    from tavern.full_agent.tools import SPECS

    store, session = story
    settings = store.settings()
    settings.full_agent_config.handoff_tokens = 1536
    settings.full_agent_config.note_tokens = 3072
    store.save_settings(settings)
    service = runtime(store)
    model = Model(("write_handoff", handoff("约定仍待兑现", through=None)), "她点头。")
    try:
        result, _ = await turn(service, session, model, update_variables=False, allow_content_edit=False)
        assert result["status"] == "committed", result["error"]
        wire = model.requests[-1]
        assert wire["messages"][0]["content"] == FULL_AGENT_PROMPT
        assert "Full Agent 工具协议" in wire["messages"][1]["content"]
        assert wire["messages"][2]["content"].startswith("本次上下文窗口上限为")
        assert all(name not in FULL_AGENT_PROMPT for name in SPECS)
        assert any(
            m.get("content", "").startswith("<user_character>\n用户：")
            and m["content"].endswith("\n</user_character>")
            for m in wire["messages"]
        )
        # The successful write is already visible in its tool exchange; do not
        # require a duplicate runtime handoff frame in the same window.
        assert not any(m.get("content", "").startswith("<tavern_memory>") for m in wire["messages"])
        assert "约定仍待兑现" in json.dumps(wire["messages"], ensure_ascii=False)
        assert not any(
            "约定仍待兑现" in m.get("content", "") for m in wire["messages"] if m["role"] == "system"
        )
        assert any("当前用户输入引用 H2" in m.get("content", "") for m in wire["messages"])
        definitions = {t["function"]["name"]: t["function"] for t in wire["extra_body"]["tools"]}
        assert set(definitions) == set(SPECS) - {"revise_content", "write_plot"}
        assert (
            "When variable updates are authorized, submit at least one successful patch this turn"
            in definitions["patch_state"]["description"]
        )
        assert (
            "validation failures may be corrected and retried and do not count as success"
            in definitions["patch_state"]["description"]
        )
        assert not any(
            m.get("content", "").startswith("本轮须至少成功调用一次 patch_state") for m in wire["messages"]
        )
        assert "1536 tokens" in definitions["new_context"]["description"]
        assert "3072 tokens" in definitions["write_note"]["description"]
        assert "Call alone" in definitions["new_context"]["description"]
        assert set(definitions["new_context"]["parameters"]["properties"]) == {"handoff", "keep_recent"}
        assert "handoff" not in definitions["new_context"]["parameters"].get("required", [])
        assert "current" in definitions["write_handoff"]["parameters"]["properties"]
        assert "offset" not in definitions["read_history"]["parameters"]["properties"]
        assert "Note contents are not automatically injected" in definitions["search_notes"]["description"]
    finally:
        await service.close()


def test_constant_and_retained_lore_have_same_tag_without_mutating_storage(story):
    store, session = story
    document = {
        "name": "向导",
        "character_book": {
            "entries": [
                {"id": "constant", "content": "常驻背景", "constant": True, "enabled": True},
            ]
        },
    }
    snapshot = copy.deepcopy(session["snapshot"])
    snapshot["lore_injections"] = [
        {"id": "retained", "content": "动态背景", "title": "背景", "anchor_id": session["head"]}
    ]
    compiled = compile_window(document, session, session["messages"], snapshot, "继续", store.settings(), [])
    lore = [m for m in compiled["trace"] if m["source"].startswith("lore:")]
    assert {m["content"] for m in lore} == {
        "<lorebook>\n常驻背景\n</lorebook>",
        "<lorebook>\n动态背景\n</lorebook>",
    }
    assert snapshot["lore_injections"][0]["content"] == "动态背景"


def test_full_window_places_current_input_before_dynamic_context(story):
    store, session = story
    snapshot = copy.deepcopy(session["snapshot"])
    snapshot["lore_injections"] = [
        {"id": "current", "content": "本轮背景", "title": "背景", "anchor_id": "current-input"},
        {"id": "second", "content": "补充背景", "title": "背景二", "anchor_id": "current-input"},
    ]
    compiled = compile_window(
        {"name": "向导", "post_history_instructions": "作者补充"},
        session,
        session["messages"],
        snapshot,
        "继续",
        store.settings(),
        [],
        input_id="current-input",
        require_state_patch=True,
        instruction="本轮重写要求",
    )
    sources = [item["source"] for item in compiled["trace"]]
    last_history = max(i for i, source in enumerate(sources) if source == "history")
    assert sources[last_history + 1 :] == [
        "lore:current",
        "lore:second",
        "input",
        "turn_variable_instruction",
        "mvu_rules",
        "post",
        "turn_instruction",
        "regenerate_instruction",
        "context_status",
    ]


@pytest.mark.parametrize(
    "window_number, notes, expected",
    [
        (1, {}, "有效 0 条，已归档 0 条"),
        (2, {"old": {"status": "archived", "content": "不可自动注入的归档内容"}}, "有效 0 条，已归档 1 条"),
        (3, {"old": {"status": "archived"}, "new": {"status": "active"}}, "有效 1 条，已归档 1 条"),
    ],
)
def test_explicit_window_and_memory_inventory(story, window_number, notes, expected):
    store, session = story
    snapshot = copy.deepcopy(session["snapshot"])
    snapshot["full_agent"]["window"]["number"] = window_number
    snapshot["full_agent"]["notes"] = notes
    result = compile_window(
        store.session_card(session["id"])["document"],
        session,
        session["messages"],
        snapshot,
        "继续",
        store.settings(),
        [],
    )
    status = next(m["content"] for m in result["trace"] if m["source"] == "context_status")
    assert expected in status
    assert f"当前第 {window_number} 个" in status
    assert "总计" not in status and "还可调用模型" not in status
    assert "无需搜索" not in status and "尚未换窗" not in status
    assert "不可自动注入的归档内容" not in str(result["messages"])
    assert [m["position"] for m in result["trace"]] == list(range(len(result["messages"])))


def test_pressure_notice_changes_only_with_actionable_state(story, monkeypatch):
    store, session = story
    settings = store.settings().for_role("full_agent")
    settings.full_agent_config.pressure_ratio = 0.7
    settings.context_tokens = 20000
    settings.reply_tokens = 128
    document = {"name": "向导", "description": "背景。" * 1000}
    snapshot = copy.deepcopy(session["snapshot"])
    args = (document, session, [], snapshot, "继续", settings, [])
    probe = compile_window(*args, input_id="current-input")
    assert not probe["rollover_required"]
    measured = [14400]
    monkeypatch.setattr("tavern.full_agent.context.context_tokens", lambda *_: measured[0])
    continuation = []

    def compile_current():
        result = compile_window(*args, input_id="current-input", continuation=continuation)
        continuation.extend(result["context_additions"])
        return result

    def read_result(call_id, text):
        continuation.extend(
            [
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {"name": "read_state", "arguments": "{}"},
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": json.dumps({"text": text}, ensure_ascii=False),
                },
            ]
        )

    pressure = compile_current()
    assert not pressure["rollover_required"]
    read_result("short-read", "读取状态")
    continued = compile_current()
    assert pressure["messages"] == continued["messages"][: len(pressure["messages"])]
    assert not continued["context_additions"]

    complete_text = "完整资料。" * 2000 + "必须保留的结尾"
    read_result("long-read", complete_text)
    measured[0] = 20100
    overflow = compile_current()
    assert overflow["rollover_required"]
    assert continued["messages"] == overflow["messages"][: len(continued["messages"])]
    notices = [m for m in overflow["trace"] if m["source"] == "context_budget"]
    assert len(notices) == 2 and "强制换窗" in notices[-1]["content"]
    assert complete_text in next(
        m["content"] for m in overflow["messages"] if m.get("tool_call_id") == "long-read"
    )

    settings.context_tokens = 60000
    measured[0] = None
    cleared = compile_current()
    assert not cleared["rollover_required"]
    # Capacity is now declared in the fixed window header. Updating it rebuilds
    # that prefix and retires obsolete warnings, while retaining tool exchanges.
    assert cleared["context_ledger"]["rebuild_reason"] == "context_sources_changed"
    assert f"{settings.context_tokens} tokens" in cleared["messages"][2]["content"]
    assert not any(m["source"] == "context_budget" for m in cleared["trace"])
    assert complete_text in next(
        m["content"] for m in cleared["messages"] if m.get("tool_call_id") == "long-read"
    )
    assert compile_current()["messages"] == cleared["messages"]

    settings.context_tokens = 20000
    measured[0] = 14400
    pressure_again = compile_current()
    assert not pressure_again["rollover_required"]
    assert pressure_again["context_ledger"]["rebuild_reason"] == "context_sources_changed"
    notices = [m for m in pressure_again["context_additions"] if m.get("source") == "context_budget"]
    assert len(notices) == 1 and "窗口水位提醒" in notices[0]["content"]
    assert compile_current()["messages"] == pressure_again["messages"]


async def test_forced_rollover_vetoes_reply_and_mutations_then_resumes(story, monkeypatch):
    store, session = story
    settings = store.settings()
    settings.full_agent.context_tokens = 16000
    settings.full_agent.reply_tokens = 1024
    store.save_settings(settings)
    large = "旧场景细节。" * 6000
    # The real observation lifecycle is covered in test_domain; this fixture
    # drives the tool veto contract with an explicit previous measurement.
    monkeypatch.setattr(
        "tavern.full_agent.context.context_tokens",
        lambda messages, *_: 16000 if any(large in (m.get("content") or "") for m in messages) else None,
    )
    with store.connection() as db:
        db.execute("UPDATE messages SET raw=?,model=? WHERE id=?", (large, large, session["head"]))
    session = store.session(session["id"])
    service = runtime(store)
    model = Model(
        ("write_handoff", handoff("这个操作也必须被否决", through=None)),
        ("new_context", {"handoff": handoff("换窗参数错误"), "keep_recent": 99}),
        ("new_context", {"handoff": handoff("新交接保留必要经历"), "keep_recent": 0}),
        "换窗后继续回应玩家。",
    )
    try:
        premature = Model("这条正文必须被否决")
        failed, _ = await turn(service, session, premature)
        assert failed["status"] == "error" and len(premature.requests) == 1
        assert store.full_agent_progress(failed["id"])["story"] is None
        service.full.provider_factory = lambda: model
        resumed = service.submit(
            session["id"],
            TurnRequest(
                text=failed["request"]["text"],
                operation_id="rollover-resume",
                expected_revision=0,
                resume_turn_id=failed["id"],
            ),
        )
        await service.tasks[resumed["id"]]
        result, current = store.turn(resumed["id"]), store.session(session["id"])
        assert result["status"] == "committed", result["error"]
        assert current["messages"][-1]["raw"] == "换窗后继续回应玩家。"
        assert current_text(current["snapshot"]["full_agent"]) == "新交接保留必要经历"
        assert current["snapshot"]["full_agent"]["window"]["number"] == 2
        for request in [premature.requests[0], *model.requests[:3]]:
            assert request["extra_body"]["tool_choice"]["function"]["name"] == "new_context"
            assert request["extra_body"]["parallel_tool_calls"] is False
        assert model.requests[-1]["extra_body"]["tool_choice"] == "auto"
        assert model.requests[-1]["extra_body"]["parallel_tool_calls"] is True
        assert large in str(model.requests[0]["messages"])
        assert large not in str(model.requests[-1]["messages"])
        assert "我答应明天在钟楼见面" in str(model.requests[-1]["messages"])
        assert "已否决且未执行" in str(model.requests[1]["messages"])
        assert "这个操作也必须被否决" not in str(
            [m for m in model.requests[1]["messages"] if m["role"] == "system"]
        )
        assert not any(
            e["kind"] == "text" and e["payload"].get("text") == "这条正文必须被否决"
            for e in store.events(result["id"])
        )
    finally:
        await service.close()


async def test_provider_sends_complete_request_without_preflight_estimate(story, monkeypatch):
    import tavern.core.provider as provider_module

    store, _ = story
    settings = store.settings().for_role("full_agent")
    settings.model, settings.api_key, settings.use_proxy = "test-model", "local", False
    settings.base_url = "http://127.0.0.1:9999/v1"
    settings.context_tokens, settings.reply_tokens = 8192, 1024
    requests = []

    def transport(request):
        requests.append(json.loads(request.content))
        return provider_module.httpx.Response(
            200,
            json={
                "id": "reply",
                "object": "chat.completion",
                "created": 1,
                "model": "test-model",
                "choices": [
                    {"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 100, "completion_tokens": 1, "total_tokens": 101},
            },
        )

    original_client = provider_module.httpx.AsyncClient

    class MockClient(original_client):
        def __init__(self, **kwargs):
            kwargs["transport"] = provider_module.httpx.MockTransport(transport)
            super().__init__(**kwargs)

    monkeypatch.setattr(provider_module.httpx, "AsyncClient", MockClient)
    captured = []
    provider = await make_provider(settings, captured.append)
    try:
        assert await provider._recover_from_overflow([], None) is None
        messages = [{"role": "user", "content": "history " * 12000}]
        response = await provider.chat_complete(messages)
        assert response.content == "ok"
        assert requests[0]["messages"] == messages
    finally:
        await provider.close()


async def test_responses_forced_rollover_tool_choice_matches_protocol(story):
    store, _ = story
    settings = store.settings().for_role("full_agent")
    settings.api_mode, settings.model, settings.api_key = "responses", "test-model", "local"
    settings.kemini.enabled = True
    provider = await make_provider(settings)
    try:
        assert await provider._recover_from_overflow([], None) is None
        payload = provider._request(
            [{"role": "user", "content": "继续"}],
            extra_body={
                "tool_choice": {"type": "function", "function": {"name": "new_context"}},
                "parallel_tool_calls": False,
            },
        )
        assert payload["tool_choice"] == {"type": "function", "name": "new_context"}
        assert payload["parallel_tool_calls"] is False
        tools = [{"type": "function", "function": {"name": "new_context", "parameters": {"type": "object"}}}]
        provider.operation_purpose = "narrative"
        assert (
            provider._begin_kemini(
                tools,
                {"extra_body": {"tool_choice": {"type": "function", "function": {"name": "new_context"}}}},
            )
            == tools
        )
        assert not provider._kemini.transport
    finally:
        await provider.close()
