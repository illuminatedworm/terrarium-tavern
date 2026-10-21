import json

import pytest
from httpx import ASGITransport, AsyncClient

from tavern.application.api import create_app
from tavern.core.models import SessionCreate, Settings, TurnRequest
from tavern.memories.memory import render_memory
from tavern.memories.memory_engine import (
    CallBudget,
    apply_operations,
    consolidate,
    estimate_batches,
    plan_batch,
)
from tavern.memories.memory_evidence import evidence_pool, select_evidence
from tavern.memories.memory_records import (
    Record,
    RecordEdit,
    install_memory,
    manual_edit,
    source_hash,
    with_source_status,
)
from tavern.storage.store import Conflict, dump, uid
from tests.support.memory_fixtures import wire_proposal


def history(texts=None):
    return [
        {
            "id": f"m{i}",
            "role": "user" if i % 2 == 0 else "assistant",
            "raw": text,
            "model": text,
            "snapshot": {"state": {}, "content": None},
        }
        for i, text in enumerate(texts or ["借我戒指", "她把戒指借给了你。", "还给你", "她收回了戒指。"])
    ]


def proposal(mid="m1", excerpt="她把戒指借给了你。", *, rid="ring", op="add", **fields):
    return {
        "operations": [
            {
                "op": op,
                "id": rid,
                "record": {
                    "category": "current",
                    "content": "戒指暂由玩家保管",
                    "sources": [{"message_id": mid, "excerpt": excerpt}],
                    **fields,
                },
            }
        ],
        "questions": [],
    }


def apply(memory, output, messages=None, **kw):
    messages = messages or history()
    by_id = {m["id"]: m for m in messages}
    return apply_operations(memory, output, by_id, by_id, [], fresh_ids=set(by_id), settings=Settings(), **kw)


def empty():
    return {"version": 3, "records": []}


def test_incremental_change_retains_experience_and_omitted_open_loop():
    memory = apply(empty(), proposal())
    memory["records"].append(Record(id="promise", category="open", content="明早归还披风").model_dump())
    memory = apply(memory, proposal(rid="loan", category="episode", content="她曾把戒指托付给玩家"))
    memory = apply(
        memory, proposal(mid="m3", excerpt="她收回了戒指。", op="revise", content="戒指已回到她手中")
    )
    assert {r["id"] for r in memory["records"]} == {"ring", "promise", "loan"}
    assert memory["records"][0]["content"] == "戒指已回到她手中"
    snap = {}
    install_memory(snap, memory)
    assert render_memory(snap).count("戒指已回到她手中") == 1


def test_completed_promise_becomes_history_instead_of_an_open_loop():
    memory = apply(empty(), proposal(category="open"))
    result = apply(memory, proposal(op="complete", mid="m3", excerpt="她收回了戒指。", content="归还已完成"))
    assert result["records"][0]["status"] == "completed"
    assert result["records"][0]["category"] == "episode"


def test_automatic_remove_leaves_no_tombstone_but_manual_delete_does():
    memory = apply(empty(), proposal())
    removed = apply(
        memory, proposal(op="remove", mid="m3", excerpt="她收回了戒指。", content="戒指借出记录已过时")
    )
    assert removed["records"] == []
    deleted = manual_edit(memory, RecordEdit(expected_revision=0, action="delete", id="ring"))
    assert len(deleted["records"]) == 1
    assert deleted["records"][0]["status"] == "deleted"
    assert deleted["records"][0]["manual"]


def test_model_cannot_discard_a_record_by_setting_superseded_status():
    with pytest.raises(ValueError, match="remove 或 merge"):
        apply(empty(), proposal(status="superseded"))


@pytest.mark.parametrize(
    "fields",
    [
        {"kind": "claim", "category": "episode", "content": "他否认杀人，事实尚未确认"},
        {"category": "open", "content": "承诺明早交物，尚未兑现"},
        {"category": "episode", "content": "回忆过去受伤，当前伤势未知", "story_time": "过去"},
        {"category": "episode", "content": "身份揭露给玩家", "known_to": ["玩家"]},
        {"kind": "inference", "category": "current", "content": "她可能已不再戒备"},
    ],
)
def test_representation_preserves_claim_time_knowledge_and_uncertainty(fields):
    result = apply(empty(), proposal(**fields))["records"][0]
    assert result["kind"] == fields.get("kind", "fact")
    assert result["known_to"] == fields.get("known_to", [])
    if fields.get("kind") == "inference":
        assert result["category"] == "uncertain"


