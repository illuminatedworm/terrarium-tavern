"""Setting-only edits, single-entry reads, script protection and bounded model data."""

import copy
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from tavern.application.api import create_app
from tavern.core.model_view import ModelView
from tavern.core.models import SessionCreate, TurnRequest
from tavern.full_agent.content import CHARACTER_FIELDS, card_data
from tavern.full_agent.state import new_state, rollover
from tavern.full_agent.tools import AgentTools, tool_definitions
from tests.backend import test_full_agent
from tests.backend.test_full_agent import Model, runtime, turn

story = test_full_agent.story


CARD_FORMATS = [
    pytest.param((False, False), id="plain-list"),
    pytest.param((False, True), id="plain-dict"),
    pytest.param((True, False), id="wrapped-list"),
    pytest.param((True, True), id="wrapped-dict"),
]


@pytest.fixture
def owner(request):
    # Only storage-format tests opt into the format matrix. Argument validation
    # and script detection have the same branches for every document container.
    wrapped, dictionary = getattr(request, "param", (False, False))
    entries = [
        {
            "id": "harbor",
            "comment": "港口",
            "content": "Harbor source",
            "keys": ["港口"],
            "extensions": {"unknown": {"keep": True}, "position": 4},
        },
        {
            "id": "ship",
            "name": "旧船",
            "content": "Ship source",
            "key": ["船"],
            "keysecondary": ["夜"],
            "disable": True,
            "insertion_order": 80,
        },
        {"id": "rule", "content": "Variable rule", "extensions": {"terrarium": {"audience": "tracker_mvu"}}},
        {"id": "init", "comment": "[InitVar] state", "content": "hp: 8"},
        {"id": "script", "content": "<script>eventOn('ready', () => {});</script>"},
    ]
    if dictionary:
        entries = {f"legacy/key~{i}": row for i, row in enumerate(entries)}
    data = {
        "name": "Guide",
        "description": "Before",
        "personality": "Shy",
        "scenario": "A port",
        "first_mes": "<button onclick='run()'>Start</button>",
        "alternate_greetings": ["Opening"],
        "extensions": {
            "tavern_helper": {"scripts": [{"id": "script-id", "content": "console.log('keep');"}]},
            "regex_scripts": [{"findRegex": "old", "replaceString": "new"}],
            "unknown": {"keep": True},
        },
        "character_book": {"name": "World", "scan_depth": 3, "entries": entries},
    }
    document = {"spec": "chara_card_v2", "spec_version": "2.0", "data": data} if wrapped else data
    req = TurnRequest(operation_id="content", expected_revision=0, allow_content_edit=True)
    result = SimpleNamespace(
        document=document,
        state=new_state(),
        snapshot={},
        session={},
        tid="content",
        model_view=ModelView(document),
        request=req,
        definitions=tool_definitions(req, {"policy_effective": "auto"}, document=document),
    )
    result.tools = AgentTools(result)
    return result


async def call(owner, name, **args):
    result = await owner.tools.execute(name, json.dumps(args, ensure_ascii=False))
    assert result["ok"] is True
    return result["data"]


async def revise(owner, *operations):
    return await call(owner, "revise_content", operations=list(operations), comment="User requested setting")


@pytest.mark.parametrize("owner", [CARD_FORMATS[0], CARD_FORMATS[2]], indirect=True)
@pytest.mark.parametrize("field", CHARACTER_FIELDS)
async def test_only_allowed_fields_can_be_read_and_written_in_both_card_formats(owner, field):
    original = copy.deepcopy(owner.document)
    data = card_data(owner.document)
    response = await call(owner, "read_character", ref=field)
    assert response["text"] == data.get(field)
    assert response["exists"] == (field in data)
    await revise(owner, {"op": "set_character", "field": field, "value": "Updated"})
    expected = card_data(original)
    expected[field] = "Updated"
    assert owner.document == original
    result = await revise(owner, {"op": "set_character", "field": field, "value": "Again"})
    assert result["characters"] == [{"ref": field, "text": "Again"}]
    assert card_data(owner.document)[field] == "Again"


@pytest.mark.parametrize("field", ["*", "character_book", "description.child"])
async def test_no_bulk_or_configuration_field_reads(owner, field):
    before = copy.deepcopy(owner.tools.reads.records)
    with pytest.raises(ValueError):
        await call(owner, "read_character", ref=field)
    assert owner.tools.reads.records == before


