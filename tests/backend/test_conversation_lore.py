import copy
import json

import pytest

from tavern.application.service import RoleplayRuntime
from tavern.character.cards import _LORE_OPTIONAL_FIELDS
from tavern.conversation.context import compile_context
from tavern.core.models import ContextRecipe, SessionCreate, TurnRequest
from tavern.core.models import Settings as AppSettings
from tavern.full_agent.context import compile_window
from tavern.memories.memory import fingerprint
from tavern.storage.store import Store, dump


def Settings(**values):
    values.setdefault("chunk_mode", "messages")
    return AppSettings(retrieval={"channels": ["keyword"]}, **values)


def document(**entry):
    return {
        "name": "角色",
        "character_book": {
            "entries": [
                {"id": "e", "keys": ["灯塔"], "content": "灯塔：{{getvar::place}}", "cooldown": 4, **entry}
            ]
        },
    }


def message(mid, role, raw):
    return {"id": mid, "role": role, "raw": raw, "model": raw, "retrieval_query": raw}


def session(mode="conversation"):
    return {"id": "s", "persona": "玩家", "lore_injection_mode": mode, "recipe": ContextRecipe().model_dump()}


def compile(doc, history, snapshot, text="灯塔", **kwargs):
    before = copy.deepcopy(snapshot)
    result = compile_context(
        doc, session(), history, snapshot, text, Settings(), "seed", input_message_id="new-user", **kwargs
    )
    assert snapshot == before
    return result


def test_card_description_is_labeled_even_when_it_describes_a_world():
    doc = {"name": "旁白", "description": "这座城每晚移动。", "personality": "语气平静"}
    result = compile(doc, [], {"state": {}}, text="继续")
    character = next(item["content"] for item in result["trace"] if item["source"] == "character")
    assert "<card_description>\n这座城每晚移动。\n</card_description>" in character
    assert any(
        item["content"] == "<card_personality>\n语气平静\n</card_personality>" for item in result["trace"]
    )
    empty = compile({"name": "旁白"}, [], {"state": {}}, text="继续")
    assert all("<card_description>" not in item["content"] for item in empty["trace"])


@pytest.mark.parametrize("greeting", ["欢迎", ""])
def test_first_turn_uses_greeting_or_user_anchor_and_system_role(greeting):
    history = [message("g", "assistant", greeting)]
    out = compile(document(position=7, role="assistant", depth=10), history, {"state": {"place": "北岸"}})
    record = out["next_lore_injections"][0]
    assert record["anchor_id"] == ("g" if greeting else "new-user")
    i = next(i for i, item in enumerate(out["trace"]) if item.get("lore_ids") == ["e"])
    assert out["trace"][i]["role"] == "system"
    assert out["trace"][i + 1]["message_id"] == record["anchor_id"]
    assert out["trace"][i]["content"] == "<lorebook>\n灯塔：北岸\n</lorebook>"
    assert all(set(m) == {"role", "content"} for m in out["messages"])


def test_later_turn_and_repeated_trigger_keep_frozen_original_injection():
    hist = [
        message("g", "assistant", "欢迎"),
        message("u1", "user", "继续"),
        message("a1", "assistant", "好"),
    ]
    snap = {"state": {"place": "北岸"}}
    first = compile(document(position=0), hist, snap)
    record = first["next_lore_injections"][0]
    assert record["anchor_id"] == "new-user"
    snap.update(
        lore=first["next_lore"], lore_injections=first["next_lore_injections"], state={"place": "南岸"}
    )
    hist += [message("new-user", "user", "灯塔"), message("a2", "assistant", "继续")]
    out = compile(document(content="已修改正文", probability=0), hist, snap)
    assert out["next_lore_injections"] == [record]
    assert [m["content"] for m in out["trace"] if m.get("lore_ids") == ["e"]] == [
        "<lorebook>\n灯塔：北岸\n</lorebook>"
    ]
    assert not any("recovery_roll" in n for n in out["diagnostics"])
    assert out["next_lore"] == {"e": {"present": True}}


def test_current_turn_lore_immediately_precedes_input():
    hist = [
        message("g", "assistant", "欢迎"),
        message("u1", "user", "继续"),
        message("a1", "assistant", "好"),
    ]
    doc = document()
    doc["character_book"]["entries"].append({"id": "second", "keys": ["灯塔"], "content": "补充设定"})
    out = compile(doc, hist, {"state": {"place": "北岸"}})
    sources = [item["source"] for item in out["trace"]]
    last_history = max(i for i, source in enumerate(sources) if source == "history")
    assert sources[last_history + 1 : last_history + 4] == ["lore:e", "lore:second", "input"]
    assert sources[-1] == "state"


