"""Real transport deltas remain provisional across Full Agent transaction boundaries."""

import asyncio
import json

import httpx
import pytest

from tavern.core.models import TurnRequest
from tavern.scripting.model_transport import ModelTransport
from tavern.storage.store import Conflict, Store
from tests.backend.test_full_agent import (
    Model,
    Script,
    ScriptHost,
    current_text,
    handoff,
    runtime,
    story_reply,
)
from tests.backend.test_full_agent_alignment import game


def delta(text):
    return {"choices": [{"index": 0, "delta": {"content": text}}]}


def terminal(reason="stop"):
    return {"choices": [{"index": 0, "delta": {}, "finish_reason": reason}]}


def body_start(text):
    return delta(text)


def body_delta(text):
    return delta(text)


def body_end():
    return [terminal()]


def story_events(text):
    return [body_start(text), *body_end()]


def tool_events(name="write_handoff", arguments=None):
    args = json.dumps(
        handoff("已确认的工具进度", through=None) if arguments is None else arguments, ensure_ascii=False
    )
    return [
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {"index": 0, "id": "call-1", "function": {"name": name, "arguments": args[:5]}}
                        ]
                    }
                }
            ]
        },
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": args[5:]}}]}}]},
        terminal("tool_calls"),
    ]


class Gate:
    def __init__(self):
        self.reached, self.release = asyncio.Event(), asyncio.Event()


class WireStream(httpx.AsyncByteStream):
    def __init__(self, *pieces):
        self.pieces, self.closed = pieces, False

    async def __aiter__(self):
        for piece in self.pieces:
            if isinstance(piece, Gate):
                piece.reached.set()
                await piece.release.wait()
            elif isinstance(piece, Exception):
                raise piece
            else:
                yield (
                    piece
                    if isinstance(piece, bytes)
                    else ("data: " + json.dumps(piece, ensure_ascii=False) + "\n\n").encode()
                )

    async def aclose(self):
        self.closed = True


def connect(store, service, *streams, api="chat_completions", mode="stream", status=200):
    settings = store.settings("full_agent")
    settings.api_mode, settings.model_request_mode = api, mode
    bodies = []

    def route(request):
        body = json.loads(request.content)
        bodies.append(body)
        assert bool(body.get("stream")) is (mode == "stream")
        return httpx.Response(
            status, headers={"content-type": "text/event-stream"}, stream=streams[len(bodies) - 1]
        )

    provider = ModelTransport(
        settings, "fixture", http_client=httpx.AsyncClient(transport=httpx.MockTransport(route))
    )
    service.full.provider_factory = lambda: provider
    return provider, bodies


def submit(service, session, **kwargs):
    return service.submit(
        session["id"],
        TurnRequest(
            text="继续故事",
            expected_revision=session["revision"],
            operation_id=kwargs.pop("operation_id", "stream-test"),
            **kwargs,
        ),
    )["id"]


def replay(store, tid):
    text = ""
    for event in store.events(tid):
        if event["kind"] == "text":
            text += event["payload"]["text"]
        elif event["kind"] == "text_reset":
            text = event["payload"]["text"]
    return text


