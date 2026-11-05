import copy
import json
from types import SimpleNamespace

from kohakuterrarium.llm.base import NativeToolCall

from tavern.core.models import Settings
from tavern.memories.history_recall import (
    HistoryBudget,
    HistoryRecall,
)
from tavern.memories.memory_protocol import exposed_excerpts, split_passages
from tavern.scripting.agent import make_provider


def history():
    return [
        {
            "id": f"m{i}",
            "role": "assistant" if i == 0 or i % 2 == 0 else "user",
            "raw": text,
            "model": text,
            "snapshot": {"state": {}},
        }
        for i, text in enumerate(
            [
                "开场见面",
                "把银戒借我",
                "艾琳借出银戒，并约定归还。",
                "我们走吧",
                "两人来到海岸。",
                "这是尚未提交回复的输入",
            ]
        )
    ]


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
    assert [item["ref"] for item in found["matches"]] == ["H2", "H3"]
    assert found["matches"][1]["passages"] == [{"ref": "H3#1", "text": "艾琳借出银戒，并约定归还。"}]
    read = recall.execute({"ref": "H3", "radius": 0})
    assert [m["role"] for m in read["rounds"]] == ["user", "assistant"]
    assert "约定归还" in "".join(p["text"] for p in read["rounds"][1]["passages"])
    assert not recall.execute({"query": "不存在的事情"})["found"]
    denied = recall.execute({"ref": "H1"})
    assert not denied["rounds"] and "3次上限" in denied["notice"]
    assert budget.calls == 3 and history() == original
    assert not any("尚未提交" in m.get("text", "") for m in read["rounds"])


def test_invalid_empty_and_duplicate_queries_count_and_branch_argument_is_rejected():
    budget = HistoryBudget()
    recall = HistoryRecall(history(), Settings(), budget)
    assert "参数错误" in recall.execute("{broken")["notice"]
    assert "参数错误" in recall.execute({"query": "银戒", "session": "another"})["notice"]
    assert recall.execute({"query": "银戒"})["found"]
    assert not recall.execute({"query": "银戒"})["found"]
    assert budget.calls == 3


def test_returned_history_includes_full_long_text():
    messages = history()[:3]
    messages[-1]["model"] = "原文很长" * 5000 + "隐藏尾句"
    recall = HistoryRecall(messages, Settings(), HistoryBudget())
    parts = list(split_passages(messages[-1]["model"]))
    [hit] = recall.execute({"query": "隐藏尾句"})["matches"]
    # A search hit is the complete passage holding the match, numbered within H3.
    assert hit["partial"] and hit["passages"] == [{"ref": f"H3#{len(parts)}", "text": parts[-1]}]
    result = recall.execute({"ref": "H3", "radius": 0})
    assert "partial" not in result["rounds"][-1]
    assert "隐藏尾句" in "".join(p["text"] for p in result["rounds"][-1]["passages"])
    assert exposed_excerpts(recall.sources["m2"]) == parts


def test_search_limit_counts_history_segments_and_radius_zero_reads_only_center():
    recall = HistoryRecall(history(), Settings(), HistoryBudget())
    result = recall.execute({"query": "银戒", "limit": 1})
    assert [item["ref"] for item in result["matches"]] == ["H2", "H3"]
    assert [item["role"] for item in result["matches"]] == ["user", "assistant"]
    centered = recall.execute({"ref": "H2", "radius": 0})
    assert [item["ref"] for item in centered["rounds"]] == ["H2", "H3"]
    edge = HistoryRecall(history(), Settings(), HistoryBudget()).execute({"ref": "H1", "radius": 2})
    assert [item["ref"] for item in edge["rounds"]] == ["H1", "H2", "H3", "H4", "H5"]
    pending = HistoryRecall(history(), Settings(), HistoryBudget()).execute({"ref": "H6"})
    assert not pending["rounds"] and "H1-H5" in pending["error"]
    messages = history()[:5]
    for message, text in zip(messages[1:], ("foobar", "done", "foo", "bar"), strict=True):
        message.update(raw=text, model=text)
    result = HistoryRecall(messages, Settings(), HistoryBudget()).execute({"query": "foobar", "limit": 1})
    assert [item["ref"] for item in result["matches"]] == ["H2"]


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
    from tavern.core.models import SessionCreate
    from tavern.storage.store import Store

    store = Store(tmp_path)
    card = store.import_card(b'{"name":"a","first_mes":"hello"}')
    session = store.new_session(SessionCreate(card_id=card["id"]))
    from tavern.tracker.task import TrackerTask

    agent = TrackerTask(
        store,
        "test",
        store.settings(),
        session,
        card["document"],
        session["messages"],
        session["snapshot"],
        phase="pre",
    )
    for args in ({"query": "hello"}, {"ref": "H1"}, {"query": "not-found"}):
        assert isinstance(agent.execute("tavern_recall_history", args), dict)
    denied = agent.execute("tavern_recall_history", {"ref": "H1"})
    assert "3次上限" in denied["notice"]
    assert agent.history_budget.calls == 3


async def test_manual_runtime_logs_each_model_request_once_and_commits_only_final(tmp_path):
    from tavern.application.api import create_app
    from tavern.core.models import SessionCreate, TurnRequest

    app = create_app(tmp_path)
    store, service = app.state.store, app.state.service
    card = store.import_card('{"name":"艾琳","first_mes":"艾琳借出银戒。"}'.encode())
    session = store.new_session(SessionCreate(card_id=card["id"]))
    provider = Provider(
        [
            ("", [call(1, {"query": "银戒"})]),
            ("", [call(2, {"ref": "H1", "radius": 0})]),
            ("", [call(3, {"query": "承诺"})]),
            ("", [NativeToolCall("write", "write_memory", '{"operations":[],"questions":[]}')]),
            ("done", []),
        ]
    )
    service.memory_provider_factory = lambda: provider
    try:
        turn = service.submit(
            session["id"], TurnRequest(operation_id="rebuild", expected_revision=0, scene_refresh="memory")
        )
        await service.tasks[turn["id"]]
        assert store.turn(turn["id"])["status"] == "committed", store.turn(turn["id"])["error"]
        events = store.events(turn["id"])
        assert [e["payload"]["used"] for e in events if e["kind"] == "memory_history_recall"] == [1, 2, 3]
        assert len([e for e in events if e["kind"] == "usage"]) == 5
        assert len([e for e in events if e["kind"] == "committed"]) == 1
        assert store.session(session["id"])["snapshot"]["memory_archive"]["covered_count"] == 1
    finally:
        await service.close()