@pytest.mark.parametrize("owner", CARD_FORMATS, indirect=True)
async def test_search_is_navigation_only_and_disabled_entry_aliases_roundtrip(owner):
    results = await call(owner, "find_lore_for_edit", query="旧船")
    assert len(results["matches"]) == 1 and "仅用于定位" in results["notice"]
    ref = results["matches"][0]["ref"]
    with pytest.raises(ValueError, match="read_lore_entry"):
        await revise(owner, {"op": "update_lore", "ref": ref, "changes": {"content": "After"}})
    read = await call(owner, "read_lore_entry", ref=ref)
    assert read == {
        "ref": ref,
        "title": "旧船",
        "content": "Ship source",
        "keys": ["船"],
        "secondary_keys": ["夜"],
        "enabled": False,
    }
    await revise(
        owner,
        {
            "op": "update_lore",
            "ref": ref,
            "changes": {
                "title": "新船",
                "content": "After",
                "keys": ["新"],
                "secondary_keys": [],
                "enabled": True,
            },
        },
    )
    entries = card_data(owner.document)["character_book"]["entries"]
    rows = list(entries.values()) if isinstance(entries, dict) else entries
    ship = rows[1]
    assert ship["key"] == ["新"] and ship["keysecondary"] == [] and ship["disable"] is False
    assert ship["insertion_order"] == 80 and ship["name"] == "新船" and "comment" not in ship
    assert (await call(owner, "read_lore_entry", ref=ref))["content"] == "After"


async def test_target_reads_are_current_window_scoped_and_batch_failure_is_atomic(owner):
    await call(owner, "read_character", ref="description")
    await call(owner, "read_character", ref="personality")
    await call(owner, "read_lore_entry", ref="L1")
    before = copy.deepcopy(owner.document)
    reads = copy.deepcopy(owner.tools.reads.records)
    with pytest.raises(ValueError, match="read_lore_entry"):
        await revise(
            owner,
            {"op": "set_character", "field": "description", "value": "After"},
            {"op": "remove_lore", "ref": "L2"},
        )
    assert owner.document == before and owner.tools.reads.records == reads
    await revise(owner, {"op": "set_character", "field": "description", "value": "After"})
    await revise(owner, {"op": "set_character", "field": "personality", "value": "Bold"})
    rollover(owner.state, [], {"chapters": [], "current": [], "revision": 0}, 0)
    with pytest.raises(ValueError, match="read_lore_entry"):
        await revise(owner, {"op": "remove_lore", "ref": "L1"})
    await call(owner, "read_lore_entry", ref="L1")
    entries = card_data(owner.document)["character_book"]["entries"]
    target = next(
        e for e in (entries.values() if isinstance(entries, dict) else entries) if e["id"] == "harbor"
    )
    target["content"] = "Changed outside tool"
    with pytest.raises(ValueError, match="最新条目"):
        await revise(owner, {"op": "remove_lore", "ref": "L1"})


EXECUTABLE = [
    "<script src='remote.js'></script>",
    "<a href='java&#x73;cript:run()'>X</a>",
    "<UpdateVariable>change</UpdateVariable>",
    "{{setvar::hp::1}}",
    "```javascript\nrun();\n```",
]


@pytest.mark.parametrize("text", EXECUTABLE)
async def test_execution_content_cannot_be_introduced(owner, text):
    await call(owner, "read_character", ref="description")
    await call(owner, "read_lore_entry", ref="L1")
    before = copy.deepcopy(owner.document)
    for operation in [
        {"op": "set_character", "field": "description", "value": text},
        {"op": "add_lore", "entry": {"title": "New", "content": text}},
        {"op": "update_lore", "ref": "L1", "changes": {"content": text}},
    ]:
        with pytest.raises(ValueError):
            await revise(owner, operation)
        assert owner.document == before