async def test_kemini_keeps_business_tool_progress_and_commits_only_clean_story(tmp_path):
    store, service, session = game(tmp_path, "chat_completions", kemini=True)
    settings = store.settings_document()
    settings.shared.kemini.normal_disclaimer_enabled = True
    settings.shared.kemini.filler_text = "填充末尾，不得省略"
    settings.shared.kemini.transport_prompt = "自定义正文传输指令末尾"
    settings.shared.kemini.tool_description = "自定义工具说明末尾"
    settings.shared.kemini.content_description = "自定义参数说明末尾"
    store.save_settings(settings)
    first = WireStream(*tool_events())
    last = WireStream(
        *tool_events(
            "kemini_emit_story",
            {"content": "<thinking>创作安排</thinking>最终正文<disclaimer>声明</disclaimer>"},
        )
    )
    provider, sent = connect(store, service, first, last, WireStream(terminal()))
    provider.settings.kemini.enabled = True  # Production providers take the Kemini conversation's settings.
    try:
        tid = submit(service, session)
        await service.tasks[tid]
        assert store.turn(tid)["status"] == "committed", store.turn(tid)["error"]
        current = store.session(session["id"])
        message = current["messages"][-1]
        assert message["raw"] == message["model"] == message["display"] == replay(store, tid) == "最终正文"
        assert message["auxiliary"] == [
            {"kind": "thinking", "text": "创作安排"},
            {"kind": "disclaimer", "text": "声明"},
        ]
        assert current_text(current["snapshot"]["full_agent"]) == "已确认的工具进度"
        assert any(m["role"] == "tool" for m in sent[1]["messages"])
        assert any("本轮已输出正文" in m.get("content", "") for m in sent[2]["messages"])
        assert json.dumps(sent[2]["messages"], ensure_ascii=False).count("最终正文") == 1
        kemini = settings.shared.kemini
        prefix = [
            {"role": "user", "content": text}
            for text in (kemini.filler_text, kemini.clear_prompt, kemini.identity_prompt, kemini.info_prompt)
        ]
        assert sent[0]["messages"][:4] == sent[1]["messages"][:4] == prefix
        assert kemini.transport_prompt in json.dumps(sent[0]["messages"], ensure_ascii=False)
        declaration = next(
            t["function"] for t in sent[0]["tools"] if t["function"]["name"] == "kemini_emit_story"
        )
        assert declaration["description"] == kemini.tool_description
        assert declaration["parameters"]["properties"]["content"]["description"] == kemini.content_description
        assert all(m["role"] != "system" for m in sent[0]["messages"])
        assert any(
            e["kind"] == "full_agent_tool_started" and e["payload"].get("name") == "kemini_emit_story"
            for e in store.events(tid)
        )
        preview = store.turn(tid)["preview"]
        assert "kemini_emit_story" in str(preview["tools"])
        assert all(
            row["cache_static"]
            for row in preview["trace"]
            if row["source"] in ("kemini_filler", "kemini_clear", "kemini_identity", "kemini_info")
        )
        sources = [row["source"] for row in preview["trace"]]
        assert sources[:5] == ["kemini_filler", "kemini_clear", "kemini_identity", "kemini_info", "rules"]
        assert (
            sources.index("input") < sources.index("kemini_acknowledgement") < sources.index("kemini_control")
        )
        assert not next(row for row in preview["trace"] if row["source"] == "kemini_acknowledgement")[
            "cache_static"
        ]
        assert any(
            e["kind"] == "kemini_auxiliary" and e["payload"]["items"] == message["auxiliary"]
            for e in store.events(tid)
        )
        assert Store(tmp_path).session(session["id"])["messages"][-1]["auxiliary"] == message["auxiliary"]
    finally:
        await service.close()


async def test_prefix_debug_observes_tool_continuation_and_survives_provider_recreation(tmp_path):
    store, service, session = game(tmp_path, "chat_completions")
    settings = store.settings("full_agent")
    settings.debug_full_agent_prefix = True
    store.save_settings(settings)
    first = WireStream(*tool_events())
    second = WireStream(*story_events("本轮正文"))
    connect(store, service, first, second)
    try:
        tid = submit(service, session)
        await service.tasks[tid]
        assert store.turn(tid)["status"] == "committed", store.turn(tid)["error"]
        reports = [
            event["payload"] for event in store.events(tid) if event["kind"] == "full_agent_prefix_debug"
        ]
        assert len(reports) == 2 and reports[0]["parameters"] == []
        parameters = {item["parameter"]: item for item in reports[1]["parameters"]}
        assert parameters["tools"]["status"] == "identical"
        assert parameters["messages"]["common_prefix_bytes"] > 100
        assert len(service.full.prefix_debug.requests) == 2
        session = store.session(session["id"])
        connect(store, service, WireStream(*story_events("下一轮正文")))
        next_tid = submit(service, session, operation_id="next-prefix-request")
        await service.tasks[next_tid]
        assert store.turn(next_tid)["status"] == "committed", store.turn(next_tid)["error"]
        report = next(e["payload"] for e in store.events(next_tid) if e["kind"] == "full_agent_prefix_debug")
        assert report["parameters"] and "#2 → #3" in report["message"]
        assert len(service.full.prefix_debug.requests) == 2
        settings = store.settings("full_agent")
        settings.debug_full_agent_prefix = False
        store.save_settings(settings)
        session = store.session(session["id"])
        provider, _ = connect(store, service, WireStream(*story_events("关闭后正文")))
        disabled = submit(service, session, operation_id="disabled-prefix-request")
        await service.tasks[disabled]
        assert store.turn(disabled)["status"] == "committed", store.turn(disabled)["error"]
        assert provider.request_observer is None and not service.full.prefix_debug.requests
        assert not any(e["kind"] == "full_agent_prefix_debug" for e in store.events(disabled))
    finally:
        await service.close()
    assert not service.full.prefix_debug.requests