def test_exit_starts_cooldown_and_old_hidden_keywords_cannot_retrigger():
    hist = [
        message("g", "assistant", "灯塔"),
        message("u1", "user", "继续"),
        message("a1", "assistant", "好"),
    ]
    first = compile(document(), hist[:1], {"state": {"place": "北岸"}})
    snap = {
        "state": {},
        "lore": first["next_lore"],
        "lore_injections": first["next_lore_injections"],
        "context_window": {"covered_count": 1, "fingerprint": fingerprint(hist[:1])},
    }
    out = compile(document(scan_depth=100), hist, snap, text="继续")
    assert out["next_lore_injections"] == []
    assert out["next_lore"] == {"e": {"present": False, "removed_at": 2}}
    assert out["lore"] == []
    out = compile(document(keys=["继续"]), hist, snap, text="别的")
    assert not out["next_lore_injections"]
    assert out["next_lore"] == {"e": {"present": False, "removed_at": 2}}
    snap.update(lore=out["next_lore"], lore_injections=[])
    hist += [message("u2", "user", "别的"), message("a2", "assistant", "继续")]
    out = compile(document(keys=["继续"]), hist, snap, text="别的")
    note = next(n for n in out["diagnostics"] if n["reason"] == "冷却恢复判断")
    assert note["recovery_elapsed"] == 1 and note["recovery_probability"] > 0


def test_current_user_is_scanned_with_zero_depth_and_multiple_entries_stay_adjacent():
    doc = document(scan_depth=0)
    doc["character_book"]["entries"].append(
        {"id": "second", "keys": ["灯塔"], "content": "补充设定", "position": 1, "scan_depth": 0}
    )
    hist = [message("g", "assistant", "欢迎")]
    out = compile(doc, hist, {"state": {}})
    i = next(i for i, m in enumerate(out["trace"]) if m.get("message_id") == "g")
    assert [m["lore_ids"] for m in out["trace"][i - 2 : i]] == [["e"], ["second"]]
    no_trigger = compile(doc, [message("g", "assistant", "灯塔")], {"state": {}}, text="继续")
    assert no_trigger["lore"] == []


def test_recipe_depth_blocks_do_not_separate_lore_from_anchor():
    s = session()
    for b in s["recipe"]["blocks"]:
        if b["source"] == "input":
            b["depth"] = 1
    out = compile_context(
        document(),
        s,
        [message("g", "assistant", "")],
        {"state": {}},
        "灯塔",
        Settings(),
        "seed",
        input_message_id="u",
    )
    i = next(i for i, m in enumerate(out["trace"]) if m.get("lore_ids") == ["e"])
    assert out["trace"][i + 1]["message_id"] == "u"


def test_constant_keeps_original_position_and_ignores_cooldown_probability():
    out = compile(
        document(constant=True, position=0, probability=0),
        [],
        {"state": {}, "lore": {"e": {"present": False, "removed_at": 1}}},
        text="继续",
    )
    item = next(m for m in out["trace"] if m.get("lore_ids") == ["e"])
    assert item["lore_region"] == "constant"
    assert item["required"] is True
    assert item["content"] == "<lorebook>\n灯塔：{{getvar::place}}\n</lorebook>"
    assert out["next_lore_injections"] == [] and out["next_lore"] == {"e": {"present": True}}


