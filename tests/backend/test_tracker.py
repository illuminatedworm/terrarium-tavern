import copy
import json

import httpx
import pytest
from kohakuterrarium.testing.llm import ScriptedLLM

from tavern.application.service import RoleplayRuntime
from tavern.core.models import SessionCreate, TurnRequest
from tavern.core.prompts import DEFAULT_MAINTENANCE_PROMPT, DEFAULT_MEMORY_PROMPT, DEFAULT_TRACKER_PROMPT
from tavern.core.settings import SettingsDocument, migrate_settings
from tavern.memories.memory_engine import CallBudget
from tavern.memories.memory_records import Record, RecordEdit, manual_edit, source_hash
from tavern.storage.store import Store
from tavern.tracker.task import TaskBudget, TrackerTask
from tests.support.tracker_model import TrackerModel, batch_payload


@pytest.fixture
def story(tmp_path):
    store = Store(tmp_path)
    document = {"spec": "chara_card_v2", "spec_version": "2.0", "opaque": {"keep": 1}, "data": {
        "name": "Alice", "first_mes": "Alice greets Bob.", "description": "A guide.",
        "character_book": {"entries": [{"id": 1, "comment": "[InitVar]", "content": "hp: 5", "enabled": True, "keys": []}]},
        "system_prompt": "CARD_WRITER_ONLY", "post_history_instructions": "POST_WRITER_ONLY",
        "mes_example": "EXAMPLE_WRITER_ONLY", "extensions": {"unrelated": {"keep": True}}}}
    card = store.import_card(json.dumps(document).encode(), "card.json")
    session = store.new_session(SessionCreate(card_id=card["id"]))
    settings = store.settings()
    settings.system_prompt = "WRITER_ONLY"
    settings.preference_prompt = "PREFERENCE_ONLY"
    return store, card, session, settings


def task(story, model=None, **kwargs):
    store, card, session, settings = story
    return TrackerTask(store, "fixture", settings, session, card["document"], session["messages"],
                       session["snapshot"], phase="post", provider=model, **kwargs)


def memory_write(messages):
    batch = batch_payload(messages)
    source = batch["messages"][-1]["passages"][0]["ref"]
    return ("write_memory", {"operations": [{"op": "add", "record": {
        "category": "episode", "content": "Alice greets Bob.", "sources": [source]}}], "questions": []})


def test_tracker_settings_migration_and_validation():
    old = SettingsDocument().model_dump()
    old["half_agent"].pop("tracker_system_prompt")
    old["half_agent"].pop("tracker_max_steps")
    old["half_agent"].update(memory_system_prompt=DEFAULT_MEMORY_PROMPT,
                             maintenance_system_prompt=DEFAULT_MAINTENANCE_PROMPT)
    original = copy.deepcopy(old)
    current = migrate_settings(old)
    assert old == original
    assert current.half_agent.tracker_system_prompt == DEFAULT_TRACKER_PROMPT
    assert current.half_agent.tracker_max_steps == 8
    assert current.revisions["half_agent"] == old["revisions"]["half_agent"] + 1
    assert migrate_settings(current.model_dump()) == current
    old["half_agent"].update(memory_system_prompt="原样保留 {{user}} 的自定义。\n", maintenance_system_prompt="保留另一段。")
    custom = migrate_settings(old).half_agent.tracker_system_prompt
    assert "## 记忆整理时\n原样保留 {{user}} 的自定义。\n" in custom
    assert "## 设定维护时\n保留另一段。" in custom
    for invalid in (1, 101, True, "8"):
        with pytest.raises(ValueError):
            SettingsDocument(half_agent={"tracker_max_steps": invalid})


