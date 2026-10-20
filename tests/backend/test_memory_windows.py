import asyncio
import copy
import json

import pytest

from tavern.conversation.context import compile_context
from tavern.core.models import ContextRecipe, Settings, TurnRequest
from tavern.memories.memory import Tracker, fingerprint, window_history
from tavern.storage.store import Store
from tests.support.memory_fixtures import memory_output, memory_snapshot


def history(n, size=0):
    return [
        {
            "id": f"m{i}",
            "role": "assistant" if i % 2 == 0 else "user",
            "raw": f"原文{i}" + "界" * size,
            "model": f"原文{i}" + "界" * size,
        }
        for i in range(n)
    ]


def session():
    return {
        "id": "s",
        "persona": "用户",
        "persona_data": {},
        "lore_injection_mode": "conversation",
        "recipe": ContextRecipe().model_dump(),
    }


def request(mode="send"):
    return TurnRequest(text="继续", operation_id="a", expected_revision=0, mode=mode)


def config(**changes):
    return Settings(history_message_cap=12, chunk_size=4, memory_interval_turns=2, **changes)


def archiver(calls):
    async def generate(messages, **kwargs):
        payload = json.loads(messages[-1]["content"])
        kind = "memory"
        calls.append((kind, payload))
        if kind == "memory":
            return memory_output(messages), {}
        return json.dumps({"narrative": "过去的剧情", "facts": [{"text": "事实", "source_ids": ["m0"]}]}), {}

    return generate


async def test_periodic_archives_do_not_move_window_and_regeneration_never_maintains(tmp_path):
    manager = Tracker(Store(tmp_path))
    snap = {"state": {}, "memory_document": {"version": 2, "records": []}, "lore": {}}
    calls = []
    await manager.prepare("t", {}, session(), history(9), snap, request(), config(), archiver(calls))
    assert "context_window" not in snap
    assert snap["memory_archive"]["covered_count"] == 9
    assert len(window_history(history(9), snap)[0]) == 9
    original = copy.deepcopy(snap)
    await manager.prepare(
        "t", {}, session(), history(9), snap, request("regenerate"), config(), archiver(calls)
    )
    assert snap == original and len(calls) == 1
    await manager.prepare("t", {}, session(), history(13), snap, request(), config(), archiver(calls))
    assert snap["context_window"]["covered_count"] == 5
    assert [m["id"] for m in window_history(history(13), snap)[0]] == [f"m{i}" for i in range(5, 13)]
    assert calls[1][1]["previous_result"]["records"][0]["content"] == "过去的剧情"
    assert [m["passages"][0]["text"] for m in calls[1][1]["messages"]] == [f"原文{i}" for i in range(9, 13)]
    compiled = compile_context({}, session(), history(13), snap, "当前输入", config(), "seed")
    assert "原文0" not in json.dumps(compiled["messages"], ensure_ascii=False)
    assert "过去的剧情" in json.dumps(compiled["messages"], ensure_ascii=False)


@pytest.mark.parametrize("failure_kind", ["memory"])
async def test_required_archive_failure_preserves_entire_snapshot(tmp_path, failure_kind):
    manager = Tracker(Store(tmp_path))
    snap = {"state": {}, "memory_document": {"version": 2, "records": []}, "lore": {}}
    original = copy.deepcopy(snap)
    good = archiver([])

    async def invalid(messages, **kwargs):
        kind = "memory"
        return ("{invalid json", {}) if kind == failure_kind else await good(messages)

    with pytest.raises(ValueError, match="原窗口"):
        await manager.prepare("t", {}, session(), history(13), snap, request(), config(), invalid)
    assert snap == original
    assert len(window_history(history(13), snap)[0]) == 13


async def test_invalid_critical_evidence_preserves_window(tmp_path):
    manager = Tracker(Store(tmp_path))
    snap = {"state": {}, "memory_document": {"version": 2, "records": []}, "lore": {}}
    original = copy.deepcopy(snap)

    async def summarize(messages, **kwargs):
        return memory_output(messages, invalid=True), {}

    with pytest.raises(ValueError, match="原窗口"):
        await manager.prepare("t", {}, session(), history(13), snap, request(), config(), summarize)
    assert snap == original