def test_constants_at_different_depths_are_sent_once_each():
    doc = {
        "name": "角色",
        "character_book": {
            "entries": [
                {
                    "id": "rules",
                    "constant": True,
                    "content": "常驻总规则",
                    # Every optional null falls back to its default or legacy alias.
                    **dict.fromkeys(_LORE_OPTIONAL_FIELDS - {"constant", "content"}),
                },
                {
                    "id": "update",
                    "constant": True,
                    "content": "变量更新规则",
                    "position": 4,
                    "depth": 2,
                    # An extension null must not hide the top-level depth.
                    "extensions": {"role": None, "depth": None},
                },
                {
                    "id": "format",
                    "constant": True,
                    "content": "变量输出格式",
                    "position": 4,
                    "depth": 1,
                    "role": 0,
                    "probability": 0,
                    "useProbability": False,
                    "scan_depth": 0,
                },
            ]
        },
    }
    history = [
        message("g", "assistant", "开场"),
        message("u", "user", "行动"),
        message("a", "assistant", "回应"),
    ]
    original = copy.deepcopy(doc)
    generate = TurnRequest(
        text="继续",
        operation_id="o",
        expected_revision=0,
        generate={
            "overrides": None,
            "ordered_prompts": None,
            "injects": [None, {"role": None, "depth": None, "content": "默认角色注入"}],
        },
    ).generate
    assert generate == {"injects": [{"content": "默认角色注入"}]}
    out = compile(doc, history, {}, text="继续", generate=generate)
    full = compile_window(
        doc,
        {**session(), "agent_mode": "full_agent"},
        history,
        {},
        "继续",
        Settings(runtime_mode="full_agent"),
        [],
    )
    for compiled in (out, full):
        lore = [row for row in compiled["trace"] if row.get("lore_region") == "constant"]
        assert len(lore) == 3
        assert all(row["role"] == "system" and row["message_type"] == "lore_constant" for row in lore)
        assert all({"role": "system", "content": row["content"]} in compiled["messages"] for row in lore)
    assert doc == original
    assert next(row for row in out["trace"] if row["source"] == "inject")["content"] == "默认角色注入"
    for eid, content in [("rules", "常驻总规则"), ("update", "变量更新规则"), ("format", "变量输出格式")]:
        assert sum(eid in item.get("lore_ids", []) for item in out["trace"]) == 1
        assert sum(item["content"].count(content) for item in out["messages"]) == 1
    for eid, depth in [("update", 2), ("format", 1)]:
        item = next(item for item in out["trace"] if item.get("lore_ids") == [eid])
        assert item["lore_region"] == "constant"
        assert item["required"] is True
    indices = [i for i, item in enumerate(out["trace"]) if item.get("lore_region") == "constant"]
    assert indices == list(range(indices[0], indices[0] + 3))
    assert indices[-1] < next(i for i, item in enumerate(out["trace"]) if item["source"] == "history")
    assert [out["trace"][i]["lore_ids"][0] for i in indices] == ["rules", "update", "format"]
    assert out["next_lore_injections"] == []


@pytest.mark.parametrize("mode", ["conversation", "st_top"])
def test_all_constant_positions_stay_in_one_protected_region(mode):
    entries = [
        {
            "id": str(i),
            "constant": True,
            "content": f"规则{i}",
            "position": i,
            "depth": 999,
            "probability": 0,
            "timelines": ["别的时间线"],
        }
        for i in range(8)
    ]
    doc = {"name": "角色", "character_book": {"entries": entries}}
    s = session(mode)
    for block in s["recipe"]["blocks"]:
        if block["source"].startswith("lore"):
            block.update(enabled=False, budget=1, depth=999)
    out = compile_context(
        doc,
        s,
        [message("g", "assistant", "开场")],
        {"state": {"世界": {"当前时间线": "当前"}}},
        "继续",
        Settings(lore_max_entries=1, lore_max_chars=1),
        "seed",
    )
    trace = out["trace"]
    indices = [i for i, item in enumerate(trace) if item.get("lore_region") == "constant"]
    assert len(indices) == 8
    assert indices == list(range(indices[0], indices[0] + 8))
    assert indices[-1] < next(i for i, item in enumerate(trace) if item["source"] == "history")
    assert all(trace[i]["required"] for i in indices)
    assert [trace[i]["lore_ids"] for i in indices] == [[str(i)] for i in range(8)]


def test_each_st_top_lore_entry_has_its_own_wrapper():
    entries = [
        {
            "id": str(i),
            "keys": ["灯塔"],
            "content": f"灯塔设定{i}",
            "position": position,
            "outletName": "special",
        }
        for i, position in enumerate((0, 0, 1, 2, 4, 5, 6, 7))
    ]
    doc = {"name": "角色", "character_book": {"entries": entries}}
    s = session("st_top")
    s["recipe"]["blocks"].append({"id": "special", "source": "outlet:special"})
    out = compile_context(
        doc,
        s,
        [message("g", "assistant", "开场")],
        {"state": {}},
        "灯塔",
        Settings(lore_max_entries=20),
        "seed",
    )
    for i in range(len(entries)):
        content = "\n".join(m["content"] for m in out["trace"] if str(i) in m.get("lore_ids", []))
        assert content.count(f"<lorebook>\n灯塔设定{i}\n</lorebook>") == 1


def test_injection_title_is_opt_in_and_prefers_comment_over_name():
    entries = [
        {"id": "c", "constant": True, "comment": "常驻标题", "name": "旧名", "content": "常驻设定"},
        {"id": "e", "keys": ["灯塔"], "comment": "", "name": "灯塔条目", "content": "灯塔设定"},
    ]
    doc = {"name": "角色", "character_book": {"entries": entries}}
    history = [message("g", "assistant", "开场")]

    def lore_bodies(mode, **values):
        out = compile_context(doc, session(mode), history, {"state": {}}, "灯塔", Settings(**values), "seed")
        # st_top lore is rewrapped per entry by the adapter from runtime_parts.
        return [
            part
            for m in out["trace"]
            if m.get("lore_ids")
            for part in m.get("runtime_parts") or [m["content"]]
        ]

    assert not any("【" in body for body in lore_bodies("conversation"))
    for mode in ("conversation", "st_top"):
        bodies = lore_bodies(mode, lore_injection_title=True)
        assert any("<lorebook>\n【常驻标题】\n常驻设定\n</lorebook>" == body for body in bodies)
        assert any(
            body.startswith(("【灯塔条目】\n灯塔设定", "<lorebook>\n【灯塔条目】\n")) for body in bodies
        )