def test_tracker_upgrade_backs_up_before_writing_and_only_once(tmp_path):
    import sqlite3
    store = Store(tmp_path)
    old = store.settings_document().model_dump()
    old["half_agent"].pop("tracker_system_prompt")
    old["half_agent"].pop("tracker_max_steps")
    old["half_agent"]["memory_system_prompt"] = "保留自定义记忆要求"
    old["half_agent"]["maintenance_system_prompt"] = DEFAULT_MAINTENANCE_PROMPT
    with store.connection() as db:
        db.execute("UPDATE meta SET value=? WHERE key='settings'", (json.dumps(old),))
    current = Store(tmp_path)
    backups = list(tmp_path.glob("settings-before-tracker-*.sqlite3"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0].resolve().as_uri() + "?mode=ro", uri=True) as db:
        saved = json.loads(db.execute("SELECT value FROM meta WHERE key='settings'").fetchone()[0])
        assert saved == old
    assert "保留自定义记忆要求" in current.settings_document().half_agent.tracker_system_prompt
    assert Store(tmp_path).settings_document() == current.settings_document()
    assert len(list(tmp_path.glob("settings-before-tracker-*.sqlite3"))) == 1


async def test_independent_context_and_task_permissions(story):
    model = TrackerModel("done")
    work = task(story, model)
    work.document["data"].update(personality="谨慎", scenario="开局只有六件货物。")
    await work.run()
    wire = json.dumps(model.requests[0], ensure_ascii=False)
    for forbidden in ("WRITER_ONLY", "PREFERENCE_ONLY", "CARD_WRITER_ONLY", "POST_WRITER_ONLY", "EXAMPLE_WRITER_ONLY"):
        assert forbidden not in wire
    payload = next(json.loads(m["content"]) for m in model.requests[0]["messages"]
                   if m["content"].startswith('{"task":'))
    assert payload["character"] == {"name": "Alice", "description": "A guide.",
                                    "personality": "谨慎", "scenario": "开局只有六件货物。"}
    names = {t["function"]["name"] for t in model.requests[0]["extra_body"]["tools"]}
    assert not names & {"write_memory", "patch_state", "revise_content"}
    for name in ("write_memory", "patch_state", "revise_content"):
        with pytest.raises(ValueError, match="不允许"):
            work.execute(name, {})


async def test_memory_and_variables_share_native_loop(story):
    model = TrackerModel(memory_write, ("patch_state", {"operations": [], "comment": "No change"}), "done")
    work = task(story, model, memory=True, variables=True)
    work.document["data"]["character_book"]["entries"].append({
        "id": "rule", "content": "MVU_HP_RULE {{user}}",
        "extensions": {"terrarium": {"audience": "tracker_mvu"}}})
    await work.run(memory_start=0, memory_budget=CallBudget())
    assert len(model.requests) == 3
    assert work.patch == []
    assert len(work.snapshot["memory_document"]["records"]) == 1
    record = work.snapshot["memory_document"]["records"][0]
    assert record["sources"][0]["message_id"] == story[2]["head"]
    first = model.requests[0]["messages"]
    [rules] = [m["content"] for m in first if m["content"].startswith("<mvu_rules>\n")]
    assert "MVU_HP_RULE " + work.persona in rules and rules.endswith("\n</mvu_rules>")
    assert model.requests[1]["messages"][:len(first)] == first
    assert model.requests[1]["messages"][-1]["role"] == "tool"
    assert json.loads(model.requests[1]["messages"][-1]["content"])["memory_complete"]
    assert "待最终 MVU" in model.requests[2]["messages"][-1]["content"]


