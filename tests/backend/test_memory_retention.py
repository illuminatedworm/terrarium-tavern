"""Age and budget limits preserve protected continuity while bounding active memory."""

import json

import pytest

from tavern.core.models import ContextRecipe, Settings, TurnRequest
from tavern.memories.memory import Tracker, fingerprint
from tavern.memories.memory_engine import consolidate
from tavern.memories.memory_records import Record, render_document, source_hash
from tavern.memories.memory_retention import retain_memory
from tavern.storage.store import Store


def story(rounds):
    return [message for number in range(1, rounds + 1) for message in (
        {"id": f"u{number}", "role": "user", "raw": f"第{number}轮用户", "model": f"第{number}轮用户"},
        {"id": f"a{number}", "role": "assistant", "raw": f"第{number}轮回复", "model": f"第{number}轮回复"})]


def record(history, rid, category, round_number, *, content=None, **fields):
    message = next(m for m in history if m["id"] == f"a{round_number}")
    return Record(id=rid, category=category, content=content or rid,
        sources=[{"message_id": message["id"], "excerpt": message["model"],
                  "fingerprint": source_hash(message)}], **fields).model_dump()


def memory(*records):
    return {"version": 1, "records": list(records)}


def test_current_limit_keeps_newest_automatic_items_and_manual_item():
    history = story(5)
    manual = Record(id="manual", category="current", content="手工固定", manual=True).model_dump()
    original = memory(record(history, "c1", "current", 1), manual,
                      record(history, "c2", "current", 2), record(history, "c3", "current", 3))
    result = retain_memory(original, history, Settings(memory_current_max_records=2))
    assert [r["id"] for r in result["records"]] == ["manual", "c2", "c3"]
    assert len(original["records"]) == 4


def test_episode_cooldown_uses_latest_source_and_completed_round_boundary():
    history = story(10)
    old = record(history, "old", "episode", 5, status="completed")
    recent = record(history, "recent", "episode", 6)
    refreshed = record(history, "refreshed", "episode", 1)
    later = next(m for m in history if m["id"] == "a9")
    refreshed["sources"].append({"message_id": later["id"], "excerpt": later["model"],
                                 "fingerprint": source_hash(later)})
    items = memory(old, recent, refreshed)
    result = retain_memory(items, history, Settings(memory_episode_cooldown_rounds=5))
    assert [r["id"] for r in result["records"]] == ["recent", "refreshed"]
    assert len(retain_memory(items, history, Settings(memory_episode_cooldown_rounds=0))["records"]) == 3


def test_budget_exits_oldest_episode_but_keeps_open_uncertain_and_manual():
    history = story(3)
    original = memory(record(history, "old", "episode", 1, content="旧" * 360),
                      record(history, "new", "episode", 2, content="新" * 360),
                      record(history, "open", "open", 2, content="还需处理"),
                      record(history, "uncertain", "uncertain", 3, content="尚待核实"),
                      Record(id="manual", category="manual", content="手工保留", manual=True).model_dump())
    result = retain_memory(original, history, Settings(memory_capacity_tokens=512,
                                                        memory_episode_cooldown_rounds=0))
    assert "old" not in [r["id"] for r in result["records"]]
    assert {"open", "uncertain", "manual"} <= {r["id"] for r in result["records"]}
    assert "新" * 360 in render_document(result)


def test_capacity_still_fails_when_only_protected_items_remain():
    history = story(1)
    protected = memory(record(history, "open", "open", 1, content="未完成" * 300))
    with pytest.raises(ValueError, match="可退出的经历已用尽"):
        retain_memory(protected, history, Settings(memory_capacity_tokens=512))


def test_changed_source_and_manual_episode_are_not_expired():
    history = story(25)
    stale = record(history, "stale", "episode", 1)
    stale["sources"][0]["fingerprint"] = "changed"
    manual = record(history, "manual", "episode", 1, manual=True)
    expired = record(history, "expired", "episode", 1)
    result = retain_memory(memory(stale, manual, expired), history,
                           Settings(memory_episode_cooldown_rounds=20))
    assert [r["id"] for r in result["records"]] == ["stale", "manual"]


async def test_question_target_is_marked_uncertain_before_current_cap_runs():
    async def generate(prompt, **kwargs):
        return json.dumps({"operations": [
            {"op": "add", "record": {"category": "current", "content": f"进展{i}", "sources": ["S1"]}}
            for i in range(6)], "questions": [
            {"topic": "knowledge", "query": "第一条是否确定", "record_ids": ["N1"]}]})

    result = await consolidate(story(1), Settings(memory_review_enabled=False), generate, memory=memory())
    assert len(result["records"]) == 6
    assert sum(r["category"] == "current" for r in result["records"]) == 5
    assert result["records"][0]["category"] == "uncertain"


async def test_retention_runs_on_send_even_when_no_tracker_batch_is_due(tmp_path):
    history = story(4)
    snap = {"state": {}, "memory_document": memory(record(history, "old", "episode", 1),
                                                    record(history, "new", "episode", 4)),
            "memory_archive": {"covered_count": len(history), "fingerprint": fingerprint(history),
                               "last_round": 4}}
    session = {"id": "s", "persona": "旅人", "persona_data": {}, "recipe": ContextRecipe().model_dump(),
               "lore_injection_mode": "conversation"}
    calls = []

    async def generate(*args, **kwargs):
        calls.append(1)
        raise AssertionError("retention needs no model call")

    await Tracker(Store(tmp_path)).prepare("t", {}, session, history, snap,
        TurnRequest(text="继续", operation_id="test", expected_revision=0),
        Settings(memory_interval_turns=100, memory_episode_cooldown_rounds=3), generate)
    assert not calls
    assert [r["id"] for r in snap["memory_document"]["records"]] == ["new"]