def test_constant_over_model_budget_remains_complete_with_warning():
    content = "常驻规则" * 3000
    out = compile_context(
        document(constant=True, content=content),
        session(),
        [],
        {},
        "继续",
        Settings(context_tokens=4096, reply_tokens=128),
        "budget",
    )
    assert any(content in m["content"] for m in out["messages"])
    assert any("仍完整发送" in d["reason"] for d in out["diagnostics"])


def test_st_top_replaces_entries_and_starts_cooldown_at_absence():
    s = session("st_top")
    snap = {"state": {}}
    a = compile_context(document(scan_depth=0), s, [], snap, "灯塔", Settings(), "a")
    assert a["next_lore_injections"] == []
    snap["lore"] = a["next_lore"]
    b = compile_context(
        document(scan_depth=0), s, [message("u", "user", "灯塔")], snap, "继续", Settings(), "b"
    )
    assert b["next_lore"] == {"e": {"present": False, "removed_at": 2}}


def test_global_mode_only_affects_new_sessions_and_fork_remaps_records(tmp_path):
    store = Store(tmp_path)
    card = store.import_card(json.dumps({"first_mes": "欢迎", **document()}).encode(), "card.json")
    original = store.new_session(SessionCreate(card_id=card["id"]))
    assert original["lore_injection_mode"] == "conversation"
    store.save_settings(Settings(lore_injection_mode="st_top"))
    other = store.new_session(SessionCreate(card_id=card["id"]))
    assert other["lore_injection_mode"] == "st_top"
    assert store.session(original["id"])["lore_injection_mode"] == "conversation"
    out = compile(document(), original["messages"], {"state": {}})
    snap = {**original["snapshot"], "lore_injections": out["next_lore_injections"], "lore": out["next_lore"]}
    # A greeting-anchored record with evidence already committed at this point.
    snap["lore_injections"][0]["source_ids"] = [original["head"]]
    with store.connection() as db:
        db.execute("UPDATE messages SET snapshot=? WHERE id=?", (dump(snap), original["head"]))
    fork = store.fork_session(original["id"], original["head"], 0)
    assert fork["lore_injection_mode"] == "conversation"
    assert fork["snapshot"]["lore_injections"][0]["anchor_id"] == fork["head"]
    assert fork["snapshot"]["lore_injections"][0]["source_ids"] == [fork["head"]]
    assert store.session(original["id"])["snapshot"] == snap


@pytest.mark.parametrize("cooldown", [0, 4])
def test_exit_request_cannot_reanchor_even_with_direct_mention(cooldown):
    hist = [
        message("g", "assistant", "欢迎"),
        message("u", "user", "灯塔"),
        message("a", "assistant", "继续"),
    ]
    first = compile(document(cooldown=cooldown), hist[:1], {"state": {}})
    snap = {
        "state": {},
        "lore": first["next_lore"],
        "lore_injections": first["next_lore_injections"],
        "context_window": {"covered_count": 1, "fingerprint": fingerprint(hist[:1])},
    }
    out = compile(document(cooldown=cooldown), hist, snap, text="灯塔")
    assert out["next_lore_injections"] == []
    assert out["next_lore"] == {"e": {"present": False, "removed_at": 2}}