@pytest.mark.parametrize("owner", [CARD_FORMATS[0], CARD_FORMATS[3]], indirect=True)
async def test_protected_entries_and_script_bearing_fields_cannot_be_read_changed_or_removed(owner):
    owner.state["lore_references"] = {"L20": "rule", "L21": "init", "L22": "script"}
    search = await call(owner, "find_lore_for_edit", query="*")
    assert {row["title"] for row in search["matches"]} == {"港口", "旧船"}
    for ref in ("L20", "L21", "L22"):
        for name, args in [
            ("read_lore_entry", {"ref": ref}),
            ("revise_content", {"operations": [{"op": "remove_lore", "ref": ref}], "comment": "Forbidden"}),
        ]:
            with pytest.raises(ValueError, match="变量规则、初始化或脚本"):
                await call(owner, name, **args)
    card_data(owner.document)["description"] = "Text <script>keep()</script>"
    with pytest.raises(ValueError, match="执行脚本"):
        await call(owner, "read_character", ref="description")
    with pytest.raises(ValueError, match="执行脚本"):
        await revise(owner, {"op": "set_character", "field": "description", "value": "Clean replacement"})


@pytest.mark.parametrize("owner", CARD_FORMATS, indirect=True)
async def test_append_requires_no_bulk_read_and_old_references_do_not_shift(owner):
    entries = card_data(owner.document)["character_book"]["entries"]
    for entry in entries.values() if isinstance(entries, dict) else entries:
        entry.pop("id")
    await call(owner, "read_lore_entry", ref="L1")
    second = await call(owner, "read_lore_entry", ref="L2")
    await revise(owner, {"op": "remove_lore", "ref": "L1"})
    # The existing read of L2 remains valid after deleting its preceding array item.
    await revise(owner, {"op": "update_lore", "ref": "L2", "changes": {"content": "Same ship, new text"}})
    assert (await call(owner, "read_lore_entry", ref="L2"))["title"] == second["title"]
    result = await revise(
        owner, {"op": "add_lore", "entry": {"title": "New place", "content": "Plain setting"}}
    )
    assert len(result["added_refs"]) == 1 and result["added_refs"][0] not in ("L1", "L2")
    with pytest.raises(ValueError, match="已删除"):
        await call(owner, "read_lore_entry", ref="L1")
    added = await call(owner, "read_lore_entry", ref=result["added_refs"][0])
    assert added["enabled"] and added["keys"] == [] and added["secondary_keys"] == []


@pytest.mark.parametrize("owner", CARD_FORMATS[:2], indirect=True)
async def test_effective_extension_aliases_and_injection_invalidation(owner):
    entries = card_data(owner.document)["character_book"]["entries"]
    raw = next(e for e in (entries.values() if isinstance(entries, dict) else entries) if e["id"] == "harbor")
    raw["extensions"].update(content="Effective text", keys=["Effective keyword"])
    owner.snapshot["lore_injections"] = [
        {"id": "harbor", "content": "Old"},
        {"id": "ship", "content": "Other"},
    ]
    owner.snapshot["lore"] = {"harbor": {"present": True}, "ship": {"present": True}}
    await call(owner, "read_lore_entry", ref="L1")
    await revise(
        owner, {"op": "update_lore", "ref": "L1", "changes": {"content": "New effective", "keys": ["New"]}}
    )
    raw = next(
        e
        for e in (
            card_data(owner.document)["character_book"]["entries"].values()
            if isinstance(entries, dict)
            else card_data(owner.document)["character_book"]["entries"]
        )
        if e["id"] == "harbor"
    )
    assert raw["content"] == "Harbor source" and raw["extensions"]["content"] == "New effective"
    assert raw["extensions"]["unknown"] == {"keep": True}
    assert owner.snapshot["lore_injections"] == [{"id": "ship", "content": "Other"}]
    assert owner.snapshot["lore"] == {"ship": {"present": True}}


@pytest.mark.parametrize(
    "operation",
    [
        {"op": "replace", "path": "", "value": {"name": "Bulk replacement"}},
        {"op": "add_lore", "entry": {"title": "X", "content": "Y", "id": "invented"}},
        {"op": "add_lore", "entry": {"title": "X", "content": "Y", "extensions": {"scripts": []}}},
        {"op": "remove_lore", "ref": "harbor"},
    ],
)
async def test_strict_interface_rejects_paths_ids_configuration_and_empty_changes(owner, operation):
    original = copy.deepcopy(owner.document)
    with pytest.raises(ValueError):
        await revise(owner, operation)
    assert owner.document == original


