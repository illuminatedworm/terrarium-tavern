"""No paid calls: exercise the wire contract all the way back to stored evidence."""
import copy
import json
from types import SimpleNamespace

import pytest
from kohakuterrarium.llm.base import NativeToolCall

from tavern.core.models import Settings
from tavern.core.tokens import count_text
from tavern.memories.history_recall import HistoryBudget, HistoryRecall, complete_with_recall
from tavern.memories.memory_engine import CallBudget, consolidate
from tavern.memories.memory_protocol import References, split_passages
from tavern.memories.memory_records import Record, source_hash, with_source_status


def history(texts):
    return [{"id": f"long-random-message-identity-{i:032x}", "role": "assistant", "raw": text,
             "model": text, "snapshot": {"state": {}}} for i, text in enumerate(texts)]


def answer(record, *, action="add", rid=None, questions=None):
    op = {"op": action, "record": record}
    if rid:
        op["id"] = rid
    return json.dumps({"operations": [op], "questions": questions or []}, ensure_ascii=False)


async def test_minimal_wire_response_fills_exact_expanded_sources_without_long_identifiers():
    messages = history(["{{user}}借我戒指。", "她把戒指借给{{user}}。"])
    async def generate(prompt, **kwargs):
        wire = json.dumps(prompt, ensure_ascii=False)
        assert all(m["id"] not in wire for m in messages)
        payload = json.loads(prompt[-1]["content"])
        source = payload["messages"][-1]["passages"][0]
        assert source["text"] == "她把戒指借给旅人。"
        return answer({"category": "current", "content": "戒指暂由旅人保管", "sources": [source["ref"]]})
    result = await consolidate(messages, Settings(), generate, memory={"version": 1, "records": []}, persona="旅人", document={})
    record = result["records"][0]
    assert len(record["id"]) == 32
    assert record["sources"] == [{"message_id": messages[-1]["id"], "excerpt": "她把戒指借给旅人。",
                                  "fingerprint": source_hash(messages[-1])}]
    changed = copy.deepcopy(messages)
    changed[-1]["model"] = "她拒绝借出戒指。"
    assert with_source_status(result, changed)["records"][0]["source_stale"]


async def test_existing_record_alias_updates_stable_id_and_preserves_omitted_items():
    messages = history(["她收回了戒指。"])
    old = {"version": 1, "records": [Record(id="very-long-stored-ring-id", category="current", content="戒指借出").model_dump(),
        Record(id="very-long-stored-promise-id", category="open", content="明早见面").model_dump()]}
    async def generate(prompt, **kwargs):
        wire = json.dumps(prompt)
        assert "very-long-stored" not in wire
        return answer({"content": "戒指归还", "sources": ["S1"]}, action="revise", rid="R1")
    result = await consolidate(messages, Settings(), generate, memory=old)
    assert result["records"][0]["id"] == old["records"][0]["id"]
    assert result["records"][0]["content"] == "戒指归还"
    assert result["records"][1] == old["records"][1]
    assert old["records"][0]["content"] == "戒指借出"


async def test_only_manual_deletion_is_returned_to_tracker():
    old = {"version": 1, "records": [
        Record(id="automatic-retired", category="episode", content="自动退出的旧事",
               status="superseded").model_dump(),
        Record(id="automatic-deleted", category="episode", content="旧版自动删除项",
               status="deleted").model_dump(),
        Record(id="manual-deleted", category="episode", content="用户删掉的细节",
               status="deleted", manual=True).model_dump(),
        Record(id="active", category="current", content="仍需保留的进展").model_dump(),
    ]}
    projected = References().previous(old)
    assert [r["content"] for r in projected["records"]] == ["用户删掉的细节", "仍需保留的进展"]
    assert [r["id"] for r in projected["records"]] == ["R1", "R2"]

    async def generate(prompt, **kwargs):
        payload = json.loads(prompt[-1]["content"])
        assert payload["previous_result"] == projected
        assert "通常认为它不重要或冗余" in prompt[0]["content"]
        assert "superseded 只是程序内部待移除标记" in prompt[0]["content"]
        return '{"operations":[],"questions":[]}'

    result = await consolidate(history(["继续。"]), Settings(), generate, memory=old)
    assert [r["id"] for r in result["records"]] == ["manual-deleted", "active"]


