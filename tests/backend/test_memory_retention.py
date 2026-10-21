"""Age and budget limits preserve protected continuity while bounding active memory."""

import copy
import json
import sqlite3
from contextlib import closing

import pytest

from tavern.core.models import ContextRecipe, Settings, TurnRequest
from tavern.memories.memory import Tracker, fingerprint
from tavern.memories.memory_archive import (
    ARCHIVE,
    RETIRING,
    archive_entry,
    edit_archive,
    inherited_entries,
    read_archive,
    settle_archive,
)
from tavern.memories.memory_engine import consolidate
from tavern.memories.memory_records import Record, RecordEdit, render_document, source_hash
from tavern.memories.memory_retention import retain_memory
from tavern.storage.store import Store, settle_message_archive


def story(rounds):
    return [
        message
        for number in range(1, rounds + 1)
        for message in (
            {"id": f"u{number}", "role": "user", "raw": f"第{number}轮用户", "model": f"第{number}轮用户"},
            {
                "id": f"a{number}",
                "role": "assistant",
                "raw": f"第{number}轮回复",
                "model": f"第{number}轮回复",
            },
        )
    ]


def record(history, rid, category, round_number, *, content=None, **fields):
    message = next(m for m in history if m["id"] == f"a{round_number}")
    return Record(
        id=rid,
        category=category,
        content=content or rid,
        sources=[
            {"message_id": message["id"], "excerpt": message["model"], "fingerprint": source_hash(message)}
        ],
        **fields,
    ).model_dump()


def memory(*records):
    return {"version": 3, "records": list(records)}


def test_current_limit_keeps_newest_automatic_items_and_manual_item():
    history = story(5)
    manual = Record(id="manual", category="current", content="手工固定", manual=True).model_dump()
    original = memory(
        record(history, "c1", "current", 1),
        manual,
        record(history, "c2", "current", 2),
        record(history, "c3", "current", 3),
    )
    result = retain_memory(original, history, Settings(memory_current_max_records=2))
    assert [r["id"] for r in result["records"]] == ["manual", "c2", "c3"]
    assert len(original["records"]) == 4


def test_episode_cooldown_uses_latest_source_and_completed_round_boundary():
    history = story(10)
    old = record(history, "old", "episode", 5, status="completed")
    recent = record(history, "recent", "episode", 6)
    refreshed = record(history, "refreshed", "episode", 1)
    later = next(m for m in history if m["id"] == "a9")
    refreshed["sources"].append(
        {"message_id": later["id"], "excerpt": later["model"], "fingerprint": source_hash(later)}
    )
    callback = record(history, "callback", "episode", 5, callback=True)
    items = memory(old, recent, refreshed, callback)
    result = retain_memory(items, history, Settings(memory_episode_cooldown_rounds=5))
    assert [r["id"] for r in result["records"]] == ["recent", "refreshed"]
    assert [r["id"] for r in result["retired"]] == ["old"]
    assert len(retain_memory(items, history, Settings(memory_episode_cooldown_rounds=0))["records"]) == 4


def test_budget_exits_oldest_episode_but_keeps_open_uncertain_and_manual():
    history = story(3)
    original = memory(
        record(history, "old", "episode", 1, content="旧" * 360),
        record(history, "new", "episode", 2, content="新" * 360),
        record(history, "open", "open", 2, content="还需处理"),
        record(history, "uncertain", "uncertain", 3, content="尚待核实"),
        Record(id="manual", category="manual", content="手工保留", manual=True).model_dump(),
    )
    result = retain_memory(
        original, history, Settings(memory_capacity_tokens=512, memory_episode_cooldown_rounds=0)
    )
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
    result = retain_memory(
        memory(stale, manual, expired), history, Settings(memory_episode_cooldown_rounds=20)
    )
    assert [r["id"] for r in result["records"]] == ["stale", "manual"]


