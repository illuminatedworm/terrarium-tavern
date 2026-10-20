"""Final request/tool views, not merely prompt wording. All providers are local."""

import copy
import json
from types import SimpleNamespace

import httpx
import pytest
from kohakuterrarium.testing.llm import ScriptedLLM

from tavern.character.card_author import CardAuthorService
from tavern.conversation.context import compile_context, macros
from tavern.core.model_view import ModelView, state_for_model
from tavern.core.models import CardAuthorRequest, ContextRecipe, SessionCreate, Settings
from tavern.core.tokens import count_text
from tavern.memories.memory_evidence import evidence_pool
from tavern.memories.memory_records import Record
from tavern.memories.recall_filter import filter_messages, filter_recall
from tavern.scripting.variables import analyze_variables
from tavern.storage.store import Store
from tavern.tracker.task import TrackerTask

ID = "12345678-90ab-cdef-1234-567890abcdef"
KEY = "long-storage-key-with-opaque-identity-019876543210"
HASH = "f" * 64


def document():
    return {
        "name": "艾琳",
        "first_mes": "旧港相遇。",
        "description": "人物设定",
        "character_book": {
            "entries": {
                KEY: {
                    "id": ID,
                    "content": "银戒属于艾琳",
                    "comment": "银戒",
                    "enabled": True,
                    "fingerprint": HASH,
                },
            }
        },
        "extensions": {
            "custom": {
                "id": "custom-random-identity-012345678901234567890",
                "checksum": HASH,
                "code": f'const original = "{ID}";',
                "unchanged": "保留",
            }
        },
    }


def runtime_state():
    return {
        "stat_data": {"hp": 8, "$internal": {"cache_key": ID}, "$meta": {"schema_version": HASH}},
        "initialized_lorebooks": {"card:" + ID: ["初始化正文" * 100]},
        "schema": {"id": HASH},
        "display_data": {"hp": "5->8"},
        "delta_data": {"hp": "+3"},
    }


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
    result = view.patch(
        doc,
        [
            {"op": "replace", "path": "/character_book/entries/" + key + "/content", "value": "仍由艾琳拥有"},
            {
                "op": "add",
                "path": "/character_book/entries/new_rules",
                "value": {"content": "变量规则", "extensions": {"terrarium": {"split_from": source}}},
            },
        ],
    )
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
    doc = {
        "name": "a",
        "character_book": {
            "entries": [
                {"id": ID, "content": "甲", "checksum": "a" * 64},
                {"id": "opaque-long-second-identity-01234567890", "content": "乙", "checksum": "b" * 64},
            ]
        },
    }
    view = ModelView(doc)
    rows = view.expose(doc)["character_book"]["entries"][::-1]
    changed = view.patch(doc, [{"op": "replace", "path": "/character_book/entries", "value": rows}])
    assert changed["character_book"]["entries"] == doc["character_book"]["entries"][::-1]


def test_json_pointer_escaping_and_array_insertion_do_not_corrupt_identity():
    key = KEY + "/literal~key"
    doc = {"name": "a", "character_book": {"entries": {key: {"id": ID, "content": "保留", "checksum": HASH}}}}
    view = ModelView(doc)
    pointer = view.pointer(
        "/character_book/entries/" + key.replace("~", "~0").replace("/", "~1") + "/content"
    )
    assert key not in pointer
    result = view.patch(doc, [{"op": "replace", "path": pointer, "value": "变更"}])
    assert result["character_book"]["entries"][key] == {"id": ID, "content": "变更", "checksum": HASH}
    doc["character_book"]["entries"] = [{"id": ID, "content": "保留", "checksum": HASH}]
    result = view.patch(
        doc, [{"op": "add", "path": "/character_book/entries/0", "value": {"content": "新增"}}]
    )
    rows = result["character_book"]["entries"]
    assert rows[0]["id"] != ID and "checksum" not in rows[0]
    assert rows[1] == doc["character_book"]["entries"][0]