@pytest.mark.parametrize("owner", CARD_FORMATS[:2], indirect=True)
async def test_identity_ambiguity_and_failed_validation_do_not_change_draft(owner):
    await call(owner, "read_character", ref="name")
    before = copy.deepcopy(owner.document)
    with pytest.raises(ValueError, match="缺少 name"):
        await revise(owner, {"op": "set_character", "field": "name", "value": ""})
    assert owner.document == before
    await revise(owner, {"op": "set_character", "field": "name", "value": "Valid name"})
    entries = card_data(owner.document)["character_book"]["entries"]
    rows = list(entries.values()) if isinstance(entries, dict) else entries
    rows[1]["id"] = rows[0]["id"]
    with pytest.raises(ValueError, match="身份重复"):
        await call(owner, "find_lore_for_edit", query="*")


async def test_visible_character_and_additions_work_when_reads_are_disabled(story):
    store, session = story
    app, service = create_app(store.root), runtime(store)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
            url = f"/api/sessions/{session['id']}/full-agent/tools"
            result = await client.put(
                url, json={"expected_revision": 0, "disabled_tools": ["read_character", "read_lore_entry"]}
            )
            session = result.json()
            catalog = (await client.get(url, params={"allow_content_edit": "true"})).json()["tools"]
            edit = next(tool for tool in catalog if tool["name"] == "revise_content")
            assert edit["available"] and edit["edit_capabilities"] == {
                "character": True,
                "lore_update_remove": True,
                "lore_add": True,
            }
        model = Model(
            (
                "revise_content",
                {
                    "operations": [{"op": "set_character", "field": "description", "value": "Blocked"}],
                    "comment": "Check",
                },
            ),
            (
                "revise_content",
                {
                    "operations": [{"op": "add_lore", "entry": {"title": "New", "content": "New setting"}}],
                    "comment": "Check",
                },
            ),
            "Narrative",
        )
        result, _ = await turn(service, session, model, allow_content_edit=True)
        assert result["status"] == "committed", result["error"]
        results = [
            e["payload"]["result"] for e in store.events(result["id"]) if e["kind"] == "full_agent_tool"
        ]
        assert results[0]["data"]["saved_to_draft"] and results[1]["data"]["saved_to_draft"]
    finally:
        await service.close()
        await app.state.service.close()


async def test_script_lore_deltas_preserve_agent_edits_across_variable_patch_and_resume(tmp_path):
    from tavern.character.cards import lore_entries
    from tavern.storage.store import Store
    from tests.backend.test_full_agent import Script

    original = {
        "one": {"id": "one", "content": "Original A", "extensions": {"private": {"keep": True}}},
        "two": {"id": "two", "content": "Original B"},
        "three": {"id": "three", "content": "Original C"},
    }
    store = Store(tmp_path)
    card = store.import_card(
        json.dumps(
            {"name": "Guide", "first_mes": "Opening", "character_book": {"entries": original}}
        ).encode(),
        "fixture.json",
    )
    settings = store.settings()
    settings.full_agent_config.auto_retrieval = False
    settings.retrieval.channels = ["keyword"]
    store.save_settings(settings)
    session = store.new_session(SessionCreate(card_id=card["id"], agent_mode="full_agent"))
    calls = {"patch": 0, "final": 0}

    class CallbackScript(Script):
        def __init__(self, document, history, snapshot):
            super().__init__(history, snapshot)
            self.mirror["lore"] = lore_entries(document)
            self.mirror["lore"][2]["comment"] = "Startup title"
            self.mirror["loreModified"] = True

        async def call(self, method, *args):
            if method == "process":
                assert self.mirror["lore"][0]["content"] == "Agent A"
                if args[1]:
                    calls["patch"] += 1
                    self.mirror["state"]["stat_data"] = {"hp": 7}
                    self.mirror["lore"][1]["content"] = "Patch callback B"
                else:
                    calls["final"] += 1
                    self.mirror["lore"][2]["content"] = "Final callback C"
            return await super().call(method, *args)

    class CallbackHost:
        async def open(self, document, history, snapshot, *args, **kwargs):
            return CallbackScript(document, history, snapshot)

        async def close(self):
            pass

    service = runtime(store)
    service.full.runtime = CallbackHost()
    model = Model(
        ("read_lore_entry", {"ref": "L1"}),
        (
            "revise_content",
            {
                "operations": [{"op": "update_lore", "ref": "L1", "changes": {"content": "Agent A"}}],
                "comment": "User requested setting",
            },
        ),
        ("read_state", {}),
        ("patch_state", {"operations": [{"op": "add", "path": "/hp", "value": 7}], "comment": "HP"}),
        ("read_lore_entry", {"ref": "L2"}),
        RuntimeError("Disconnected after accepted tools"),
    )
    try:
        failed, unchanged = await turn(
            service, session, model, update_variables=True, allow_content_edit=True
        )
        assert failed["status"] == "error"
        assert unchanged["head"] == session["head"]
        progress = store.full_agent_progress(failed["id"])
        entries = card_data(progress["document"])["character_book"]["entries"]
        assert entries["one"]["content"] == "Agent A"
        assert entries["two"]["content"] == "Patch callback B"
        assert entries["three"]["comment"] == "Startup title"
        assert progress["patch_state_done"]
        assert progress["script_mirror"]["lore"][0]["content"] == "Agent A"
        read = next(m for m in reversed(model.requests[-1]["messages"]) if m["role"] == "tool")
        assert json.loads(read["content"])["data"]["content"] == "Patch callback B"
    finally:
        await service.close()

    store = Store(tmp_path)
    service = runtime(store)
    service.full.runtime = CallbackHost()
    service.full.provider_factory = lambda: Model("Narrative after reconnect")
    try:
        resumed = service.submit(
            session["id"],
            TurnRequest(
                text=failed["request"]["text"],
                operation_id="resume-lore",
                expected_revision=session["revision"],
                resume_turn_id=failed["id"],
                update_variables=True,
                allow_content_edit=True,
            ),
        )
        await service.tasks[resumed["id"]]
        result = store.turn(resumed["id"])
        assert result["status"] == "committed", result["error"]
        current = store.session(session["id"])
        entries = card_data(current["snapshot"]["content"]["document"])["character_book"]["entries"]
        assert entries == {
            "one": {**original["one"], "content": "Agent A"},
            "two": {**original["two"], "content": "Patch callback B"},
            "three": {**original["three"], "content": "Final callback C", "comment": "Startup title"},
        }
        assert current["snapshot"]["state"]["stat_data"] == {"hp": 7}
        assert calls == {"patch": 1, "final": 1}
    finally:
        await service.close()