async def test_setting_and_review_aliases_restore_versions_and_new_record_identity(monkeypatch):
    first = {"id": "random-setting-id-first", "text": "戒指归艾琳", "version": "random-version-one", "historical": True}
    extra = {"id": "random-setting-id-extra", "text": "借出不改变所有权", "version": "random-version-two", "historical": True}
    monkeypatch.setattr("tavern.memories.memory_engine.select_evidence", lambda *a, **kw: [extra if kw else first])
    phases = []
    async def generate(prompt, phase, **kwargs):
        phases.append(phase)
        assert "random-setting" not in json.dumps(prompt) and "random-version" not in json.dumps(prompt)
        if phase == "memory":
            return answer({"category": "uncertain", "content": "归属待确认", "sources": ["S1"], "setting_refs": ["E1"]},
                rid="N1", questions=[{"topic": "ownership", "query": "戒指归属", "record_ids": ["N1"]}])
        payload = json.loads(prompt[-1]["content"])
        assert payload["questions"][0]["record_ids"] == ["R1"]
        assert payload["evidence"][1]["version"] == "V2"
        return answer({"category": "current", "content": "艾琳仍拥有戒指", "sources": ["S1"], "setting_refs": ["E2"]},
                      action="revise", rid="R1")
    result = await consolidate(history(["艾琳把戒指借给旅人。"]), Settings(), generate, memory={"version": 1, "records": []})
    record = result["records"][0]
    assert phases == ["memory", "memory_review"]
    assert record["category"] == "current" and len(record["id"]) == 32
    assert record["setting_sources"][0]["version"] == extra["version"]
    assert record["setting_refs"] == [extra["id"]]


async def test_questions_can_reference_unnamed_adds_by_position():
    async def generate(prompt, **kwargs):
        return json.dumps({"operations": [
            {"op": "add", "record": {"category": category, "content": content,
                                     "sources": ["S1"]}}
            for category, content in [("episode", "一"), ("uncertain", "二待确认"),
                                      ("episode", "三"), ("uncertain", "四待确认")]
        ], "questions": [
            {"topic": "completion", "query": "第二项", "record_ids": ["N2"]},
            {"topic": "knowledge", "query": "第四项", "record_ids": ["N4"]},
        ]}, ensure_ascii=False)

    result = await consolidate(history(["一、二、三、四。"]),
        Settings(memory_review_enabled=False), generate, memory={"version": 1, "records": []})
    assert [r["content"] for r in result["records"]] == ["一", "二待确认", "三", "四待确认"]
    assert [r["category"] for r in result["records"]] == ["episode", "uncertain", "episode", "uncertain"]
    assert len({r["id"] for r in result["records"]}) == 4


def test_explicit_new_alias_takes_precedence_over_positional_alias():
    refs = References()
    proposal = {"operations": [{"op": "add", "record": {}},
                               {"op": "add", "id": "N1", "record": {}}],
                "questions": [{"record_ids": ["N1"]}]}
    resolved = refs.resolve(proposal)
    assert resolved["questions"][0]["record_ids"] == [resolved["operations"][1]["id"]]
    assert resolved["operations"][0]["id"] != resolved["operations"][1]["id"]


async def test_question_outside_add_positions_still_rejects_batch():
    async def generate(prompt, **kwargs):
        return answer({"category": "uncertain", "content": "待确认", "sources": ["S1"]},
                      questions=[{"topic": "knowledge", "query": "谁知情", "record_ids": ["N2"]}])

    with pytest.raises(ValueError, match="实际存在的待确认记忆"):
        await consolidate(history(["一件待确认的事。"]), Settings(memory_review_enabled=False),
            generate, memory={"version": 1, "records": []})


@pytest.mark.parametrize("sources,match", [(["S999"], "未知"), (["M1"], "未知"), (["S2"], "本批新增")])
async def test_unknown_or_background_only_citation_preserves_old_memory(sources, match):
    old = {"version": 1, "records": []}
    async def generate(prompt, **kwargs):
        return answer({"category": "episode", "content": "不能保存的结论", "sources": sources})
    with pytest.raises(ValueError, match=match):
        await consolidate(history(["旧事。", "新事。"]), Settings(), generate, memory=old, start=1)
    assert old == {"version": 1, "records": []}


@pytest.mark.parametrize("protected", ["manual", "source_stale"])
async def test_short_record_ids_do_not_bypass_edit_protection(protected):
    record = Record(id="persistent-protected", category="current", content="保留").model_dump()
    record[protected] = True
    async def generate(prompt, **kwargs):
        return answer({"content": "试图覆盖", "sources": ["S1"]}, action="revise", rid="R1")
    with pytest.raises(ValueError, match="不可自动修改"):
        await consolidate(history(["新事件。"]), Settings(), generate, memory={"version": 1, "records": [record]})


async def test_short_deleted_record_id_is_suppressed():
    record = Record(id="persistent-protected", category="current", content="保留",
                    status="deleted", manual=True).model_dump()
    events = []
    async def generate(prompt, **kwargs):
        return answer({"content": "试图覆盖", "sources": ["S1"]}, action="revise", rid="R1")
    result = await consolidate(history(["新事件。"]), Settings(), generate,
                               memory={"version": 1, "records": [record]},
                               event=lambda kind, payload: events.append(kind))
    assert result["records"][0]["status"] == "deleted"
    assert "memory_tombstone_suppressed" in events