async def test_preview_is_visible_before_completion_and_commit_is_atomic(tmp_path):
    store, service, session = game(tmp_path, "chat_completions")
    gate = Gate()
    usage = {"usage": {"prompt_tokens": 12, "completion_tokens": 4, "total_tokens": 16}}
    stream = WireStream(body_start("尚未完成的"), gate, body_delta("正文。"), *body_end(), usage)
    provider, bodies = connect(store, service, stream)
    try:
        tid = submit(service, session)
        await asyncio.wait_for(gate.reached.wait(), 5)
        assert store.turn(tid)["status"] == "running"
        assert store.turn(tid)["draft"] == replay(store, tid) == "尚未完成的"
        assert store.session(session["id"])["head"] == session["head"]
        assert not any(
            item["message"]["role"] == "assistant" for item in store.full_agent_progress(tid)["new_trace"]
        )
        gate.release.set()
        await service.tasks[tid]
        result, current = store.turn(tid), store.session(session["id"])
        assert result["status"] == "committed", result["error"]
        assert result["draft"] == replay(store, tid) == "尚未完成的正文。"
        assert current["messages"][-1]["raw"] == result["draft"]
        assert len(current["messages"]) == len(session["messages"]) + 2
        assert len(bodies) == len(provider.usage_history) == 1
        assert provider.usage_history[0]["total_tokens"] == 16
        assert stream.closed and provider.http.is_closed
        with pytest.raises(Conflict):
            store.write_turn_draft(tid, "过期回调")
        assert replay(store, tid) == result["draft"]
    finally:
        await service.close()


@pytest.mark.parametrize(
    "tail",
    [
        [],
        [httpx.ReadError("fixture disconnect")],
        [{"error": {"message": "fixture upstream error"}}],
        [terminal(), delta("非法追加")],
        [b"data: {broken\n\n"],
        [terminal("length")],
    ],
    ids=["eof", "read_error", "error_event", "late_content", "invalid_json", "length"],
)
async def test_interface_failure_never_commits_partial_text_or_tools(tmp_path, tail):
    store, service, session = game(tmp_path, "chat_completions")
    stream = WireStream(body_start("未完成片段"), *tail)
    provider, bodies = connect(store, service, stream)
    try:
        tid = submit(service, session)
        await service.tasks[tid]
        result, current = store.turn(tid), store.session(session["id"])
        assert result["status"] == "error", result
        assert result["pending"] is None
        assert result["draft"] == replay(store, tid) == "未完成片段"
        assert current["head"] == session["head"] and current["snapshot"] == session["snapshot"]
        assert len(bodies) == 1  # No silent retry of a paid, partially received request.
        assert not any(e["kind"] == "full_agent_tool_started" for e in store.events(tid))
        assert stream.closed and provider.http.is_closed
    finally:
        await service.close()


