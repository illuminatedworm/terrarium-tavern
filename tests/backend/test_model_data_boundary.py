"""Final request/tool views, not merely prompt wording. All providers are local."""
import copy
import json

import pytest
from kohakuterrarium.testing.llm import ScriptedLLM

from tavern.character.card_editor import CardEditorService
from tavern.conversation.context import compile_context, macros
from tavern.core.model_view import ModelView, state_for_model
from tavern.core.models import CardEditorRequest, ContextRecipe, SessionCreate, Settings
from tavern.core.tokens import count_text
from tavern.memories.memory_evidence import evidence_pool
from tavern.memories.memory_records import Record
from tavern.scripting.variables import analyze_variables
from tavern.storage.store import Store
from tavern.tracker.task import TrackerTask

ID = "12345678-90ab-cdef-1234-567890abcdef"
KEY = "long-storage-key-with-opaque-identity-019876543210"
HASH = "f" * 64


def document():
    return {"name": "艾琳", "first_mes": "旧港相遇。", "description": "人物设定", "character_book": {"entries": {
        KEY: {"id": ID, "content": "银戒属于艾琳", "comment": "银戒", "enabled": True, "fingerprint": HASH},
    }}, "extensions": {"custom": {"id": "custom-random-identity-012345678901234567890", "checksum": HASH,
                                   "code": f'const original = "{ID}";', "unchanged": "保留"}}}


def runtime_state():
    return {"stat_data": {"hp": 8, "$internal": {"cache_key": ID}, "$meta": {"schema_version": HASH}},
            "initialized_openings": {ID: True}, "initialized_lorebooks": {"card:" + ID: ["初始化正文" * 100]},
            "schema": {"id": HASH}, "display_data": {"hp": "5->8"}, "delta_data": {"hp": "+3"}}


def test_card_projection_restores_ids_keys_code_and_hidden_hashes_on_parent_replace():
    original = document()
    view = ModelView(original)
    compact = view.expose(original)
    wire = json.dumps(compact, ensure_ascii=False)
    for value in (ID, KEY, HASH, original["extensions"]["custom"]["id"]):
        assert value not in wire
    assert "fingerprint" not in wire and "checksum" not in wire
    compact["description"] = "改过的描述"
    result = view.patch(original, [{"op": "replace", "path": "", "value": compact}])
    expected = copy.deepcopy(original)
    expected["description"] = "改过的描述"
    assert result == expected
    assert original == document()
    assert count_text(wire, Settings()) < count_text(json.dumps(original, ensure_ascii=False), Settings())


def test_short_dictionary_path_and_split_reference_restore_without_model_generated_ids():
    doc = document()
    view = ModelView(doc)
    compact = view.expose(doc)
    key = next(iter(compact["character_book"]["entries"]))
    source = compact["character_book"]["entries"][key]["id"]
    result = view.patch(doc, [
        {"op": "replace", "path": "/character_book/entries/" + key + "/content", "value": "仍由艾琳拥有"},
        {"op": "add", "path": "/character_book/entries/new_rules", "value": {
            "content": "变量规则", "extensions": {"terrarium": {"split_from": source}}}},
    ])
    entries = result["character_book"]["entries"]
    assert entries[KEY]["id"] == ID and entries[KEY]["fingerprint"] == HASH
    assert len(entries["new_rules"]["id"]) == 32
    assert entries["new_rules"]["extensions"]["terrarium"]["split_from"] == ID


def test_unknown_refs_and_direct_hash_edits_fail_without_changing_document():
    doc, view = document(), ModelView(document())
    for op in [
        {"op": "replace", "path": "/description", "value": "@D9999"},
        {"op": "replace", "path": "/extensions/custom/checksum", "value": "伪造"},
        {"op": "replace", "path": "/extensions/custom", "value": {"checksum": "伪造"}},
    ]:
        with pytest.raises(ValueError):
            view.patch(doc, [op])
    assert doc == document()


def test_reorder_preserves_hidden_fields_by_identity_not_array_index():
    doc = {"name": "a", "character_book": {"entries": [
        {"id": ID, "content": "甲", "checksum": "a" * 64},
        {"id": "opaque-long-second-identity-01234567890", "content": "乙", "checksum": "b" * 64},
    ]}}
    view = ModelView(doc)
    rows = view.expose(doc)["character_book"]["entries"][::-1]
    changed = view.patch(doc, [{"op": "replace", "path": "/character_book/entries", "value": rows}])
    assert changed["character_book"]["entries"] == doc["character_book"]["entries"][::-1]