async def test_real_narrative_requests_keep_lore_then_exit_at_cap_and_regenerate(tmp_path):
    from kohakuterrarium.testing.llm import ScriptedLLM

    from tests.support.tracker_model import MemoryModel

    store = Store(tmp_path)
    store.save_settings(Settings(history_message_cap=8, chunk_size=2, memory_interval_turns=1))
    card = store.import_card(
        json.dumps({"first_mes": "欢迎", **document(content="冻结灯塔设定")}).encode(), "card.json"
    )
    current = store.new_session(SessionCreate(card_id=card["id"]))
    narrative = ScriptedLLM(["正文"])
    service = RoleplayRuntime(store)
    service.provider_factory = lambda: narrative
    service.memory_provider_factory = lambda: MemoryModel([{"operations": [], "questions": []}])
    try:
        for i in range(4):
            turn = service.submit(
                current["id"],
                TurnRequest(text="灯塔", operation_id=f"send-{i}", expected_revision=current["revision"]),
            )
            await service.tasks[turn["id"]]
            assert store.turn(turn["id"])["status"] == "committed"
            current = store.session(current["id"])
            events = store.events(turn["id"])
            ready = next(event for event in events if event["kind"] == "input_ready")
            assert ready["payload"]["message"]["id"] == current["messages"][-2]["id"]
            assert ready["payload"]["message"]["raw"] == "灯塔"
            assert all(ready["seq"] < event["seq"] for event in events if event["kind"] == "text")
            if i == 0:
                assert ready["payload"]["lore_injections"][0]["content"] == "冻结灯塔设定"
            present = [m for m in narrative.call_log[-1] if "冻结灯塔设定" in str(m.get("content", ""))]
            assert len(present) == (1 if i < 3 else 0)
            if i == 0:
                wire = narrative.call_log[-1]
                at = wire.index(present[0])
                assert present[0]["role"] == "system" and wire[at + 1]["content"] == "欢迎"
        assert current["snapshot"]["lore"] == {"e": {"present": False, "removed_at": 4}}
        assert current["snapshot"]["context_window"]["covered_count"] == 3
        original_snapshot = current["snapshot"]
        turn = service.submit(
            current["id"],
            TurnRequest(mode="regenerate", operation_id="regen", expected_revision=current["revision"]),
        )
        await service.tasks[turn["id"]]
        assert store.turn(turn["id"])["status"] == "committed"
        regenerated = store.session(current["id"])["snapshot"]
        # Regeneration restores the pre-turn archive, never the discarded
        # candidate's post-story coverage; recomputing the same exchange must
        # not advance its window or lore recovery a second time.
        for key in ("context_window", "lore", "lore_injections", "state"):
            assert regenerated.get(key) == original_snapshot.get(key)
    finally:
        await service.close()


def test_lore_limits_apply_before_recursive_activation():
    doc = {
        "name": "角色",
        "character_book": {
            "entries": [
                {"id": "a", "keys": ["灯塔"], "content": "递归关键词", "order": 1},
                {"id": "large", "keys": ["灯塔"], "content": "超长" * 100, "order": 2},
                {"id": "b", "keys": ["递归关键词"], "content": "补充", "order": 3},
            ]
        },
    }
    out = compile_context(
        doc, session(), [], {"state": {}}, "灯塔", Settings(lore_max_entries=1, lore_max_chars=128), "seed"
    )
    assert [e["id"] for e in out["lore"]] == ["a"]
    assert len(out["next_lore_injections"]) == 1
    assert any("上限" in d["reason"] for d in out["diagnostics"])


def test_new_rolling_lore_remains_complete_when_total_budget_is_full():
    doc = document(content="界" * 2000)
    out = compile_context(
        doc,
        session(),
        [],
        {"state": {}},
        "灯塔",
        Settings(context_tokens=4608, reply_tokens=1024, system_prompt="规则" * 100),
        "seed",
    )
    assert out["next_lore_injections"][0]["content"] == "界" * 2000
    assert out["next_lore"]["e"]["present"]


def test_frozen_lore_does_not_consume_new_injection_limits():
    doc = document()
    doc["character_book"]["entries"].append({"id": "new", "keys": ["灯塔"], "content": "新设定"})
    first = compile_context(
        document(), session(), [], {"state": {}}, "灯塔", Settings(), "first", input_message_id="u"
    )
    snap = {"state": {}, "lore": first["next_lore"], "lore_injections": first["next_lore_injections"]}
    history = [message("u", "user", "灯塔"), message("a", "assistant", "继续")]
    out = compile_context(doc, session(), history, snap, "灯塔", Settings(lore_max_entries=1), "next")
    assert out["next_lore_injections"][0] == first["next_lore_injections"][0]
    assert [e["id"] for e in out["lore"]] == ["new"]
    assert len(out["next_lore_injections"]) == 2
    assert out["next_lore"] == {"e": {"present": True}, "new": {"present": True}}


@pytest.mark.parametrize("mode", ["conversation", "st_top"])
def test_default_limit_is_five_new_entries_excluding_constants(mode):
    entries = [{"id": f"c{i}", "constant": True, "content": "常驻", "order": i} for i in range(6)]
    entries += [
        {"id": f"e{i}", "keys": ["灯塔"], "comment": "灯塔", "content": "设定", "order": 10 + i}
        for i in range(6)
    ]
    out = compile_context(
        {"name": "角色", "character_book": {"entries": entries}},
        session(mode),
        [],
        {"state": {}},
        "灯塔",
        Settings(),
        "seed",
    )
    injected = {eid for m in out["trace"] for eid in m.get("lore_ids", [])}
    assert injected == {f"c{i}" for i in range(6)} | {f"e{i}" for i in range(5)}