async def test_question_target_is_marked_uncertain_before_current_cap_runs():
    async def generate(prompt, **kwargs):
        return json.dumps(
            {
                "operations": [
                    {
                        "op": "add",
                        "record": {"category": "current", "content": f"进展{i}", "sources": ["H1#1"]},
                    }
                    for i in range(6)
                ],
                "questions": [{"topic": "knowledge", "query": "第一条是否确定", "record_ids": ["N1"]}],
            }
        )

    result = await consolidate(story(1), Settings(memory_review_enabled=False), generate, memory=memory())
    assert len(result["records"]) == 6
    assert sum(r["category"] == "current" for r in result["records"]) == 5
    assert result["records"][0]["category"] == "uncertain"


async def test_retention_runs_on_send_even_when_no_tracker_batch_is_due(tmp_path):
    history = story(4)
    snap = {
        "state": {},
        "memory_document": memory(record(history, "old", "episode", 1), record(history, "new", "episode", 4)),
        "memory_archive": {
            "covered_count": len(history),
            "fingerprint": fingerprint(history),
            "last_round": 4,
        },
    }
    session = {
        "id": "s",
        "persona": "用户",
        "persona_data": {},
        "recipe": ContextRecipe().model_dump(),
        "lore_injection_mode": "conversation",
    }
    calls = []

    async def generate(*args, **kwargs):
        calls.append(1)
        raise AssertionError("retention needs no model call")

    await Tracker(Store(tmp_path)).prepare(
        "t",
        {},
        session,
        history,
        snap,
        TurnRequest(text="继续", operation_id="test", expected_revision=0),
        Settings(memory_interval_turns=100, memory_episode_cooldown_rounds=3),
        generate,
    )
    assert not calls
    assert [r["id"] for r in snap["memory_document"]["records"]] == ["new"]
    # The retired episode leaves the projection for the archive, unchanged.
    assert [(e["id"], e["content"], e["archived_round"]) for e in snap[RETIRING]] == [("old", "old", 4)]


def test_archive_delta_is_settled_against_the_branch_chain():
    history = story(3)
    old = archive_entry(record(history, "old", "episode", 1), 1)
    newer = archive_entry(record(history, "newer", "episode", 2), 2)
    history[1]["snapshot"] = {ARCHIVE: [old]}
    # A draft carries a1's delta; only what this turn retired is persisted.
    draft = settle_archive({ARCHIVE: [old], RETIRING: [newer]}, inherited_entries(history[:2]))
    assert draft == {ARCHIVE: [newer]}
    assert ARCHIVE not in settle_archive({ARCHIVE: [old]}, inherited_entries(history[:2]))
    # A sibling version of a1 (parent u1) keeps a1's own retirement.
    assert settle_archive({ARCHIVE: [old]}, inherited_entries(history[:1])) == {ARCHIVE: [old]}
    history[3]["snapshot"] = draft
    assert [e["id"] for e in read_archive(history[:4])] == ["old", "newer"]
    head = copy.deepcopy(draft)
    edit_archive(head, history[:4], RecordEdit(expected_revision=0, action="delete", id="old"))
    history[3]["snapshot"] = settle_archive(head, inherited_entries(history[:3]))
    assert [e["id"] for e in read_archive(history[:4])] == ["newer"]
    # A fork made before the delete still sees the entry.
    assert [e["id"] for e in read_archive(history[:2])] == ["old"]
    with closing(sqlite3.connect(":memory:")) as db:
        db.row_factory = sqlite3.Row
        db.execute("CREATE TABLE messages(id TEXT PRIMARY KEY, session TEXT, parent TEXT, snapshot TEXT)")
        for index, message in enumerate(history):
            db.execute(
                "INSERT INTO messages VALUES (?,?,?,?)",
                (
                    message["id"],
                    "s",
                    history[index - 1]["id"] if index else None,
                    json.dumps(message.get("snapshot", {})),
                ),
            )
        queries = []
        db.set_trace_callback(queries.append)
        assert settle_message_archive(db, "s", history[-1]["id"], {"state": {}}) == {"state": {}}
        assert queries == []
        # The nearest delta wins, including a deletion; a sibling keeps its own delta.
        carried = history[3]["snapshot"]
        assert settle_message_archive(db, "s", history[3]["id"], copy.deepcopy(carried)) == {}
        assert settle_message_archive(db, "s", history[2]["id"], copy.deepcopy(carried)) == carried