def test_json_pointer_escaping_and_array_insertion_do_not_corrupt_identity():
    key = KEY + "/literal~key"
    doc = {"name": "a", "character_book": {"entries": {key: {"id": ID, "content": "保留", "checksum": HASH}}}}
    view = ModelView(doc)
    pointer = view.pointer("/character_book/entries/" + key.replace("~", "~0").replace("/", "~1") + "/content")
    assert key not in pointer
    result = view.patch(doc, [{"op": "replace", "path": pointer, "value": "变更"}])
    assert result["character_book"]["entries"][key] == {"id": ID, "content": "变更", "checksum": HASH}
    doc["character_book"]["entries"] = [{"id": ID, "content": "保留", "checksum": HASH}]
    result = view.patch(doc, [{"op": "add", "path": "/character_book/entries/0", "value": {"content": "新增"}}])
    rows = result["character_book"]["entries"]
    assert rows[0]["id"] != ID and "checksum" not in rows[0]
    assert rows[1] == doc["character_book"]["entries"][0]


def test_writer_and_nested_context_exclude_ledgers_and_internal_state():
    state = runtime_state()
    snapshot = {"state": state, "memory_document": {"version": 1, "records": [Record(id=ID, category="episode", content="有约定", sources=[{"message_id": ID, "excerpt": "约定", "fingerprint": HASH}]).model_dump()]}, "lore": {}}
    doc = {"name": "艾琳", "description": "角色描述", "character_book": {"entries": [
        {"id": ID, "comment": "资料", "content": "普通设定", "constant": True}]}}
    session = {"id": ID, "persona": "用户", "recipe": ContextRecipe().model_dump()}
    compiled = compile_context(doc, session, [{"id": ID, "role": "assistant", "raw": "相遇。"}], snapshot,
                               "继续", Settings(), ID)
    wire = json.dumps(compiled["messages"], ensure_ascii=False)
    assert ID not in wire and HASH not in wire and "初始化正文" not in wire
    assert "普通设定" in wire
    assert any('"hp": 8' in m["content"] for m in compiled["messages"])
    assert state == runtime_state()
    assert state_for_model(state) == {"stat_data": {"hp": 8}}
    assert ID not in macros("{{getvar::initialized_openings}}", doc, "用户", state)
    # Script-triggered generation uses this exact compiler and messages view.
    nested = compile_context(doc, session, [], snapshot, "继续", Settings(), ID, generate={"user_input": "继续"})
    assert ID not in json.dumps(nested["messages"]) and HASH not in json.dumps(nested["messages"])


async def test_mvu_final_request_and_memory_auxiliary_state_exclude_bookkeeping():
    captured = []
    class Events:
        def event(self, *args):
            captured.append(args)
    provider = ScriptedLLM(['[{"op":"replace","path":"/hp","value":9}]'])
    result = await analyze_variables(Events(), "turn", Settings(), [], runtime_state(), "休息", "恢复精神",
                                     lambda: provider, document={"name": "艾琳"})
    assert result == [{"op": "replace", "path": "/hp", "value": 9}]
    wire = json.dumps(provider.last_messages, ensure_ascii=False)
    assert ID not in wire and HASH not in wire and "$internal" not in wire and "$meta" not in wire
    pool = evidence_pool([{"id": ID, "snapshot": {"state": runtime_state()}}], {}, 0, 1)
    body = "\n".join(e["text"] for e in pool)
    assert ID not in body and HASH not in body and "初始化正文" not in body
    assert '"hp": 8' in body


async def test_authorized_tools_only_return_short_refs_and_program_fills_sources(tmp_path):
    store = Store(tmp_path)
    doc = document()
    content = "银戒属于艾琳。" * 80 + "ENDMARK"
    doc["character_book"]["entries"][KEY]["content"] = content
    doc["character_book"]["entries"]["disabled"] = {
        "id": "disabled", "content": "银戒的禁用条目", "enabled": False}
    card = store.import_card(json.dumps(doc).encode(), "card.json")
    session = store.new_session(SessionCreate(card_id=card["id"]))
    session["snapshot"]["state"] = runtime_state()
    from tavern.conversation.scene import archive_history
    from tavern.memories.memory_engine import plan_batch
    agent = TrackerTask(store, "test", store.settings(), session, doc, session["messages"],
                        session["snapshot"], phase="pre", memory=True)
    _, request, _, _ = plan_batch(agent.history, archive_history(agent.history, doc, session["persona"]),
                                  0, agent.memory, doc, agent.settings, session["persona"])
    agent.batch, agent.references = request, request.references
    agent._new_recall()
    async def invoke(name, args):
        body = agent.encoded(agent.execute(name, args))
        assert all(value not in body for value in (ID, HASH, KEY, session["head"]))
        return body
    lore = json.loads(await invoke("search_lore", {"query": "银戒"}))
    assert "read_lore" not in agent.allowed
    assert set(lore[0]) == {"name", "content"}
    assert len(lore) == 1 and lore[0]["content"] == content
    assert agent.snapshot == session["snapshot"]
    recalled = json.loads(await invoke("tavern_recall_history", {"turn": 1, "radius": 0}))
    ref = recalled["rounds"][0]["passages"][0]["ref"]
    await invoke("write_memory", {"operations": [{"op": "add", "record": {"category": "episode", "content": "已经相遇", "sources": [ref]}}]})
    record = agent.memory["records"][-1]
    assert record["sources"][0]["message_id"] == session["head"] and record["sources"][0]["excerpt"] == "旧港相遇。"
    await invoke("search_memory", {"query": "相遇"})
    await invoke("read_state", {})
    await invoke("read_character", {"field": "extensions"})


