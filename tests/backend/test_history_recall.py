import copy
import json
from types import SimpleNamespace

import httpx
import pytest
from kohakuterrarium.llm.base import NativeToolCall
from openai import AsyncOpenAI

from tavern.core.models import Settings
from tavern.core.tokens import count_text
from tavern.memories.history_recall import (
    HISTORY_TOOL,
    HistoryBudget,
    HistoryRecall,
    MemoryText,
    complete_with_recall,
)
from tavern.memories.memory_engine import CallBudget, apply_operations, consolidate
from tavern.scripting.agent import Writer, make_provider


def history():
    return [{"id": f"m{i}", "role": "assistant" if i == 0 or i % 2 == 0 else "user",
             "raw": text, "model": text, "snapshot": {"state": {}}}
            for i, text in enumerate(["开场见面", "把银戒借我", "艾琳借出银戒，并约定归还。",
                                       "我们走吧", "两人来到海岸。", "这是尚未提交回复的输入"])]


def request(messages=None):
    return [{"role": "system", "content": "最终只返回operations/questions"},
            {"role": "user", "content": json.dumps({"messages": [
                {"id": m["id"], "role": m["role"], "text": m["model"]} for m in (messages or history()[3:5])]})}]


def call(index, args):
    return NativeToolCall(str(index), "tavern_recall_history", json.dumps(args))


class Provider:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.last_tool_calls = []
        self.last_assistant_extra_fields = {}
        self.calls = []

    async def chat_complete(self, messages, **kwargs):
        self.calls.append((copy.deepcopy(messages), kwargs))
        text, self.last_tool_calls = next(self.responses)
        return SimpleNamespace(content=text, usage={"prompt_tokens": 100, "completion_tokens": 10})

    async def close(self):
        pass


def test_search_then_read_preserves_quotes_roles_and_three_shared_attempts():
    budget = HistoryBudget()
    recall = HistoryRecall(history(), Settings(), budget)
    original = copy.deepcopy(history())
    found = recall.execute({"query": "银戒 艾琳"})
    assert found["matches"][0]["turn"] == 2
    read = recall.execute({"turn": 2, "radius": 0})
    assert [m["role"] for m in read["rounds"]] == ["user", "assistant"]
    assert "约定归还" in "".join(p["text"] for p in read["rounds"][1]["passages"])
    assert not recall.execute({"query": "不存在的事情"})["found"]
    denied = recall.execute({"turn": 1})
    assert not denied["rounds"] and "3次上限" in denied["notice"]
    assert budget.calls == 3 and history() == original
    assert not any("尚未提交" in m.get("text", "") for m in read["rounds"])


def test_invalid_empty_and_duplicate_queries_count_and_branch_argument_is_rejected():
    budget = HistoryBudget()
    recall = HistoryRecall(history(), Settings(), budget)
    assert "参数错误" in recall.execute('{broken')["notice"]
    assert "参数错误" in recall.execute({"query": "银戒", "session": "another"})["notice"]
    assert recall.execute({"query": "银戒"})["found"]
    assert not recall.execute({"query": "银戒"})["found"]
    assert budget.calls == 3


def test_returned_text_has_token_ceiling_and_excludes_unseen_text():
    messages = history()[:3]
    messages[-1]["model"] = "原文很长" * 5000 + "隐藏尾句"
    recall = HistoryRecall(messages, Settings(), HistoryBudget())
    result = recall.execute({"turn": 2, "radius": 0})
    assert count_text(json.dumps(result, ensure_ascii=False, separators=(",", ":")), Settings()) <= 2048
    assert result["rounds"][-1]["truncated"]
    assert "隐藏尾句" not in "".join(p["text"] for p in result["rounds"][-1]["passages"])
    assert "隐藏尾句" not in recall.sources["m2"]["exposed_excerpts"][0]
    operation = {"op": "add", "id": "unseen", "record": {"category": "episode", "content": "编造的引用",
        "sources": [{"message_id": "m2", "excerpt": "隐藏尾句"}]}}
    with pytest.raises(ValueError, match="返回片段"):
        apply_operations({"version": 1, "records": []}, {"operations": [operation]},
            {m["id"]: m for m in messages}, recall.sources, [], fresh_ids={"m2"}, settings=Settings())