async def test_cancelled_preview_and_resume_after_restart_do_not_replay_partial_output(tmp_path):
    store, service, session = game(tmp_path, "chat_completions")
    gate = Gate()
    stream = WireStream(body_start("取消前的残片"), gate)
    provider, _ = connect(store, service, stream)
    tid = submit(service, session)
    try:
        await asyncio.wait_for(gate.reached.wait(), 5)
        await service.cancel(tid)
        assert store.turn(tid)["status"] == "cancelled"
        assert store.turn(tid)["draft"] == "取消前的残片"
        assert stream.closed and provider.http.is_closed
        assert not any(e["kind"] == "full_agent_tool_started" for e in store.events(tid))
    finally:
        await service.close()
    reopened = Store(tmp_path)
    resumed_service = runtime(reopened)
    complete = WireStream(*story_events("重新生成的完整正文"))
    _, sent = connect(reopened, resumed_service, complete)
    try:
        resumed = submit(resumed_service, session, operation_id="resumed", resume_turn_id=tid)
        await resumed_service.tasks[resumed]
        assert reopened.turn(resumed)["status"] == "committed"
        assert "取消前的残片" not in json.dumps(sent, ensure_ascii=False)
        assert not any(m.get("tool_calls") for m in sent[0]["messages"])
        assert reopened.session(session["id"])["messages"][-1]["raw"] == "重新生成的完整正文"
        assert replay(reopened, resumed) == "重新生成的完整正文"
    finally:
        await resumed_service.close()


async def test_story_and_tools_keep_running_until_native_end(tmp_path):
    store, service, session = game(tmp_path, "chat_completions")
    tool_gate, next_gate = Gate(), Gate()
    first = WireStream(delta("唯一正文"), *tool_events()[:1], tool_gate, *tool_events()[1:])
    second = WireStream(next_gate, *tool_events("read_history", {"ref": "H3"}))
    _, sent = connect(store, service, first, second, WireStream(terminal()))
    try:
        tid = submit(service, session)
        await asyncio.wait_for(tool_gate.reached.wait(), 5)
        assert store.turn(tid)["draft"] == "唯一正文"
        assert not any(e["kind"] == "full_agent_tool_started" for e in store.events(tid))
        tool_gate.release.set()
        await asyncio.wait_for(next_gate.reached.wait(), 5)
        assert store.turn(tid)["draft"] == replay(store, tid) == "唯一正文"
        assert any(
            e["kind"] == "full_agent_assistant" and e["payload"].get("is_story") for e in store.events(tid)
        )
        assert store.session(session["id"])["snapshot"] == session["snapshot"]
        assert current_text(store.full_agent_progress(tid)["snapshot"]["full_agent"]) == "已确认的工具进度"
        next_gate.release.set()
        await service.tasks[tid]
        assert store.turn(tid)["status"] == "committed", store.turn(tid)["error"]
        assert store.turn(tid)["draft"] == replay(store, tid) == "唯一正文"
        assert store.session(session["id"])["messages"][-1]["raw"] == "唯一正文"
        assert any(m["role"] == "tool" for m in sent[1]["messages"])
        assert len(sent) == 3
        assert [e["payload"]["name"] for e in store.events(tid) if e["kind"] == "full_agent_tool"] == [
            "write_handoff",
            "read_history",
        ]
    finally:
        await service.close()


async def test_missing_work_stops_and_explicit_resume_keeps_story(tmp_path):
    store, service, session = game(tmp_path, "chat_completions")
    _, sent = connect(
        store,
        service,
        WireStream(*story_events("尚未更新变量的正文")),
    )
    try:
        tid = submit(service, session, update_variables=True)
        await service.tasks[tid]
        assert store.turn(tid)["draft"] == replay(store, tid) == "尚未更新变量的正文"
        assert len(sent) == 1
        assert store.turn(tid)["status"] == "error"
        assert "patch_state" in store.turn(tid)["error"]
        assert store.session(session["id"])["head"] == session["head"]
        model = Model(("patch_state", {"operations": [], "comment": "本轮变量无变化"}), "")
        service.full.provider_factory = lambda: model
        resumed = submit(
            service, session, operation_id="resume-work", resume_turn_id=tid, update_variables=True
        )
        await service.tasks[resumed]
        assert store.turn(resumed)["status"] == "committed", store.turn(resumed)["error"]
        assert len(model.requests) == 2
        assert store.session(session["id"])["messages"][-1]["raw"] == "尚未更新变量的正文"
    finally:
        await service.close()