@pytest.mark.parametrize("mode", ["conversation", "st_top"])
def test_character_entries_remain_complete_regardless_of_size(mode):
    entries = [
        {"id": "constant", "constant": True, "content": "常驻" * 2000, "order": 0},
        {"id": "a", "keys": ["灯塔"], "comment": "灯塔", "content": "中" * 1500, "order": 1},
        {"id": "b", "keys": ["灯塔"], "comment": "灯塔", "content": "文" * 1498 + "{{user}}", "order": 2},
        {"id": "c", "keys": ["灯塔"], "comment": "灯塔", "content": "再", "order": 3},
    ]
    out = compile_context(
        {"name": "角色", "character_book": {"entries": entries}},
        session(mode),
        [],
        {"state": {}},
        "灯塔",
        Settings(),
        "seed",
    )
    injected = {eid for m in out["trace"] for eid in m.get("lore_ids", [])}
    assert injected == {"constant", "a", "b", "c"}
    assert any("文" * 1498 + "玩家" in m["content"] for m in out["trace"])


@pytest.mark.parametrize("examples", ["示例台词", ""])
def test_first_rolling_lore_precedes_first_history_with_or_without_examples(examples):
    doc = document()
    doc["mes_example"] = examples
    doc["character_book"]["entries"].append(
        {"id": "constant", "constant": True, "content": "常驻设定", "position": 0}
    )
    out = compile(doc, [message("g", "assistant", "开场白")], {"state": {}})
    trace = out["trace"]
    history = next(i for i, m in enumerate(trace) if m["source"] == "history")
    assert trace[history - 1]["lore_ids"] == ["e"]
    constant = next(i for i, m in enumerate(trace) if "constant" in m.get("lore_ids", []))
    assert constant < history - 1
    if examples:
        example = next(i for i, m in enumerate(trace) if m["source"] == "examples")
        assert example < history - 1
    else:
        assert not any(m["source"] == "examples" for m in trace)


def test_named_subject_selection_does_not_drop_later_entries_for_size():
    doc = {
        "name": "角色",
        "character_book": {
            "entries": [
                {"id": "121", "keys": ["力量", "米妮娜"], "content": "【米妮娜】\n力量很强"},
                {"id": "130", "keys": ["弱点", "纳纳纳"], "content": "【纳纳纳】\n观察弱点"},
                {"id": "55", "keys": ["露琪亚"], "content": "【露琪亚】\n" + "冰" * 30},
                {"id": "98", "keys": ["一护"], "content": "【一护】\n" + "刀" * 30},
                {"id": "fill", "keys": ["露琪亚或一护"], "content": "低相关补充"},
            ]
        },
    }
    out = compile_context(
        doc, session(), [], {"state": {}}, "力量，弱点，露琪亚或一护", Settings(lore_max_chars=50), "seed"
    )
    assert [e["id"] for e in out["lore"]] == ["55", "98", "fill"]
    assert out["lore"][0]["match_source"] == "input"
    assert out["lore"][0]["matched_keys"] == ["露琪亚"]
    assert all(e["content"] in "\n".join(m["content"] for m in out["messages"]) for e in out["lore"])


@pytest.mark.parametrize(
    "enabled,depth,expected",
    [
        (False, 3, ["a"]),
        (True, 0, ["a"]),
        (True, 1, ["a", "b"]),
        (True, 2, ["a", "b", "c"]),
    ],
)
def test_compiler_does_not_expand_unapproved_hops(enabled, depth, expected):
    doc = {
        "name": "角色",
        "character_book": {
            "entries": [
                {"id": "a", "keys": ["灯塔"], "content": "海港"},
                {"id": "b", "keys": ["海港"], "content": "远山"},
                {"id": "c", "keys": ["远山"], "content": "终点"},
            ]
        },
    }
    out = compile_context(
        doc,
        session(),
        [],
        {"state": {}},
        "灯塔",
        Settings(lore_recursive=enabled, lore_recursion_depth=depth),
        "seed",
    )
    assert [e["id"] for e in out["lore"]] == ["a"]
    assert [e["recursion_depth"] for e in out["lore"]] == [0]


def test_constants_cannot_recursively_activate_unrelated_entries():
    doc = {
        "name": "角色",
        "character_book": {
            "entries": [
                {"id": "rules", "constant": True, "content": "咏唱，BLEACH"},
                {"id": "spell", "keys": ["咏唱"], "content": "鬼道"},
                {"id": "knight", "keys": ["B"], "content": "哈斯沃德"},
            ]
        },
    }
    assert [e["id"] for e in compile(doc, [], {"state": {}}, text="继续")["lore"]] == ["rules"]