@pytest.mark.parametrize("mutation", ["foreign", "quote", "setting", "background"])
def test_invalid_critical_evidence_never_changes_archive(mutation):
    old = empty()
    out = proposal()
    if mutation == "foreign":
        out["operations"][0]["record"]["sources"][0]["message_id"] = "other-branch"
    elif mutation == "quote":
        out["operations"][0]["record"]["sources"][0]["excerpt"] = "她赠送了戒指"
    elif mutation == "setting":
        out["operations"][0]["record"]["setting_refs"] = ["not-supplied"]
    by_id = {m["id"]: m for m in history()}
    with pytest.raises(ValueError):
        apply_operations(
            old,
            out,
            by_id,
            by_id,
            [],
            fresh_ids={"m3"} if mutation == "background" else set(by_id),
            settings=Settings(),
        )
    assert old == empty()


def test_manual_edit_deletion_and_changed_sources_are_protected():
    memory = apply(empty(), proposal())
    manual = manual_edit(
        memory,
        RecordEdit(expected_revision=0, action="edit", id="ring", content="手工保留", category="current"),
    )
    with pytest.raises(ValueError, match="手工"):
        apply(manual, proposal(op="revise"))
    deleted = manual_edit(memory, RecordEdit(expected_revision=0, action="delete", id="ring"))
    notices = []
    suppressed = set()
    unchanged = apply(
        deleted, proposal(rid="renamed"), on_suppressed=notices.append, suppressed_ids=suppressed
    )
    assert unchanged == deleted
    assert suppressed == {"renamed"} and len(notices) == 1
    # Deleting an old record must not suppress a genuinely new event forever,
    # even when the resulting ownership sentence happens to be the same.
    later = history()
    later[3]["raw"] = later[3]["model"] = "她第二次把戒指借给了你。"
    renewed = apply(deleted, proposal(mid="m3", excerpt="她第二次把戒指借给了你。", rid="new-loan"), later)
    assert renewed["records"][-1]["id"] == "new-loan"
    changed = history()
    changed[1]["model"] = "没有借出戒指"
    view = with_source_status(memory, changed)
    assert view["records"][0]["source_stale"]
    assert memory["records"][0]["content"] == view["records"][0]["content"]
    with pytest.raises(ValueError, match="改动"):
        apply(view, proposal(op="revise"))


async def test_deleted_memory_does_not_block_other_changes_or_coverage():
    old = apply(empty(), proposal())
    deleted = manual_edit(old, RecordEdit(expected_revision=0, action="delete", id="ring"))
    events = []

    async def generate(prompt, **kwargs):
        response = {
            "operations": [
                {
                    "op": "add",
                    "id": "N1",
                    "record": {"category": "current", "content": "戒指暂由玩家保管", "sources": ["H2#1"]},
                },
                {
                    "op": "add",
                    "id": "N2",
                    "record": {"category": "episode", "content": "她随后收回了戒指", "sources": ["H4#1"]},
                },
            ],
            "questions": [
                {"topic": "ownership", "query": "戒指归属", "record_ids": ["N1"]},
                {"topic": "ownership", "query": "已删除的戒指归属", "record_ids": ["R1"]},
            ],
        }
        return json.dumps(response, ensure_ascii=False)

    result = await consolidate(
        history(),
        Settings(),
        generate,
        memory=deleted,
        event=lambda kind, payload: events.append((kind, payload)),
    )
    assert [r["content"] for r in result["records"] if r["status"] != "deleted"] == ["她随后收回了戒指"]
    assert any(kind == "memory_tombstone_suppressed" for kind, _ in events)
    assert not any(kind == "memory_review_started" for kind, _ in events)