async def test_responses_terminal_text_replaces_delta_preview_without_duplication(tmp_path):
    store, service, session = game(tmp_path, "responses")
    stream = WireStream(
        {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {
                "type": "message",
                "id": "msg_fixture",
                "role": "assistant",
                "content": [],
            },
        },
        {
            "type": "response.output_text.delta",
            "item_id": "msg_fixture",
            "delta": "临时显示",
        },
        {"type": "response.completed", "response": story_reply("responses", "完整权威正文")},
    )
    connect(store, service, stream, api="responses")
    try:
        tid = submit(service, session)
        await service.tasks[tid]
        assert store.turn(tid)["status"] == "committed", store.turn(tid)["error"]
        assert store.turn(tid)["draft"] == replay(store, tid) == "完整权威正文"
    finally:
        await service.close()


@pytest.mark.parametrize("accept", [False, True])
async def test_final_script_processing_and_review_still_gate_persistence(tmp_path, accept):
    store, service, session = game(tmp_path, "chat_completions")
    session = store.update_session(session["id"], session["revision"], policy="review")

    class ProcessingScript(Script):
        async def call(self, method, *args):
            result = await super().call(method, *args)
            if method == "process":
                result["state"] = {"stat_data": {"energy": 4}}
                result["text"] = "脚本处理后的正文"
            return result

    class ProcessingHost(ScriptHost):
        async def open(self, document, history, snapshot, *args, **kwargs):
            return ProcessingScript(history, snapshot)

    service.full.runtime = ProcessingHost()
    # An explicit native stop can accompany tools. Drain them before callbacks,
    # without paying for an extra response to confirm completion.
    first = WireStream(
        *story_events("模型原始正文")[:-1],
        *tool_events()[:-1],
        terminal(),
    )
    _, requests = connect(store, service, first)
    try:
        tid = submit(service, session)
        await service.tasks[tid]
        assert store.turn(tid)["status"] == "awaiting_review", store.turn(tid)["error"]
        assert len(requests) == 1
        assert store.turn(tid)["draft"] == replay(store, tid) == "脚本处理后的正文"
        assert store.session(session["id"])["snapshot"] == session["snapshot"]
        service.review(tid, accept)
        result, current = store.turn(tid), store.session(session["id"])
        if accept:
            assert result["status"] == "committed"
            assert current["messages"][-1]["raw"] == "脚本处理后的正文"
            assert current["snapshot"]["state"]["stat_data"]["energy"] == 4
        else:
            assert result["status"] == "cancelled" and not result["resumable"]
            assert result["draft"] == replay(store, tid) == ""
            assert current["head"] == session["head"] and current["snapshot"] == session["snapshot"]
    finally:
        await service.close()


@pytest.mark.parametrize("status,unreadable", [(401, False), (429, False), (503, True)])
async def test_http_errors_close_responses_without_retry_or_preview(tmp_path, status, unreadable):
    store, service, session = game(tmp_path, "chat_completions")
    stream = WireStream(httpx.ReadError("error body interrupted") if unreadable else b'{"error":"fixture"}')
    provider, bodies = connect(store, service, stream, status=status)
    try:
        tid = submit(service, session)
        await service.tasks[tid]
        assert store.turn(tid)["status"] == "error"
        assert store.turn(tid)["draft"] == replay(store, tid) == ""
        assert len(bodies) == 1
        assert stream.closed and provider.http.is_closed
        assert store.session(session["id"])["head"] == session["head"]
    finally:
        await service.close()