@pytest.mark.parametrize("name", ["patch_state", "revise_content"])
async def test_comment_argument_recovers_from_old_name_in_tracker_loop(story, name):
    operations = [{"op": "add", "path": "/hp", "value": 3}] if name == "patch_state" else [
        {"op": "replace", "path": "/description", "value": "An experienced guide."}]
    read = ("read_state", {}) if name == "patch_state" else ("read_character", {"field": "description"})
    comment = "简短操作备注"
    model = TrackerModel(read, (name, {"operations": operations, "reason": "旧参数备注"}),
                         (name, {"operations": operations, "comment": comment}), "done")
    work = task(story, model, variables=name == "patch_state", content=name == "revise_content")
    await work.run()
    schema = next(tool["function"]["parameters"] for tool in model.requests[0]["extra_body"]["tools"]
                  if tool["function"]["name"] == name)
    assert set(schema["properties"]) == {"operations", "comment"}
    assert "comment" in schema["required"]
    outputs = [json.loads(message["content"]) for message in model.requests[-1]["messages"]
               if message["role"] == "tool"]
    assert "comment" in outputs[-2]["error"] and "reason" in outputs[-2]["error"]
    assert outputs[-1]["saved_to_draft"] and work.completed
    if name == "patch_state":
        assert work.patch == operations and work.variables["hp"] == 3
        assert work.patch_reason == comment
    else:
        assert work.document["data"]["description"] == "An experienced guide."
        assert work.snapshot["content"]["reason"] == comment
    events = story[0].events("fixture")
    proposal = next(event for event in events if event["kind"] == (
        "state_proposal" if name == "patch_state" else "content_proposal"))
    assert proposal["payload"]["reason"] == comment
    assert sum(event["kind"] in {"state_proposal", "content_proposal"} for event in events) == 1


async def test_deleted_memory_cannot_be_readded_by_tool(story):
    work = task(story, TrackerModel(memory_write, "done"), memory=True)
    original = work.history[0]
    record = Record(category="episode", content="Alice greets Bob.", sources=[{
        "message_id": original["id"], "excerpt": original["raw"], "fingerprint": source_hash(original)}]).model_dump()
    work.memory = manual_edit({"version": 1, "records": [record]},
                             RecordEdit(expected_revision=0, action="delete", id=record["id"]))
    await work.run(memory_start=0, memory_budget=CallBudget())
    assert [r["status"] for r in work.snapshot["memory_document"]["records"]] == ["deleted"]


def test_card_paths_are_data_relative_and_lossless(story):
    work = task(story, content=True)
    original = copy.deepcopy(work.document)
    work.execute("revise_content", {"operations": [{"op": "replace", "path": "/description", "value": "An experienced guide."}],
                                    "comment": "Long-term experience"})
    assert work.document["data"]["description"] == "An experienced guide."
    assert work.document["opaque"] == original["opaque"]
    assert work.document["data"]["extensions"] == original["data"]["extensions"]
    assert work.execute("read_character", {"field": "description"}) == "An experienced guide."
    assert work.snapshot["content"]["document"] == work.document
    assert story[1]["document"] == original
    work.document = copy.deepcopy(original["data"])
    work.execute("revise_content", {"operations": [{"op": "replace", "path": "/description", "value": "Flat card."}],
                                    "comment": "Same path"})
    assert work.document["description"] == "Flat card."


def test_patch_staging_rejects_repetition_and_bad_paths(story):
    work = task(story, variables=True)
    before = copy.deepcopy(work.snapshot)
    with pytest.raises(Exception):
        work.execute("patch_state", {"operations": [{"op": "replace", "path": "/missing", "value": 1}], "comment": "bad"})
    assert work.patch is None
    result = work.execute("patch_state", {"operations": [{"op": "add", "path": "/hp", "value": 3}], "comment": "injury"})
    assert "待最终" in result["notice"]
    assert work.snapshot == before
    assert work.execute("read_state", {"path": "hp"}) == 3
    with pytest.raises(ValueError, match="重复"):
        work.execute("patch_state", {"operations": [], "comment": "repeat"})