def test_passages_are_lossless_and_never_join_disjoint_tool_results():
    text = "  她说：『不能送你。』\n" + "超长无标点" * 500 + "\n终句。"
    parts = list(split_passages(text))
    assert "".join(parts) == text and all(0 < len(p) <= 600 for p in parts)
    refs = References()
    refs.message({"id": "hidden-id", "role": "assistant", "raw": "不可见原文",
                  "exposed_excerpts": ["第一处", "另一处"]})
    assert [s["excerpt"] for s in refs.sources.values()] == ["第一处", "另一处"]


def test_tool_registers_only_returned_passages_within_real_token_limit():
    messages = history(["戒指" * 6000 + "禁止访问的尾部"])
    refs, settings = References(), Settings()
    recall = HistoryRecall(messages, settings, HistoryBudget(), references=refs)
    result = recall.execute({"turn": 1, "radius": 0})
    wire = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    assert count_text(wire, settings) <= 2048
    assert messages[0]["id"] not in wire and "禁止访问的尾部" not in wire
    exposed = {p["ref"]: p["text"] for item in result["rounds"] for p in item["passages"]}
    assert {k: v["excerpt"] for k, v in refs.sources.items()} == exposed
    assert all(k.startswith("H") for k in exposed)


async def test_native_tool_short_refs_use_batch_cutoff_and_resolve_history_evidence():
    messages = history(["艾琳借出银戒。"] + ["继续。"] * 5 + ["旅人归还银戒。", "未来剧情秘密。"])
    settings, budget = Settings(), CallBudget()
    class Provider:
        last_tool_calls = []
        calls = 0

        async def chat_complete(self, thread, **kwargs):
            self.calls += 1
            wire = json.dumps(thread, ensure_ascii=False)
            assert all(m["id"] not in wire for m in messages)
            assert "未来剧情秘密" not in wire
            if self.calls == 1:
                self.last_tool_calls = [NativeToolCall("call1", "tavern_recall_history", '{"query":"银戒"}')]
                return SimpleNamespace(content="", usage={})
            self.last_tool_calls = []
            result = json.loads(thread[-1]["content"])
            old_ref = next(p["ref"] for item in result["matches"] for p in item["passages"] if "借出" in p["text"])
            return SimpleNamespace(content=answer({"category": "episode", "content": "借出后已归还",
                                                  "sources": ["S1", old_ref]}), usage={})
    provider = Provider()
    async def generate(prompt, phase, **kwargs):
        return await complete_with_recall(provider, prompt, history=messages, settings=settings, budget=budget.history, phase=phase)
    result = await consolidate(messages[:-1], settings, generate, memory={"version": 1, "records": []}, start=6, budget=budget)
    assert provider.calls == 2 and budget.history.calls == 1
    assert {s["message_id"] for s in result["records"][0]["sources"]} == {messages[0]["id"], messages[6]["id"]}


async def test_optional_bad_ref_is_discarded_without_losing_valid_change():
    events = []
    async def generate(prompt, **kwargs):
        valid = json.loads(answer({"category": "current", "content": "戒指已归还", "sources": ["S1"]}))
        valid["operations"].append({"op": "add", "record": {"category": "episode", "content": "可选动作",
                                                            "callback": True, "sources": ["S999"]}})
        return json.dumps(valid)
    result = await consolidate(history(["她收回戒指。"]), Settings(), generate,
        memory={"version": 1, "records": []}, event=lambda k, p: events.append((k, p)))
    assert len(result["records"]) == 1 and result["records"][0]["content"] == "戒指已归还"
    assert any(k == "memory_records_discarded" for k, _ in events)


async def test_local_refs_reset_between_manual_batches_and_final_failure_is_atomic():
    messages = history([str(i) + "界" * 600 for i in range(24)])
    for i, message in enumerate(messages):
        message["role"] = "user" if i % 2 == 0 else "assistant"
    settings = Settings(memory_input_tokens=8000, memory_evidence_tokens=0)
    original = {"version": 1, "records": []}
    calls = []
    fail = False
    async def generate(prompt, **kwargs):
        payload = json.loads(prompt[-1]["content"])
        first = payload["messages"][0]["passages"][0]
        assert first["ref"] == "S1"
        calls.append(prompt.references.sources["S1"]["message_id"])
        return answer({"category": "episode", "content": "经历" + str(len(calls)),
                       "sources": ["S999" if fail and len(calls) == 2 else "S1"]})
    result = await consolidate(messages, settings, generate, memory=original, budget=CallBudget(manual=True))
    assert len(calls) > 1 and [r["sources"][0]["message_id"] for r in result["records"]] == calls
    assert len({r["id"] for r in result["records"]}) == len(calls)
    calls.clear()
    fail = True
    with pytest.raises(ValueError, match="未知"):
        await consolidate(messages, settings, generate, memory=original, budget=CallBudget(manual=True))
    assert original == {"version": 1, "records": []}