def test_tombstone_does_not_hide_invalid_source_of_other_operation():
    deleted = manual_edit(
        apply(empty(), proposal()), RecordEdit(expected_revision=0, action="delete", id="ring")
    )
    out = proposal(rid="renamed")
    out["operations"].append(proposal(rid="bad", excerpt="原文里没有这句话")["operations"][0])
    with pytest.raises(ValueError, match="摘录"):
        apply(deleted, out)


def test_merge_with_deleted_source_is_ignored_without_changing_survivor():
    memory = apply(empty(), proposal())
    memory = apply(memory, proposal(rid="other", content="她曾借出戒指"))
    deleted = manual_edit(memory, RecordEdit(expected_revision=0, action="delete", id="ring"))
    out = proposal(rid="other", op="merge", mid="m3", excerpt="她收回了戒指。", content="戒指交易已结束")
    out["operations"][0]["merge_ids"] = ["ring"]
    suppressed = set()
    result = apply(deleted, out, suppressed_ids=suppressed)
    assert result == deleted
    assert suppressed == {"other"}


async def test_incremental_input_and_shared_budget_are_bounded():
    calls = []

    async def generate(messages, **kw):
        calls.append((json.loads(messages[-1]["content"]), kw, messages.references))
        return json.dumps({"operations": [], "questions": []})

    budget = CallBudget()
    await consolidate(
        history(), Settings(), generate, memory=empty(), start=2, budget=budget, document={}, persona="用户"
    )
    payload = calls[0][0]
    assert (payload["batch_range"], payload["background_range"]) == ("H3-H4", "H1-H2")
    assert [m["ref"] for m in payload["messages"]] == ["H3", "H4"]
    assert [m["ref"] for m in payload["background_messages"]] == ["H1", "H2"]
    await consolidate(history(), Settings(), generate, memory=empty(), budget=budget)
    with pytest.raises(ValueError, match="两次"):
        await consolidate(history(), Settings(), generate, memory=empty(), budget=budget)
    assert len(calls) == 2


async def test_custom_prompt_macro_and_user_text_use_batch_identity():
    seen = []

    async def generate(messages, **kwargs):
        seen.extend(messages)
        return '{"operations":[],"questions":[]}'

    await consolidate(
        [{"id": "m", "role": "assistant", "raw": "{{user}}遇见{{char}}"}],
        Settings(tracker_system_prompt="为{{user}}记录{{char}}的变化"),
        generate,
        memory=empty(),
        document={"name": "艾琳"},
        persona="小舟",
    )
    assert seen[0]["content"].startswith("为小舟记录艾琳的变化\n\n【程序输出契约】")
    assert json.loads(seen[-1]["content"])["messages"][0]["passages"] == [
        {"ref": "H1#1", "text": "小舟遇见艾琳"}
    ]


async def test_review_only_once_and_failure_keeps_uncertain(monkeypatch):
    from tavern.memories import memory_engine

    def evidence(pool, query, settings, **kwargs):
        return [{"id": "extra" if kwargs else "first", "text": "戒指不可赠送" if kwargs else "戒指归艾琳"}]

    monkeypatch.setattr(memory_engine, "select_evidence", evidence)
    output = proposal(category="uncertain")
    output["questions"] = [{"topic": "ownership", "query": "戒指归属", "record_ids": ["ring"]}]
    calls = []

    async def generate(messages, **kw):
        calls.append(kw["phase"])
        if kw["phase"] == "memory_review":
            raise TimeoutError("timeout")
        return wire_proposal(messages, output)

    budget = CallBudget()
    result = await consolidate(history(), Settings(), generate, memory=empty(), budget=budget)
    assert result["records"][0]["category"] == "uncertain"
    assert calls == ["memory", "memory_review"] and budget.review == 1


async def test_no_new_evidence_or_disabled_review_does_not_call_twice():
    output = proposal(category="uncertain")
    output["questions"] = [{"topic": "ownership", "query": "戒指", "record_ids": ["ring"]}]
    calls = []

    async def generate(messages, **kw):
        calls.append(kw)
        return wire_proposal(messages, output)

    await consolidate(history(), Settings(), generate, memory=empty(), document={})
    assert len(calls) == 1