async def test_periodic_failure_and_cancel_preserve_original_archives(tmp_path):
    manager = Tracker(Store(tmp_path))
    snap = {"state": {}, "memory_document": {"version": 2, "records": []}, "lore": {}}
    original = copy.deepcopy(snap)

    async def invalid(_, **kwargs):
        return "{invalid json", {}

    await manager.prepare("t", {}, session(), history(9), snap, request(), config(), invalid)
    assert snap.pop("memory_warning")
    assert snap == original

    async def cancelled(_, **kwargs):
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await manager.prepare("t", {}, session(), history(13), snap, request(), config(), cancelled)
    assert snap == original


async def test_periodic_timeout_has_visible_diagnostic(tmp_path):
    store = Store(tmp_path)
    manager = Tracker(store)
    snap = {"state": {}, "memory_document": {"version": 2, "records": []}, "lore": {}}

    async def timed_out(_, **kwargs):
        raise TimeoutError()

    await manager.prepare(
        "timeout-turn", {}, session(), history(9), snap, request(), config(memory_timeout=120), timed_out
    )
    assert snap["memory_warning"] == "记忆模型请求超过 120 秒未完成"
    failed = [event for event in store.events("timeout-turn") if event["kind"] == "memory_failed"]
    assert failed[0]["payload"]["message"] == snap["memory_warning"]


async def test_cap_forces_memory_coverage_before_period(tmp_path):
    manager = Tracker(Store(tmp_path))
    messages = history(13)
    snap = {
        "state": {},
        "memory_document": {"version": 2, "records": []},
        "lore": {},
        "memory_archive": {"covered_count": 1, "fingerprint": fingerprint(messages[:1]), "last_round": 6},
    }
    calls = []
    await manager.prepare("t", {}, session(), messages, snap, request(), config(), archiver(calls))
    assert snap["context_window"]["covered_count"] == 5
    assert snap["memory_archive"]["covered_count"] == 5
    assert len(snap["memory_document"]["records"]) == 1
    assert len(calls) == 1


@pytest.mark.parametrize("mode", ["messages", "tokens"])
async def test_both_enabled_modes_obey_cap(tmp_path, mode):
    snap = {"state": {}, "memory_document": {"version": 2, "records": []}, "lore": {}}
    await Tracker(Store(tmp_path)).prepare(
        "t", {}, session(), history(13), snap, request(), config(chunk_mode=mode), archiver([])
    )
    assert len(window_history(history(13), snap)[0]) + 1 < 12


async def test_token_pressure_trims_early_and_window_off_retains_everything(tmp_path):
    manager = Tracker(Store(tmp_path))
    cfg = Settings(
        context_tokens=10000,
        reply_tokens=128,
        background={"context_tokens": 32768, "reply_tokens": 1024},
        history_message_cap=40,
        chunk_size=10,
        memory_interval_turns=5,
    )
    messages = history(11, size=800)
    snap = {"state": {}, "memory_document": {"version": 2, "records": []}, "lore": {}}
    await manager.prepare("t", {}, session(), messages, snap, request(), cfg, archiver([]))
    assert snap["context_window"]["covered_count"] == 7
    cfg = cfg.model_copy(update={"chunk_mode": "off"})
    calls = []
    await manager.prepare("t", {}, session(), messages, snap, request(), cfg, archiver(calls))
    visible, _ = window_history(messages, snap, enabled=False)
    assert visible == messages
    compiled = compile_context({}, session(), messages, snap, "当前", cfg, "seed", enforce_budget=False)
    assert len([m for m in compiled["trace"] if m["source"] == "history"]) == 11


def test_trimmed_history_keeps_memory_even_when_recipe_block_disabled():
    messages = history(9)
    state = {
        **memory_snapshot("已经发生的重要剧情"),
        "context_window": {"covered_count": 5, "fingerprint": fingerprint(messages[:5])},
    }
    current = session()
    for block in current["recipe"]["blocks"]:
        if block["source"] == "memory":
            block["enabled"] = False
    result = compile_context({}, current, messages, state, "继续", config(), "protected-memory")
    assert any(
        item["source"] == "memory" and "已经发生的重要剧情" in item["content"] for item in result["trace"]
    )