def test_writer_and_nested_context_exclude_ledgers_and_internal_state():
    state = runtime_state()
    snapshot = {
        "state": state,
        "memory_document": {
            "version": 2,
            "records": [
                Record(
                    id=ID,
                    category="episode",
                    content="有约定",
                    sources=[{"message_id": ID, "excerpt": "约定", "fingerprint": HASH}],
                ).model_dump()
            ],
        },
        "lore": {},
    }
    doc = {
        "name": "艾琳",
        "description": "角色描述",
        "character_book": {
            "entries": [{"id": ID, "comment": "资料", "content": "普通设定", "constant": True}]
        },
    }
    session = {"id": ID, "persona": "用户", "recipe": ContextRecipe().model_dump()}
    compiled = compile_context(
        doc, session, [{"id": ID, "role": "assistant", "raw": "相遇。"}], snapshot, "继续", Settings(), ID
    )
    wire = json.dumps(compiled["messages"], ensure_ascii=False)
    assert ID not in wire and HASH not in wire and "初始化正文" not in wire
    assert "普通设定" in wire
    assert any('"hp": 8' in m["content"] for m in compiled["messages"])
    assert state == runtime_state()
    assert state_for_model(state) == {"stat_data": {"hp": 8}}
    assert ID not in macros("{{getvar::initialized_lorebooks}}", doc, "用户", state)
    # Script-triggered generation uses this exact compiler and messages view.
    nested = compile_context(
        doc, session, [], snapshot, "继续", Settings(), ID, generate={"user_input": "继续"}, story_turn=False
    )
    assert ID not in json.dumps(nested["messages"]) and HASH not in json.dumps(nested["messages"])
    # A story input keeps its future position; a nested input never becomes history.
    inputs = [
        next(m["content"] for m in context["messages"] if m["role"] == "user" and "继续" in m["content"])
        for context in (compiled, nested)
    ]
    assert inputs == ["[H2]\n继续", "继续"]


async def test_mvu_final_request_and_memory_auxiliary_state_exclude_bookkeeping():
    captured = []

    class Events:
        def event(self, *args):
            captured.append(args)

    provider = ScriptedLLM(['[{"op":"replace","path":"/hp","value":9}]'])
    result = await analyze_variables(
        Events(),
        "turn",
        Settings(),
        [],
        runtime_state(),
        "休息",
        "恢复精神",
        lambda: provider,
        document={"name": "艾琳"},
    )
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
        "id": "disabled",
        "content": "银戒的禁用条目",
        "enabled": False,
    }
    card = store.import_card(json.dumps(doc).encode(), "card.json")
    session = store.new_session(SessionCreate(card_id=card["id"]))
    session["snapshot"]["state"] = runtime_state()
    from tavern.conversation.scene import archive_history
    from tavern.memories.memory_engine import plan_batch

    agent = TrackerTask(
        store,
        "test",
        store.settings(),
        session,
        doc,
        session["messages"],
        session["snapshot"],
        phase="pre",
        memory=True,
    )
    _, request, _, _ = plan_batch(
        agent.history,
        archive_history(agent.history, doc, session["persona"]),
        0,
        agent.memory,
        doc,
        agent.settings,
        session["persona"],
    )
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
    recalled = json.loads(await invoke("tavern_recall_history", {"ref": "H1", "radius": 0}))
    ref = recalled["rounds"][0]["passages"][0]["ref"]
    await invoke(
        "write_memory",
        {
            "operations": [
                {"op": "add", "record": {"category": "episode", "content": "已经相遇", "sources": [ref]}}
            ]
        },
    )
    record = agent.memory["records"][-1]
    assert (
        record["sources"][0]["message_id"] == session["head"]
        and record["sources"][0]["excerpt"] == "旧港相遇。"
    )
    await invoke("search_memory", {"query": "相遇"})
    await invoke("read_state", {})
    await invoke("read_character", {"field": "extensions"})