def test_reference_versions_and_cutoff_never_admit_future_material():
    messages = history()
    old_doc = {
        "description": "戒指是借出的",
        "character_book": {"entries": [{"id": 1, "content": "戒指归她", "keys": ["戒指"]}]},
    }
    messages[1]["snapshot"]["content"] = {"document": old_doc}
    messages[3]["snapshot"]["lore_injections"] = [{"id": "future", "content": "未来秘密"}]
    pool = evidence_pool(messages, {"description": "当前回退"}, 0, 2)
    assert any(r["historical"] and r["text"] == "戒指是借出的" for r in pool)
    assert any(not r["historical"] for r in pool)
    assert "未来秘密" not in json.dumps(pool, ensure_ascii=False)
    assert select_evidence(pool, "戒指", Settings())


def test_evidence_deduplicates_same_text_and_preserves_full_version():
    messages = history()
    doc = {
        "description": "戒指归艾琳",
        "character_book": {"entries": [{"id": 1, "keys": ["戒指"], "content": "戒指归艾琳"}]},
    }
    messages[1]["snapshot"]["lore_injections"] = [{"id": "frozen", "content": "戒指归艾琳"}]
    selected = select_evidence(evidence_pool(messages, doc, 0, 2), "戒指", Settings())
    assert len(selected) == 1
    assert selected[0]["historical"] and selected[0]["id"].startswith("e")
    assert len(selected[0]["reference"]) == 64


def test_long_evidence_and_single_oversized_exchange_remain_complete():
    content = "旧设定" * 3000 + "尾部证据"
    selected = select_evidence(
        [{"id": "long", "kind": "frozen_lore", "text": content}], "尾部证据", Settings()
    )
    assert selected[0]["text"] == content
    messages = history(["旧剧情" * 3000 + "尾部剧情", "她回应了。"])
    end, request, _, _ = plan_batch(
        messages,
        messages,
        0,
        {"version": 3, "records": []},
        {},
        Settings(memory_input_tokens=2048, memory_evidence_tokens=0),
    )
    assert end == 2
    assert "尾部剧情" in request[1]["content"]


async def test_duplicate_material_does_not_spend_review(monkeypatch):
    from tavern.memories import memory_engine

    monkeypatch.setattr(
        memory_engine,
        "select_evidence",
        lambda *a, **kw: [{"id": "different-handle" if kw else "first", "text": "同一段资料"}],
    )
    out = proposal(category="uncertain")
    out["questions"] = [{"topic": "ownership", "query": "戒指", "record_ids": ["ring"]}]
    phases = []

    async def generate(messages, **kw):
        phases.append(kw["phase"])
        return wire_proposal(messages, out)

    await consolidate(history(), Settings(), generate, memory=empty())
    assert phases == ["memory"]


def test_optional_invalid_quote_is_dropped_but_other_operations_survive():
    out = proposal(category="episode", callback=True, excerpt="虚构引用")
    out["operations"] += proposal(rid="valid")["operations"]
    notes = []
    result = apply(empty(), out, on_invalid=notes.append)
    assert [r["id"] for r in result["records"]] == ["valid"]
    assert notes[0]["count"] == 1


async def test_manual_batches_cover_every_message_automatic_batches_are_atomic():
    messages = history([f"第{i}条" + "界" * 600 for i in range(24)])
    cfg = Settings(memory_input_tokens=8000, memory_evidence_tokens=0)
    seen = []

    async def generate(prompt, **kw):
        seen.extend(m["ref"] for m in json.loads(prompt[-1]["content"])["messages"])
        return '{"operations":[],"questions":[]}'

    before = empty()
    with pytest.raises(ValueError, match="两次"):
        await consolidate(messages, cfg, generate, memory=before)
    assert before == empty()
    seen.clear()
    result = await consolidate(messages, cfg, generate, memory=before, budget=CallBudget(manual=True))
    assert result == empty()
    assert seen == [f"H{i}" for i in range(1, len(messages) + 1)]