def test_variable_parent_replace_restores_hidden_metadata():
    from tavern.tracker.state import stage
    original = {"bag": {"id": "a" * 32, "count": 1, "checksum": "b" * 64, "$meta": {"readonly": True}}}
    operations, updated = stage(original, [{"op": "replace", "path": "/bag", "value": {"count": 2}}])
    assert updated["bag"] == {**original["bag"], "count": 2}
    assert operations[0]["value"] == updated["bag"]
    for path in ("/bag/$meta", "/bag/checksum", "/constructor"):
        with pytest.raises(ValueError):
            stage(original, [{"op": "replace", "path": path, "value": {}}])
    with pytest.raises(ValueError):
        stage({}, [{"op": "add", "path": "/bag", "value": {"$internal": "injected"}}])
    assert original["bag"]["count"] == 1
    keyed = {"items": [{"id": "a", "checksum": "A"}, {"id": "b", "checksum": "B"}]}
    _, reordered = stage(keyed, [{"op": "replace", "path": "/items", "value": [{"id": "b"}, {"id": "a"}]}])
    assert [item["checksum"] for item in reordered["items"]] == ["B", "A"]
    _, replaced = stage(keyed, [{"op": "replace", "path": "/items", "value": [{"name": "new"}]}])
    assert replaced["items"] == [{"name": "new"}]


async def test_review_arrives_in_tool_result_and_shares_budget(story, monkeypatch):
    from tavern.memories import memory_engine
    def evidence(pool, query, settings, **kwargs):
        return [{"id": "extra" if kwargs else "first", "version": "v1", "kind": "character",
                 "historical": True, "text": "A gift." if kwargs else "A ring."}]
    monkeypatch.setattr(memory_engine, "select_evidence", evidence)
    def first(messages):
        result = memory_write(messages)[1]
        result["operations"][0]["record"]["category"] = "uncertain"
        result["questions"] = [{"topic": "ownership", "query": "Was it a gift?", "record_ids": ["N1"]}]
        return "write_memory", result
    def review(messages):
        data = batch_payload(messages)
        assert data["mode"] == "review"
        record = data["previous_result"]["records"][0]
        return "write_memory", {"operations": [{"op": "revise", "id": record["id"], "record": {
            "category": "episode", "sources": [data["messages"][-1]["passages"][0]["ref"]],
            "setting_refs": [data["evidence"][-1]["id"]]}}]}
    model = TrackerModel(first, review, "done")
    work = task(story, model, memory=True)
    budget = CallBudget()
    await work.run(memory_start=0, memory_budget=budget)
    assert budget.review == 1 and work.budget.calls == 3
    assert len(model.requests[1]["messages"]) == len(model.requests[0]["messages"]) + 2
    assert model.requests[1]["messages"][-1]["role"] == "tool"
    assert work.snapshot["memory_document"]["records"][0]["category"] == "episode"


async def test_failed_optional_review_keeps_uncertainty_and_sent_prefix(story, monkeypatch):
    from tavern.memories import memory_engine
    monkeypatch.setattr(memory_engine, "select_evidence", lambda pool, query, settings, **kwargs: [
        {"id": "extra" if kwargs else "first", "kind": "character", "historical": True,
         "text": "new evidence" if kwargs else "old evidence"}])
    def propose(messages):
        result = memory_write(messages)[1]
        result["operations"][0]["record"]["category"] = "uncertain"
        result["questions"] = [{"topic": "identity", "query": "Who?", "record_ids": ["N1"]}]
        return "write_memory", result
    model = TrackerModel(propose, TimeoutError("review timeout"), "done")
    work = task(story, model, memory=True)
    await work.run(memory_start=0, memory_budget=CallBudget())
    assert work.memory["records"][0]["category"] == "uncertain"
    sent = model.requests[1]["messages"]
    assert model.requests[2]["messages"][:len(sent)] == sent
    assert model.requests[2]["messages"][-1]["role"] == "system"