async def test_three_tools_then_final_response_and_no_future_batch_visibility():
    provider = Provider([
        ("", [call(1, {"query": "银戒"})]), ("", [call(2, {"turn": 2, "radius": 0})]),
        ("", [call(3, {"query": "不存在"})]), ('{"operations":[],"questions":[]}', []),
    ])
    messages = history()[:3]
    budget = HistoryBudget()
    events = []
    result = await complete_with_recall(provider, request(messages), history=history(), settings=Settings(),
        budget=budget, phase="memory", event=lambda k, p: events.append((k, p)))
    assert isinstance(result, MemoryText) and "m2" in result.history_sources
    assert budget.calls == budget.continuations == 3
    assert len(provider.calls) == 4
    assert provider.calls[-1][1]["extra_body"]["tool_choice"] == "none"
    assert "海岸" not in json.dumps(provider.calls[-1][0], ensure_ascii=False)
    assert len([p for k, p in events if k == "usage"]) == 4


async def test_parallel_attempts_cannot_execute_fourth_and_quota_spans_batches():
    budget = HistoryBudget()
    provider = Provider([("", [call(i, {"query": f"词{i}"}) for i in range(1, 5)]),
                         ('{"operations":[],"questions":[]}', [])])
    await complete_with_recall(provider, request(), history=history(), settings=Settings(), budget=budget, phase="memory")
    tool_results = [m for m in provider.calls[-1][0] if m["role"] == "tool"]
    assert len(tool_results) == 4 and "3次上限" in tool_results[-1]["content"]
    assert budget.calls == 3
    later = Provider([("", [call(5, {"turn": 1})])])
    with pytest.raises(ValueError, match="已停用"):
        await complete_with_recall(later, request(), history=history(), settings=Settings(), budget=budget, phase="memory_review")
    assert budget.calls == 3


async def test_loop_prohibits_unknown_tools():
    provider = Provider([("", [NativeToolCall("1", "write_memory", "{}")])])
    with pytest.raises(ValueError, match="仅允许"):
        await complete_with_recall(provider, request(), history=history(), settings=Settings(), budget=HistoryBudget(), phase="memory")


async def test_cancellation_during_followup_does_not_advance_memory():
    import asyncio
    provider = Provider([("", [call(1, {"query": "银戒"})])])
    original = provider.chat_complete
    async def cancel_after_tool(messages, **kwargs):
        if any(m["role"] == "tool" for m in messages):
            raise asyncio.CancelledError()
        return await original(messages, **kwargs)
    provider.chat_complete = cancel_after_tool
    with pytest.raises(asyncio.CancelledError):
        await complete_with_recall(provider, request(), history=history(), settings=Settings(), budget=HistoryBudget(), phase="memory")


async def test_recalled_source_can_supplement_but_cannot_replace_fresh_source():
    messages = history()[:3]
    messages += [{"id": f"m{i}", "role": "user" if i % 2 else "assistant", "raw": "继续前进", "model": "继续前进"}
                 for i in range(3, 10)]
    messages.append({"id": "m10", "role": "assistant", "raw": "两人来到海岸。", "model": "两人来到海岸。"})
    operation = {"op": "add", "id": "journey", "record": {"category": "episode", "content": "带着借来的银戒到了海岸",
        "sources": [{"message_id": "m2", "excerpt": "艾琳借出银戒"}, {"message_id": "m10", "excerpt": "两人来到海岸。"}]}}
    cfg, budget = Settings(), CallBudget()
    wire = copy.deepcopy(operation)
    wire["record"]["sources"] = ["H1", "S2"]
    provider = Provider([("", [call(1, {"query": "银戒"})]), (json.dumps({"operations": [wire]}), [])])
    async def generate(prompt, **kw):
        payload = json.loads(prompt[-1]["content"])
        assert "m2" not in {prompt.references.messages[m["id"]]
                            for m in payload["background_messages"] + payload["messages"]}
        return await complete_with_recall(provider, prompt, history=messages, settings=cfg,
                                         budget=budget.history, phase=kw["phase"])
    result = await consolidate(messages, cfg, generate, memory={"version": 1, "records": []}, start=9, budget=budget)
    assert len(result["records"][0]["sources"]) == 2
    operation["record"]["sources"] = operation["record"]["sources"][:1]
    by_id = {m["id"]: m for m in messages}
    with pytest.raises(ValueError, match="本批新增"):
        apply_operations({"version": 1, "records": []}, {"operations": [operation]}, by_id, by_id, [],
                         fresh_ids={"m10"}, settings=cfg)