def test_short_latin_codes_require_author_opt_in_and_word_boundaries():
    from tavern.conversation.context import matches

    assert not matches("B", "BLEACH", {})
    assert not matches("B", "BLEACH", {"use_regex": True})
    assert not matches("B", "BLEACH", {"use_regex": True, "matchWholeWords": True})
    assert matches("/B/", "BLEACH", {})
    assert not matches("U", "{{user}}", {})
    assert not matches("B", "圣文字 B", {})
    assert matches("B", "圣文字 B", {"matchWholeWords": True})
    assert not matches("B", "BLEACH", {"matchWholeWords": True})
    assert not matches("AI", "said", {})
    assert matches("AI", "AI 扮演", {})


def test_exported_lore_flags_preserve_matching_recursion_and_source():
    from tavern.character.cards import export_card, lore_entries, parse_card
    from tavern.conversation.context import activate_lore, matches
    from tavern.search.lore_admission import recursive_candidates

    original = {
        "spec": "chara_card_v3",
        "spec_version": "3.0",
        "data": {
            "name": "角色",
            "character_book": {
                "entries": [
                    {
                        "id": "a",
                        "keys": ["LIGHT", "B"],
                        "content": "海港：灯塔完整设定",
                        "extensions": {
                            "match_whole_words": True,
                            "case_sensitive": True,
                            "prevent_recursion": True,
                        },
                    },
                    {"id": "b", "keys": ["海港"], "content": "远山完整设定"},
                    {
                        "id": "c",
                        "keys": ["海港"],
                        "content": "排除递归的完整设定",
                        "extensions": {"exclude_recursion": True},
                    },
                ]
            },
        },
    }
    raw = json.dumps(original).encode()
    doc, kind, assets, _ = parse_card(raw, "card.json")
    entries = lore_entries(doc)
    assert entries[0]["matchWholeWords"] is True
    assert matches("B", "B", {**entries[0], "use_regex": True})
    assert not matches("B", "BLEACH", {**entries[0], "use_regex": True})
    assert not matches("LIGHT", "LIGHTING", entries[0])
    assert not matches("LIGHT", "light", entries[0])
    assert compile(doc, [], {"state": {}}, text="LIGHTING")["lore"] == []
    assert compile(doc, [], {"state": {}}, text="light")["lore"] == []
    assert recursive_candidates(entries, ["a"], {"a"}, 1) == {}
    selected, _, _ = activate_lore(doc, [], "LIGHT", {}, "seed", recursion_depth=1)
    assert [e["id"] for e in selected] == ["a"]
    out = compile(doc, [], {"state": {}}, text="LIGHT")
    assert any(m["content"] == "<lorebook>\n海港：灯塔完整设定\n</lorebook>" for m in out["messages"])
    assert doc == original
    assert json.loads(export_card(doc, kind, raw, assets)[0]) == original

    # Existing explicit camelCase values must keep precedence over exported aliases.
    source = doc["data"]["character_book"]["entries"][0]
    source["preventRecursion"] = False
    entries = lore_entries(doc)
    assert entries[0]["preventRecursion"] is False
    assert recursive_candidates(entries, ["a"], {"a"}, 1) == {
        "b": {"recursive_parents": ["a"], "recursion_depth": 1}
    }
    selected, _, _ = activate_lore(doc, [], "LIGHT", {}, "seed", recursion_depth=1)
    assert [e["id"] for e in selected] == ["a", "b"]
    out = compile(
        doc,
        [],
        {"state": {}},
        text="LIGHT",
        retrieval={"ids": ["a", "b"], "query": "LIGHT", "diagnostics": []},
    )
    assert any(m["content"] == "<lorebook>\n远山完整设定\n</lorebook>" for m in out["messages"])
    assert all("排除递归的完整设定" not in m["content"] for m in out["messages"])
    assert [e["id"] for e in compile(doc, [], {"state": {}}, text="海港")["lore"]] == ["b", "c"]
    source["matchWholeWords"] = False
    source["caseSensitive"] = None
    assert matches("LIGHT", "lighting", lore_entries(doc)[0])
    assert source["extensions"] == original["data"]["character_book"]["entries"][0]["extensions"]


@pytest.mark.parametrize(
    "values",
    [
        {"lore_recursion_depth": -1},
        {"lore_recursion_depth": 17},
        {"lore_recursion_depth": 1.5},
        {"lore_recursive": "false"},
    ],
)
def test_recursion_settings_reject_invalid_values(values):
    with pytest.raises(ValueError):
        Settings(**values)