async def test_two_batches_share_loop_and_never_expose_future_body_early(story, monkeypatch):
    from tavern.memories import memory_engine
    original = memory_engine.plan_batch
    def one_exchange(history, expanded, start, memory, document, settings, persona=None):
        end = next(i + 1 for i in range(start, len(history)) if history[i]["role"] == "assistant")
        return original(history[:end], expanded[:end], start, memory, document, settings, persona)
    monkeypatch.setattr(memory_engine, "plan_batch", one_exchange)
    model = TrackerModel(memory_write, memory_write, ("patch_state", {"operations": [], "comment": "No change"}), "done")
    work = task(story, model, memory=True, variables=True, narrative="FUTURE_BODY")
    work.history.extend([{"id": "new-u", "role": "user", "raw": "Continue", "snapshot": {}},
                         {"id": "new-a", "role": "assistant", "raw": "FUTURE_BODY", "snapshot": {}}])
    await work.run(memory_start=0, memory_budget=CallBudget())
    assert "FUTURE_BODY" not in json.dumps(model.requests[0])
    assert "FUTURE_BODY" in json.dumps(model.requests[1])
    assert work.budget.calls == 4 and len(work.memory["records"]) == 2
    for first, second in zip(model.requests, model.requests[1:]):
        assert second["messages"][:len(first["messages"])] == first["messages"]


async def test_manual_rebuild_uses_bounded_contexts_and_per_batch_steps(story, monkeypatch):
    from tavern.memories import memory_engine
    original = memory_engine.plan_batch
    def one_exchange(history, expanded, start, memory, document, settings, persona=None):
        end = next(i + 1 for i in range(start, len(history)) if history[i]["role"] == "assistant")
        return original(history[:end], expanded[:end], start, memory, document, settings, persona)
    monkeypatch.setattr(memory_engine, "plan_batch", one_exchange)
    model = TrackerModel(memory_write, memory_write, memory_write, "done")
    work = task(story, model, memory=True, budget=TaskBudget(2))
    work.history.extend([{"id": f"message-{i}", "role": "user" if i % 2 else "assistant",
                          "raw": f"passage-{i}", "snapshot": {}} for i in range(1, 5)])
    await work.run(memory_start=0, memory_budget=CallBudget(manual=True))
    assert work.total_calls == 4 and work.budget.calls == 2
    assert len(work.memory["records"]) == 3
    assert not any(m["role"] == "tool" for m in model.requests[1]["messages"])
    assert not any(m["role"] == "tool" for m in model.requests[2]["messages"])