async def test_five_hundred_entries_only_selected_source_reaches_model_and_other_data_is_preserved(story):
    store, _ = story
    entries = [
        {
            "id": uuid4().hex,
            "comment": f"Entry {i}",
            "content": f"UNRELATED_ENTRY_{i} " + "Setting " * 100,
            "extensions": {"position": 4, "source_hash": "e" * 64},
        }
        for i in range(500)
    ]
    entries[250].update(comment="PICKED_TARGET", content="Selected full source " * 3000 + "TARGET_END")
    original = {
        "spec": "chara_card_v2",
        "spec_version": "2.0",
        "data": {
            "name": "Large world",
            "first_mes": "Opening",
            "description": "Safe narrative",
            "extensions": {
                "tavern_helper": {"scripts": [{"content": "DO_NOT_LOAD_SCRIPT"}]},
                "unknown": {"keep": True},
            },
            "character_book": {"name": "World", "entries": entries},
        },
    }
    card = store.import_card(json.dumps(original).encode(), "large.json")
    session = store.new_session(SessionCreate(card_id=card["id"], agent_mode="full_agent"))
    settings = store.settings()
    settings.context_tokens = 128000
    store.save_settings(settings)
    service = runtime(store)
    model = Model(
        ("find_lore_for_edit", {"query": "PICKED_TARGET"}),
        ("read_lore_entry", {"ref": "L251"}),
        (
            "revise_content",
            {
                "operations": [
                    {"op": "update_lore", "ref": "L251", "changes": {"content": "Updated selected setting"}}
                ],
                "comment": "User request",
            },
        ),
        "Narrative",
    )
    try:
        result, current = await turn(service, session, model, allow_content_edit=True)
        assert result["status"] == "committed", result["error"]
        wire = json.dumps(
            [message.get("content", "") for req in model.requests for message in req["messages"]],
            ensure_ascii=False,
        )
        assert "TARGET_END" in wire and "DO_NOT_LOAD_SCRIPT" not in wire
        assert all(f"UNRELATED_ENTRY_{i}" not in wire for i in range(500) if i != 250)
        assert all(entry["id"] not in wire for entry in entries) and "e" * 64 not in wire
        actual = current["snapshot"]["content"]["document"]
        expected = copy.deepcopy(original)
        expected["data"]["character_book"]["entries"][250]["content"] = "Updated selected setting"
        assert actual == expected
    finally:
        await service.close()