def test_recursion_settings_persist_and_defaults(tmp_path):
    store = Store(tmp_path)
    assert store.settings().lore_recursive is True
    assert store.settings().lore_recursion_depth == 1
    store.save_settings(Settings(lore_recursive=False, lore_recursion_depth=7))
    assert store.settings().lore_recursive is False
    assert store.settings().lore_recursion_depth == 7


def test_body_only_short_keywords_do_not_activate_named_subjects():
    doc = {
        "name": "角色",
        "character_book": {
            "entries": [
                {"id": "a", "keys": ["力量", "米妮娜"], "use_regex": True, "content": "【米妮娜】\n力量"},
                {"id": "b", "keys": ["弱点", "纳纳纳"], "content": "【纳纳纳】\n弱点"},
            ]
        },
    }
    out = compile(doc, [], {"state": {}}, text="把力量注入刀中，攻击弱点")
    assert out["lore"] == []
    assert len([n for n in out["diagnostics"] if "短关键词" in n["reason"]]) == 2
    assert [e["id"] for e in compile(doc, [], {"state": {}}, text="米妮娜的力量")["lore"]] == ["a"]
    doc["character_book"]["entries"][0]["matchWholeWords"] = True
    assert [e["id"] for e in compile(doc, [], {"state": {}}, text="力量")["lore"]] == ["a"]


def test_explicit_timeline_tags_exclude_later_arc_entries():
    doc = document(keys=["灭却师"], content="后期设定", timelines=["千年血战篇"])
    state = {"世界": {"当前时间线": "死神代行篇"}}
    out = compile(doc, [], {"state": state}, text="灭却师", requested=["e"])
    assert out["lore"] == []
    assert any("时间线" in n["reason"] for n in out["diagnostics"])
    state["世界"]["当前时间线"] = "千年血战篇"
    assert [e["id"] for e in compile(doc, [], {"state": state}, text="灭却师")["lore"]] == ["e"]
    assert [e["id"] for e in compile(doc, [], {"state": {}}, text="灭却师")["lore"]] == ["e"]


def test_weak_match_cannot_bypass_original_query_gate_through_recursion():
    doc = {
        "name": "角色",
        "character_book": {
            "entries": [
                {"id": "a", "keys": ["灯塔"], "content": "米妮娜"},
                {"id": "b", "keys": ["力量", "米妮娜"], "content": "【米妮娜】\n力量"},
            ]
        },
    }
    assert [e["id"] for e in compile(doc, [], {"state": {}}, text="灯塔的力量")["lore"]] == ["a"]


@pytest.mark.parametrize("batch, expected", [(2, 2), (3, 4), (4, 4)])
def test_window_batch_counts_story_only_and_removes_all_anchored_lore(batch, expected):
    from tavern.conversation.window import plan_window

    hist = [message(f"m{i}", "user" if i % 2 == 0 else "assistant", "继续") for i in range(8)]
    records = [
        {
            "id": f"entry-{i}-{j}",
            "anchor_id": f"m{i}",
            "content": f"冻结资料{i}-{j}",
            "title": "资料",
            "source_ids": [],
        }
        for i in range(5)
        for j in range(3)
    ]
    snap = {"lore_injections": records, "lore": {r["id"]: {"present": True} for r in records}}
    before = copy.deepcopy(snap)
    # Nine story messages including input reach cap; lore does not affect the batch.
    count, context = plan_window(
        {}, session(), hist, snap, "继续", Settings(history_message_cap=8, chunk_size=batch), "batch"
    )
    assert count == expected
    assert snap == before
    remaining = [r for r in records if int(r["anchor_id"][1:]) >= expected]
    assert context["next_lore_injections"] == remaining
    assert {m["message_id"] for m in context["trace"] if m["source"] == "history"} == {
        f"m{i}" for i in range(expected, 8)
    }
    for record in remaining:
        at = next(i for i, m in enumerate(context["trace"]) if m.get("lore_ids") == [record["id"]])
        anchor = next(i for i, m in enumerate(context["trace"]) if m.get("message_id") == record["anchor_id"])
        assert 1 <= anchor - at <= 3


def test_lore_does_not_trigger_message_cap():
    from tavern.conversation.window import plan_window

    hist = [message(f"m{i}", "user" if i % 2 == 0 else "assistant", "continue") for i in range(6)]
    records = [
        {"id": f"entry-{j}", "anchor_id": "m0", "content": "lore", "title": "lore", "source_ids": []}
        for j in range(20)
    ]
    snap = {"lore_injections": records, "lore": {r["id"]: {"present": True} for r in records}}
    count, context = plan_window(
        {}, session(), hist, snap, "continue", Settings(history_message_cap=8, chunk_size=2), "cap"
    )
    assert count == 0
    assert context["window_message_count"] == 7
    assert context["next_lore_injections"] == records