async def test_chat_completions_wire_carries_memory_and_variable_tools(story):
    from tavern.scripting.model_transport import ModelTransport
    store, _, _, settings = story
    cfg = settings.for_role("background")
    cfg.model = "fixture"
    cfg.api_mode = "chat_completions"
    cfg.base_url = "https://fixture.test"
    requests = []
    def route(request):
        body = json.loads(request.content)
        requests.append(body)
        assert any(m["role"] == "user" for m in body["messages"])
        message = {"role": "assistant", "content": "done"}
        if len(requests) == 1:
            message = {"role": "assistant", "content": None, "tool_calls": [
                {"id": "memory", "type": "function", "function": {"name": "write_memory", "arguments": '{"operations":[],"questions":[]}'}},
                {"id": "variables", "type": "function", "function": {"name": "patch_state", "arguments": '{"operations":[],"comment":"No change"}'}},
            ]}
        return httpx.Response(200, json={"id": "fixture", "object": "chat.completion", "created": 0, "model": "fixture",
            "choices": [{"index": 0, "message": message, "finish_reason": "tool_calls" if len(requests) == 1 else "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})
    provider = ModelTransport(cfg, "isolated-fixture-key", http_client=httpx.AsyncClient(transport=httpx.MockTransport(route)))
    try:
        work = task(story, provider, memory=True, variables=True)
        work.user_input = "完整用户输入尾部"
        work.narrative = "完整剧情正文尾部"
        await work.run(memory_start=0, memory_budget=CallBudget())
        initial = requests[0]["messages"][-1]
        assert initial["role"] == "user"
        payload = json.loads(initial["content"])
        assert payload["current_input"] == "完整用户输入尾部"
        assert payload["current_narrative"] == "完整剧情正文尾部"
        names = {t["function"]["name"] for t in requests[0]["tools"]}
        assert {"write_memory", "patch_state"} <= names
        assert "revise_content" not in names
        assert [m["tool_call_id"] for m in requests[1]["messages"] if m["role"] == "tool"] == ["memory", "variables"]
        assert work.patch == [] and len(requests) == 2
    finally:
        await provider.close()


async def test_anthropic_tracker_marks_execution_and_recall_failures_only(story):
    from tavern.scripting.model_transport import ModelTransport
    settings = story[3].for_role("background")
    settings.api_mode = "anthropic"
    settings.model = "fixture"
    requests = []
    actions = [
        ("read_character", {"field": "extensions.diagnostic"}),
        ("read_character", {"unexpected": True}),
        ("tavern_recall_history", {"query": ""}),
        ("tavern_recall_history", {"query": "NO_MATCH_924"}),
        ("tavern_recall_history", {"query": "NO_MATCH_924"}),
        ("tavern_recall_history", {"query": "quota_exhausted"}),
        ("patch_state", {"operations": [], "comment": "未授权"}),
    ]

    def route(request):
        requests.append(json.loads(request.content))
        first = len(requests) == 1
        content = ([{"type": "tool_use", "id": f"call{i}", "name": name, "input": args}
                    for i, (name, args) in enumerate(actions)] if first else [{"type": "text", "text": "done"}])
        return httpx.Response(200, json={"content": content, "stop_reason": "tool_use" if first else "end_turn"})

    provider = ModelTransport(settings, "fixture", http_client=httpx.AsyncClient(transport=httpx.MockTransport(route)))
    try:
        work = task(story, provider)
        work.document["data"]["extensions"]["diagnostic"] = {"error": "user-authored data", "ok": False}
        await work.run()
        results = [b for m in requests[1]["messages"] for b in m.get("content", [])
                   if isinstance(b, dict) and b.get("type") == "tool_result"]
        assert [b["tool_use_id"] for b in results] == [f"call{i}" for i in range(len(actions))]
        assert [b.get("is_error", False) for b in results] == [False, True, True, False, False, True, True]
        assert json.loads(results[0]["content"])["error"] == "user-authored data"
        assert "参数错误" in json.loads(results[2]["content"])["notice"]
        assert "3次上限" in json.loads(results[5]["content"])["error"]
        assert "_tool_error" not in json.dumps(requests)
        assert work.completed and work.patch is None
    finally:
        await provider.close()


def test_script_lore_merge_preserves_tracker_changes_and_containers():
    from tavern.character.cards import lore_entries
    from tavern.tracker.workflow import merge_script_lore, sync_runtime_lore
    original = {"spec": "card", "data": {"name": "A", "character_book": {"entries": {
        "one": {"id": "one", "content": "old", "extensions": {"private": "keep"}},
        "two": {"id": "two", "content": "stable"}}}}}
    changed = copy.deepcopy(original)
    changed["data"]["character_book"]["entries"]["one"]["content"] = "tracker edit"
    mirror = {"lore": lore_entries(original)}
    mirror["lore"][0]["name"] = "earlier same-entry edit"
    mirror["lore"][1]["content"] = "earlier script edit"
    changed = merge_script_lore(changed, mirror["lore"], {"loreModified": True, "lore": mirror["lore"]}, include_existing=True)
    changed["data"]["character_book"]["entries"]["one"]["content"] = "tracker edit"
    sync_runtime_lore(mirror, changed, original)
    assert mirror["lore"][0]["content"] == "tracker edit"
    assert mirror["lore"][1]["content"] == "earlier script edit"
    initial = copy.deepcopy(mirror["lore"])
    mirror["lore"][0]["comment"] = "callback title"
    final = merge_script_lore(changed, initial, {"loreModified": True, "lore": mirror["lore"]})
    entries = final["data"]["character_book"]["entries"]
    assert isinstance(entries, dict)
    assert entries["one"]["content"] == "tracker edit"
    assert entries["one"]["comment"] == "callback title"
    assert entries["one"]["extensions"] == {"private": "keep"}
    assert entries["one"]["name"] == "earlier same-entry edit"
    assert entries["two"]["content"] == "earlier script edit"


async def test_incomplete_required_tools_exhaust_budget(story):
    work = task(story, TrackerModel("done", "done"), variables=True, budget=TaskBudget(2))
    with pytest.raises(ValueError, match="上限"):
        await work.run()


async def send(service, store, session, **options):
    turn = service.submit(session["id"], TurnRequest(text="Continue", operation_id=str(session["revision"]),
                                                    expected_revision=session["revision"], **options))
    await service.tasks[turn["id"]]
    return store.turn(turn["id"]), store.session(session["id"])


async def test_post_cycle_and_variables_share_task_and_bind_final_sources(story):
    store, _, session, settings = story
    settings.memory_interval_turns = 1
    settings.chunk_mode = "off"
    store.save_settings(settings)
    service = RoleplayRuntime(store)
    writer = ScriptedLLM(["Alice greets Bob."])
    model = TrackerModel(memory_write, ("patch_state", {"operations": [{"op": "add", "path": "/hp", "value": 3}],
                                                       "comment": "This turn"}), "done")
    service.provider_factory = lambda: writer
    service.tracker_provider_factory = lambda: model
    try:
        turn, current = await send(service, store, session, update_variables=True)
        assert turn["status"] == "committed", (turn["error"], [e for e in store.events(turn["id"]) if e["kind"] == "tracker_failed"])
        assert writer.call_count == 1 and len(model.requests) == 3
        assert current["snapshot"]["state"]["stat_data"]["hp"] == 3
        assert current["snapshot"]["memory_archive"]["covered_count"] == 3
        source = current["snapshot"]["memory_document"]["records"][0]["sources"][0]
        assert source["message_id"] == current["head"]
        assert source["fingerprint"] == source_hash(current["messages"][-1])
        assert model.closed
    finally:
        await service.close()


@pytest.mark.parametrize("variables", [False, True])
async def test_optional_failure_vs_required_atomic_failure(story, variables):
    store, _, session, settings = story
    settings.memory_interval_turns = 1
    settings.chunk_mode = "off"
    store.save_settings(settings)
    service = RoleplayRuntime(store)
    service.provider_factory = lambda: ScriptedLLM(["Alice waits."])
    service.tracker_provider_factory = lambda: TrackerModel(RuntimeError("fixture failure"))
    try:
        turn, current = await send(service, store, session, update_variables=variables)
        assert turn["status"] == ("error" if variables else "committed")
        if variables:
            assert current["snapshot"] == session["snapshot"]
            assert current["head"] == session["head"]
            assert turn["draft"] == "Alice waits."
        else:
            assert current["snapshot"]["memory_warning"] == "fixture failure"
            assert current["snapshot"].get("memory_archive") == session["snapshot"].get("memory_archive")
    finally:
        await service.close()


async def test_pre_edit_reaches_writer_and_variable_only_cannot_write_memory(story):
    store, _, session, settings = story
    settings.memory_interval_turns = 10
    store.save_settings(settings)
    service = RoleplayRuntime(store)
    writer = ScriptedLLM(["Alice waits."])
    pre = TrackerModel(("revise_content", {"operations": [{"op": "replace", "path": "/description", "value": "NEW_SETTING"}],
                                         "comment": "Explicit edit"}), "done")
    post = TrackerModel(("patch_state", {"operations": [], "comment": "No change"}), "done")
    providers = iter([pre, post])
    service.provider_factory = lambda: writer
    service.tracker_provider_factory = lambda: next(providers)
    try:
        turn, current = await send(service, store, session, allow_content_edit=True, update_variables=True)
        assert turn["status"] == "committed", turn["error"]
        assert "NEW_SETTING" in json.dumps(writer.last_messages)
        names = {t["function"]["name"] for t in post.requests[0]["extra_body"]["tools"]}
        assert "write_memory" not in names
        assert current["snapshot"]["content"]["document"]["data"]["description"] == "NEW_SETTING"
    finally:
        await service.close()


async def test_optional_task_discards_partial_card_changes(story):
    store, card, session, settings = story
    settings.memory_interval_turns = 1
    settings.chunk_mode = "off"
    store.save_settings(settings)
    session = store.update_session(session["id"], session["revision"], policy="evolving")
    service = RoleplayRuntime(store)
    service.provider_factory = lambda: ScriptedLLM(["Alice waits."])
    service.tracker_provider_factory = lambda: TrackerModel(
        ("revise_content", {"operations": [{"op": "replace", "path": "/description", "value": "partial"}],
                            "comment": "Provisional change"}), RuntimeError("after edit"))
    try:
        turn, current = await send(service, store, session)
        assert turn["status"] == "committed", turn["error"]
        assert current["snapshot"].get("content") == session["snapshot"].get("content")
        assert store.session_card(session["id"])["document"] == card["document"]
        assert current["snapshot"]["memory_warning"] == "after edit"
    finally:
        await service.close()


async def test_mvu_callbacks_run_once_after_repaired_tool_and_on_regeneration(tmp_path):
    store = Store(tmp_path)
    doc = {"name": "Counter", "first_mes": "hello", "character_book": {"entries": [{
        "id": 1, "comment": "[InitVar]", "keys": [], "content": "hp: 5\nupdates: 0\nmessages: 0"}]},
        "extensions": {"tavern_helper": {"scripts": [{"id": "counter", "enabled": True, "content":
            "eventOn(Mvu.events.VARIABLE_UPDATE_ENDED, data => { data.stat_data.updates += 1; });"
            "eventOn('MESSAGE_RECEIVED', async () => { const data = getVariables({type:'chat'});"
            "data.stat_data.messages += 1; await replaceVariables(data,{type:'chat'}); });"}]}}}
    card = store.import_card(json.dumps(doc).encode(), "counter.json")
    with store.connection() as db:
        db.execute("UPDATE cards SET permission=1 WHERE id=?", (card["id"],))
    session = store.new_session(SessionCreate(card_id=card["id"]))
    service = RoleplayRuntime(store)
    service.provider_factory = lambda: ScriptedLLM(["A small injury."])
    def model():
        return TrackerModel(("patch_state", {"operations": [{"op": "replace", "path": "/missing", "value": 0}], "comment": "bad path"}),
            ("patch_state", {"operations": [{"op": "replace", "path": "/hp", "value": 4}], "comment": "injury"}),
            ("patch_state", {"operations": [{"op": "replace", "path": "/hp", "value": 3}], "comment": "duplicate"}), "done")
    service.tracker_provider_factory = model
    calls = []
    original_open = service.runtime.open
    async def observed(*args, **kwargs):
        runtime = await original_open(*args, **kwargs)
        original_call = runtime.call
        async def tracked(name, *values, **options):
            if name == "process":
                calls.append(values)
            return await original_call(name, *values, **options)
        runtime.call = tracked
        return runtime
    service.runtime.open = observed
    try:
        turn, current = await send(service, store, session, update_variables=True)
        assert turn["status"] == "committed", turn["error"]
        assert current["snapshot"]["state"]["stat_data"] == {"hp": 4, "updates": 1, "messages": 1}
        turn, regenerated = await send(service, store, current, mode="regenerate", update_variables=True)
        assert turn["status"] == "committed", turn["error"]
        assert regenerated["snapshot"]["state"]["stat_data"] == current["snapshot"]["state"]["stat_data"]
        assert len(calls) == 2
    finally:
        await service.close()