@pytest.mark.parametrize("repeat_failure", [False, True])
async def test_script_failure_after_complete_stream_does_not_save_story(tmp_path, repeat_failure):
    store, service, session = game(tmp_path, "chat_completions")

    class BrokenScript(Script):
        async def call(self, method, *args):
            if method == "process":
                raise RuntimeError("fixture script failed")
            return await super().call(method, *args)

    class BrokenHost(ScriptHost):
        async def open(self, document, history, snapshot, *args, **kwargs):
            return BrokenScript(history, snapshot)

    service.full.runtime = BrokenHost()
    note = {"key": "本轮场景", "content": "正文中的场景", "kind": "fact", "sources": ["H3"]}
    _, requests = connect(
        store,
        service,
        WireStream(*tool_events("read_history", {"ref": "H999"})),
        WireStream(delta("模型完整正文"), *tool_events("write_note", note)[:-1], terminal()),
    )
    try:
        tid = submit(service, session)
        await service.tasks[tid]
        result = store.turn(tid)
        assert result["status"] == "error" and result["pending"] is None
        assert result["draft"] == replay(store, tid) == "模型完整正文"
        assert store.session(session["id"])["snapshot"] == session["snapshot"]
        progress = store.full_agent_progress(tid)
        output_id = progress["output_id"]
        assert progress["model_completed"]
        if repeat_failure:
            tid = submit(service, session, operation_id="callback-retry", resume_turn_id=tid)
            await service.tasks[tid]
            assert store.turn(tid)["status"] == "error"
            progress = store.full_agent_progress(tid)
            assert progress["story"]["content"] == "模型完整正文" and progress["model_completed"]
            assert progress["output_id"] == output_id
        service.full.runtime = ScriptHost()
        service.full.provider_factory = None
        settings = store.settings_document()
        settings.full_agent.model.model = ""
        settings.shared.providers[0].api_key = ""
        store.save_settings(settings)
        resumed = submit(service, session, operation_id="callback-resume", resume_turn_id=tid)
        await service.tasks[resumed]
        assert store.turn(resumed)["status"] == "committed", store.turn(resumed)["error"]
        current = store.session(session["id"])
        assert current["messages"][-1]["raw"] == "模型完整正文"
        assert current["messages"][-1]["id"] == output_id
        assert current["snapshot"]["full_agent"]["notes"][note["key"]]["sources"] == [
            {"message_id": output_id}
        ]
        assert len(requests) == 2
    finally:
        await service.close()


async def test_terminal_tool_call_with_incomplete_arguments_is_never_executed(tmp_path):
    store, service, session = game(tmp_path, "chat_completions")
    connect(store, service, WireStream(delta("工具前的临时文字"), *tool_events()[:1], terminal("tool_calls")))
    try:
        tid = submit(service, session)
        await service.tasks[tid]
        result = store.turn(tid)
        assert result["status"] == "error" and "JSON" in result["error"]
        assert result["draft"] == replay(store, tid) == "工具前的临时文字"
        assert store.full_agent_progress(tid)["story"] is None
        assert not any(e["kind"] == "full_agent_tool_started" for e in store.events(tid))
        assert store.session(session["id"])["snapshot"] == session["snapshot"]
    finally:
        await service.close()


async def test_empty_successful_stream_is_not_a_story_reply(tmp_path):
    store, service, session = game(tmp_path, "chat_completions")
    settings = store.settings("full_agent")
    settings.full_agent_config.max_steps = 1
    store.save_settings(settings)
    _, requests = connect(store, service, WireStream(terminal()))
    try:
        tid = submit(service, session)
        await service.tasks[tid]
        result = store.turn(tid)
        assert result["status"] == "error" and "尚未输出" in result["error"]
        assert len(requests) == 1
        assert result["draft"] == ""
        assert store.session(session["id"])["head"] == session["head"]
    finally:
        await service.close()


async def test_total_deadline_stops_a_stream_that_keeps_running_after_stop_reason(tmp_path, monkeypatch):
    store, service, session = game(tmp_path, "chat_completions")
    gate = Gate()
    stream = WireStream(*story_events("尚未完整接收的正文"), gate)
    provider, requests = connect(store, service, stream)
    timeouts, original = [], asyncio.timeout

    def capture_timeout(seconds):
        timeout = original(seconds)
        if seconds is None:
            timeouts.append(timeout)
        return timeout

    monkeypatch.setattr(asyncio, "timeout", capture_timeout)
    try:
        tid = submit(service, session)
        await asyncio.wait_for(gate.reached.wait(), 5)
        # No real sleep: advance the existing turn's deadline to this loop tick.
        timeouts[0].reschedule(asyncio.get_running_loop().time())
        await service.tasks[tid]
        result = store.turn(tid)
        assert result["status"] == "error" and result["resumable"]
        assert len(requests) == 1 and stream.closed and provider.http.is_closed
        assert any(e["payload"].get("error_code") == "TIME_BUDGET_EXHAUSTED" for e in store.events(tid))
        assert store.full_agent_progress(tid)["story"] is None
        assert store.session(session["id"])["head"] == session["head"]
        assert not any(e["kind"] == "full_agent_tool_started" for e in store.events(tid))
    finally:
        await service.close()