async def test_editor_final_requests_hide_ids_hashes_and_support_reversible_parent_edit(tmp_path):
    from tests.backend.test_card_editor import ToolLLM
    doc = document()
    store = Store(tmp_path)
    card = store.import_card(json.dumps(doc).encode(), "card.json")
    service = CardEditorService(store)
    workspace = service.edits.create(card["id"])
    class Editor(ToolLLM):
        async def chat(self, messages, **kwargs):
            wire = json.dumps(messages, ensure_ascii=False)
            assert all(v not in wire for v in (ID, HASH, KEY, doc["extensions"]["custom"]["id"]))
            if self.call_count == 1:
                body = json.loads([m["content"] for m in messages if m.get("role") == "tool"][-1])
                projected = json.loads(body["text"])
                assert "checksum" not in body["text"] and "fingerprint" not in body["text"]
                projected["description"] = "本轮修改"
                self.steps[1] = ("patch_draft", {"operations": [{"op": "replace", "path": "", "value": projected}],
                                                "reason": "更新描述，保留其余内容"})
            async for item in super().chat(messages, **kwargs):
                yield item
    provider = Editor([("read_field", {"path": ""}), ("patch_draft", {}), ("validate_draft", {})])
    service.provider_factory = lambda: provider
    try:
        run = service.submit(workspace["id"], CardEditorRequest(text="更新描述", operation_id="one", expected_revision=0))
        await service.tasks[run["id"]]
        final = service.edits.run(run["id"])
        assert final["status"] == "completed", final["error"]
        expected = copy.deepcopy(doc)
        expected["description"] = "本轮修改"
        assert service.edits.get(workspace["id"])["document"] == expected
    finally:
        await service.close()


async def test_opening_tool_restores_handles_before_generating_its_own_hash(tmp_path):
    from tavern.character.card_workflow import greeting_hash
    from tests.backend.test_card_editor import ToolLLM
    doc = {"name": "艾琳", "first_mes": "遗物编号：" + ID}
    store = Store(tmp_path)
    card = store.import_card(json.dumps(doc).encode(), "card.json")
    service = CardEditorService(store)
    workspace = service.edits.create(card["id"])
    class Editor(ToolLLM):
        async def chat(self, messages, **kwargs):
            assert ID not in json.dumps(messages)
            if self.call_count == 1:
                text = json.loads([m["content"] for m in messages if m.get("role") == "tool"][-1])["text"]
                short_id = text.split("：", 1)[1]
                self.steps[1] = ("set_opening", {"index": 0, "narrative": text,
                                                "initialization": "<initvar>artifact: " + short_id + "</initvar>"})
            async for item in super().chat(messages, **kwargs):
                yield item
    provider = Editor([("read_field", {"path": "/first_mes"}), ("set_opening", {})])
    service.provider_factory = lambda: provider
    try:
        run = service.submit(workspace["id"], CardEditorRequest(text="保留编号并初始化", operation_id="one", expected_revision=0))
        await service.tasks[run["id"]]
        final = service.edits.run(run["id"])
        assert final["status"] == "completed", final["error"]
        result = service.edits.get(workspace["id"])["document"]
        opening = result["extensions"]["terrarium"]["workflow"]["greetings"][0]
        assert result["first_mes"] == doc["first_mes"]
        assert ID in opening["initialization"] and "@D" not in opening["initialization"]
        assert opening["greeting_hash"] == greeting_hash(doc["first_mes"])
    finally:
        await service.close()


def test_literal_handle_looking_text_is_not_mistaken_for_an_internal_identity():
    doc = {"name": "literal @D1", "description": ID}
    view = ModelView(doc)
    compact = view.expose(doc)
    assert compact["description"] != "@D1"
    assert view.restore(compact) == doc