async def test_no_headroom_disables_optional_tool_without_losing_batch():
    provider = Provider([('{"operations":[],"questions":[]}', [])])
    cfg = Settings(memory_input_tokens=4096, memory_evidence_tokens=0)
    budget = HistoryBudget()
    result = await complete_with_recall(provider, request(), history=history(), settings=cfg, budget=budget, phase="memory")
    assert result and budget.calls == 0
    assert provider.calls[0][1]["extra_body"]["tool_choice"] == "none"


async def test_native_tool_definition_reaches_http_and_reasoning_roundtrip():
    provider = await make_provider(Settings(api_key="fixture", model="fixture", use_proxy=False))
    await provider._client.close()
    captured = []
    def route(request):
        payload = json.loads(request.content)
        captured.append(payload)
        if len(captured) == 1:
            message = {"role": "assistant", "content": None, "reasoning_content": "需要核对旧约定",
                       "tool_calls": [{"id": "lookup", "type": "function", "function": {
                           "name": "tavern_recall_history", "arguments": '{"query":"银戒"}'}}]}
        else:
            message = {"role": "assistant", "content": '{"operations":[],"questions":[]}'}
        return httpx.Response(200, json={"id": "test", "object": "chat.completion", "created": 0,
            "model": "fixture", "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})
    provider._client = AsyncOpenAI(api_key="fixture", http_client=httpx.AsyncClient(transport=httpx.MockTransport(route)))
    try:
        await complete_with_recall(provider, request(), history=history(), settings=Settings(), budget=HistoryBudget(), phase="memory")
        assert captured[0]["tools"] == [HISTORY_TOOL]
        assert captured[0]["tool_choice"] == "auto"
        assert captured[1]["messages"][-2]["reasoning_content"] == "需要核对旧约定"
        assert captured[1]["messages"][-1]["tool_call_id"] == "lookup"
    finally:
        await provider.close()


async def test_provider_request_timeout_is_opt_out_per_call():
    settings = Settings(api_key="fixture", model="fixture", use_proxy=False)
    ordinary = await make_provider(settings)
    memory = await make_provider(settings, request_timeout=None)
    try:
        assert ordinary._client._client.timeout.read == 120
        assert memory._client._client.timeout.read is None
    finally:
        await ordinary.close()
        await memory.close()


async def test_maintenance_alias_shares_the_actual_recall_budget(tmp_path):
    from tavern.core.models import SessionCreate, TurnRequest
    from tavern.storage.store import Store
    store = Store(tmp_path)
    card = store.import_card(b'{"name":"a","first_mes":"hello"}', 'c.json')
    session = store.new_session(SessionCreate(card_id=card["id"]))
    agent = Writer(store, "test", {}, session, session["messages"], session["snapshot"],
                   TurnRequest(text="next", operation_id="a", expected_revision=0), None)
    registered = {tool.tool_name: tool for tool in agent.tools()}
    for name, args in [("tavern_recall_history", {"query": "hello"}), ("tavern_recall_history", {"turn": 1}),
                       ("tavern_recall_history", {"query": "not-found"})]:
        result = await registered[name].execute(args)
        assert result.success
    denied = await registered["tavern_recall_history"].execute({"turn": 1})
    assert "3次上限" in denied.get_text_output()
    assert agent.history_recall_budget.calls == 3


async def test_manual_runtime_logs_each_model_request_once_and_commits_only_final(tmp_path):
    from tavern.application.api import create_app
    from tavern.core.models import SessionCreate, TurnRequest
    app = create_app(tmp_path)
    store, service = app.state.store, app.state.service
    card = store.import_card('{"name":"艾琳","first_mes":"艾琳借出银戒。"}'.encode(), 'c.json')
    session = store.new_session(SessionCreate(card_id=card["id"]))
    provider = Provider([("", [call(1, {"query": "银戒"})]), ("", [call(2, {"turn": 1, "radius": 0})]),
                         ("", [call(3, {"query": "承诺"})]), ('{"operations":[],"questions":[]}', [])])
    service.memory_provider_factory = lambda: provider
    try:
        turn = service.submit(session["id"], TurnRequest(operation_id="rebuild", expected_revision=0, scene_refresh="memory"))
        await service.tasks[turn["id"]]
        assert store.turn(turn["id"])["status"] == "committed", store.turn(turn["id"])["error"]
        events = store.events(turn["id"])
        assert [e["payload"]["used"] for e in events if e["kind"] == "memory_history_recall"] == [1, 2, 3]
        assert len([e for e in events if e["kind"] == "usage"]) == 4
        assert len([e for e in events if e["kind"] == "committed"]) == 1
        assert store.session(session["id"])["snapshot"]["memory_archive"]["covered_count"] == 1
    finally:
        await service.close()