async def test_editor_final_requests_hide_ids_hashes_and_support_reversible_parent_edit(tmp_path):
    from tests.backend.test_card_author import ToolLLM

    doc = document()
    store = Store(tmp_path)
    card = store.import_card(json.dumps(doc).encode(), "card.json")
    service = CardAuthorService(store)
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
                self.steps[1] = (
                    "patch_draft",
                    {
                        "operations": [{"op": "replace", "path": "", "value": projected}],
                        "comment": "更新描述，保留其余内容",
                    },
                )
            async for item in super().chat(messages, **kwargs):
                yield item

    provider = Editor([("read_field", {"path": ""}), ("patch_draft", {}), ("validate_draft", {})])
    service.provider_factory = lambda: provider
    try:
        run = service.submit(
            workspace["id"], CardAuthorRequest(text="更新描述", operation_id="one", expected_revision=0)
        )
        await service.tasks[run["id"]]
        final = service.edits.run(run["id"])
        assert final["status"] == "completed", final["error"]
        expected = copy.deepcopy(doc)
        expected["description"] = "本轮修改"
        assert service.edits.get(workspace["id"])["document"] == expected
    finally:
        await service.close()


async def test_opening_tool_restores_handles_in_complete_inline_text(tmp_path):
    from tests.backend.test_card_author import ToolLLM

    doc = {"name": "艾琳", "first_mes": "遗物编号：" + ID}
    store = Store(tmp_path)
    card = store.import_card(json.dumps(doc).encode(), "card.json")
    service = CardAuthorService(store)
    workspace = service.edits.create(card["id"])

    class Editor(ToolLLM):
        async def chat(self, messages, **kwargs):
            assert ID not in json.dumps(messages)
            if self.call_count == 1:
                text = json.loads([m["content"] for m in messages if m.get("role") == "tool"][-1])["text"]
                short_id = text.split("：", 1)[1]
                self.steps[1] = (
                    "set_opening",
                    {"index": 0, "narrative": text + "\n<initvar>artifact: " + short_id + "</initvar>"},
                )
            async for item in super().chat(messages, **kwargs):
                yield item

    provider = Editor([("read_field", {"path": "/first_mes"}), ("set_opening", {})])
    service.provider_factory = lambda: provider
    try:
        run = service.submit(
            workspace["id"],
            CardAuthorRequest(text="保留编号并初始化", operation_id="one", expected_revision=0),
        )
        await service.tasks[run["id"]]
        final = service.edits.run(run["id"])
        assert final["status"] == "completed", final["error"]
        result = service.edits.get(workspace["id"])["document"]
        assert result["first_mes"] == doc["first_mes"] + "\n<initvar>artifact: " + ID + "</initvar>"
        assert "@D" not in result["first_mes"]
        assert "extensions" not in result
    finally:
        await service.close()


def test_literal_handle_looking_text_is_not_mistaken_for_an_internal_identity():
    doc = {"name": "literal @D1", "description": ID}
    view = ModelView(doc)
    compact = view.expose(doc)
    assert compact["description"] != "@D1"
    assert view.restore(compact) == doc