def test_capacity_overflow_does_not_drop_open_items_or_mutate_old_state():
    cfg = Settings(memory_capacity_tokens=512)
    messages = {m["id"]: m for m in history()}
    old = empty()
    with pytest.raises(ValueError, match="活跃记忆"):
        apply_operations(
            old,
            proposal(category="open", content="界" * 600),
            messages,
            messages,
            [],
            fresh_ids=set(messages),
            settings=cfg,
        )
    assert old == empty()


def test_manual_deletion_references_survive_a_fork(tmp_path):
    from tavern.storage.store import Store

    store = Store(tmp_path)
    card = store.import_card(b'{"name":"a","first_mes":"hello"}', "c.json")
    session = store.new_session(SessionCreate(card_id=card["id"]))
    record = Record(
        id="x",
        category="episode",
        content="hello",
        sources=[{"message_id": session["head"], "excerpt": "hello"}],
    ).model_dump()
    snapshot = session["snapshot"]
    install_memory(snapshot, {"version": 3, "records": [record]})
    with store.connection() as db:
        db.execute("UPDATE messages SET snapshot=? WHERE id=?", (dump(snapshot), session["head"]))
    edited = store.edit_memory_record(session["id"], RecordEdit(expected_revision=0, action="delete", id="x"))
    fork = store.fork_session(session["id"], session["head"], edited["revision"])
    deleted = fork["snapshot"]["memory_document"]["records"][0]
    assert deleted["deleted_sources"][0]["message_id"] == fork["head"]


async def test_record_api_revision_and_fork(tmp_path):
    app = create_app(tmp_path)
    store = app.state.store
    card = store.import_card(
        json.dumps({"name": "她", "first_mes": "她把戒指借给了你。"}).encode(), "card.json"
    )
    session = store.new_session(SessionCreate(card_id=card["id"]))
    mid = session["head"]
    record = Record(
        id="ring",
        category="current",
        content="玩家保管戒指",
        sources=[{"message_id": mid, "fingerprint": source_hash(session["messages"][0]), "excerpt": "戒指"}],
    ).model_dump()
    snapshot = session["snapshot"]
    install_memory(snapshot, {"version": 3, "records": [record]})
    with store.connection() as db:
        db.execute("UPDATE messages SET snapshot=? WHERE id=?", (dump(snapshot), mid))
    fork = store.fork_session(session["id"], mid, 0)
    assert fork["memory_view"]["records"][0]["sources"][0]["message_id"] == fork["head"]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1") as client:
        response = await client.put(
            f"/api/sessions/{session['id']}/memory/records",
            json={
                "action": "edit",
                "id": "ring",
                "content": "手工版本",
                "category": "current",
                "expected_revision": 0,
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["memory_view"]["records"][0]["manual"]
    with pytest.raises(Conflict):
        store.edit_memory_record(session["id"], RecordEdit(action="delete", id="ring", expected_revision=0))
    assert fork["memory_view"]["records"][0]["content"] == "玩家保管戒指"
    await app.state.service.close()


async def test_rebuild_memory_saves_without_content_review(tmp_path):
    from tests.support.tracker_model import MemoryModel

    app = create_app(tmp_path)
    store = app.state.store
    card = store.import_card(b'{"name":"a","first_mes":"hello"}', "c.json")
    s = store.new_session(SessionCreate(card_id=card["id"]))
    s = store.update_session(s["id"], 0, policy="preview")
    app.state.service.memory_provider_factory = lambda: MemoryModel(
        [json.dumps(proposal(rid="N1", content="她打了招呼", sources=["H1#1"]))]
    )
    turn = app.state.service.submit(
        s["id"], TurnRequest(operation_id=uid(), expected_revision=s["revision"], scene_refresh="memory")
    )
    await app.state.service.tasks[turn["id"]]
    assert store.turn(turn["id"])["status"] == "committed"
    assert store.session(s["id"])["snapshot"]["memory_document"]["records"][0]["content"] == "她打了招呼"
    assert not any(e["kind"] == "awaiting_review" for e in store.events(turn["id"]))
    assert estimate_batches(s["messages"], empty(), {}, "用户", store.settings().for_role("background")) == 1
    await app.state.service.close()