async def test_recall_filter_sees_short_refs_and_returns_record_ids(monkeypatch):
    from tavern.application.service import RoleplayRuntime
    from tavern.core.usage import aggregate_cache_usage
    from tavern.memories.memory_archive import ARCHIVE

    entries = [
        Record(
            id=ID,
            category="episode",
            content="守塔人交出钥匙",
            sources=[{"message_id": KEY, "excerpt": "钥匙", "fingerprint": HASH}],
        ).model_dump(),
        Record(id=KEY, category="episode", content="集市买伞").model_dump(),
    ]
    history = [
        {"id": ID, "role": "user", "raw": "走", "model": "走"},
        {"id": KEY, "role": "assistant", "raw": "好", "model": "好"},
    ]
    messages, _ = filter_messages(entries, history, "去钟塔")
    wire = json.dumps(messages, ensure_ascii=False)
    assert all(value not in wire for value in (ID, KEY, HASH))
    candidates = json.loads(messages[1]["content"])["memory_recall"]["candidates"]
    assert [c["ref"] for c in candidates] == ["A1", "A2"]

    class Provider:
        def __init__(self, refs):
            self.error = None
            self.closed = 0
            self.usage = {"prompt_tokens": 100, "completion_tokens": 20, "cached_tokens": 60}
            self.last_tool_calls = [
                type("Call", (), {"name": "select_recall", "arguments": json.dumps({"refs": refs})})()
            ]

        async def chat_complete(self, *args, **kwargs):
            assert all(value not in json.dumps(args[0]) for value in (ID, KEY, HASH))
            if self.error:
                raise self.error
            return SimpleNamespace(usage=self.usage)

        async def close(self):
            self.closed += 1

    settings = Settings()
    assert await filter_recall(Provider(["A1"]), entries, history, "去钟塔", settings) == [ID]
    with pytest.raises(ValueError, match="未提供的候选"):
        await filter_recall(Provider(["A3"]), entries, history, "去钟塔", settings)

    events = []
    provider = Provider(["A1"])
    runtime = SimpleNamespace(
        store=SimpleNamespace(event=lambda tid, kind, payload: events.append((kind, payload))),
        tracker_provider_factory=lambda: provider,
    )
    settings.memory_recall_filter = True
    prepared = {"memory_recall": {"ids": [ID], "diagnostics": []}}
    args = (
        runtime,
        "turn",
        settings,
        history,
        {ARCHIVE: entries},
        "去钟塔",
        prepared,
        SimpleNamespace(new=lambda purpose: None),
    )
    session = {"id": "story", "persona": "用户", "recipe": ContextRecipe().model_dump()}

    async def run_filter(**kwargs):
        return await RoleplayRuntime.filter_memory_recall(*args, session=session, **kwargs)

    result = await run_filter()
    assert result["memory_recall"]["ids"] == [ID]
    assert events[0][0] == "usage"
    assert events[0][1]["usage"] == provider.usage
    assert events[0][1]["purpose"] == "memory_recall" and events[0][1]["calls"] == 1
    provider.error = httpx.ReadTimeout("offline")
    assert await run_filter() is prepared
    assert events[-1][0] == "memory_recall_filter" and events[-1][1]["kept_original"]
    provider.error = None
    provider.last_tool_calls[0].arguments = '{"refs":'
    assert await run_filter() is prepared
    assert "有效 JSON" in events[-1][1]["message"]
    # A returned but malformed selection still consumed tokens; a failed transport has no response.
    usages = [payload["usage"] for kind, payload in events if kind == "usage"]
    assert usages == [provider.usage, provider.usage]
    assert aggregate_cache_usage(usages) == {"prompt_tokens": 200, "cached_tokens": 120}
    event_count = len(events)
    provider.error = KeyError("implementation_bug")
    with pytest.raises(KeyError, match="implementation_bug"):
        await run_filter()
    assert provider.closed == 4 and len(events) == event_count

    async def unavailable(*args, **kwargs):
        raise ValueError("后台账号刷新失败")

    runtime.tracker_provider_factory = None
    monkeypatch.setattr("tavern.core.provider.make_provider", unavailable)
    assert await run_filter() is prepared
    assert events[-1][1]["kept_original"] and "账号刷新失败" in events[-1][1]["message"]

    async def forbidden(*args, **kwargs):
        pytest.fail("Disabled memory must not create a filter provider")

    monkeypatch.setattr("tavern.core.provider.make_provider", forbidden)
    block = next(b for b in session["recipe"]["blocks"] if b["source"] == "memory")
    block["enabled"] = False
    assert await run_filter() is prepared
    block["enabled"] = True
    block["condition"] = "missing"
    assert await run_filter() is prepared
    block["condition"] = ""
    assert await run_filter(generate={"ordered_prompts": ["user_input"]}) is prepared
