"""Modification evidence follows complete current text in actual model context."""
import copy
import json

import pytest

from tavern.character.cards import normalize
from tavern.conversation.compaction import hashes
from tavern.conversation.context import macros
from tavern.core.message_adapter import AdapterRule
from tavern.full_agent.context import compile_window
from tavern.full_agent.state import rollover
from tavern.full_agent.tools import StateRead
from tavern.full_agent.visibility import context_messages
from tavern.scripting.request_context import response_origin
from tests.backend import test_full_agent_read_guards as guards
from tests.backend.test_full_agent import Model, runtime, turn

owner = guards.owner
story = guards.story
call = guards.call


@pytest.mark.parametrize(("scope", "expected"), [
    ("mvu", {"hp": 8}),
    ("full", {"stat_data": {"hp": 8}, "weather": "rain", "other": {"day": 3}}),
    ("other", {"weather": "rain", "other": {"day": 3}}),
])
async def test_state_scopes_partition_visible_state_without_exposing_metadata(owner, scope, expected):
    owner.snapshot["state"] = {"stat_data": {"hp": 8, "$meta": "private"}, "weather": "rain",
        "other": {"day": 3, "$internal": "private"}, "schema": {"private": True},
        "initialized_lorebooks": ["private"], "display_data": {"private": True}, "delta_data": {"private": True}}
    original = copy.deepcopy(owner.snapshot)
    result = await call(owner, "read_state", scope=scope)
    assert result["scope"] == scope and json.loads(result["text"]) == expected
    assert owner.snapshot == original


async def test_other_scope_paths_exclude_mvu_and_do_not_grant_patch_evidence(owner):
    owner.snapshot["state"]["weather"] = "rain"
    assert json.loads((await call(owner, "read_state", scope="other", path="/weather"))["text"]) == "rain"
    assert json.loads((await call(owner, "read_state", scope="other", path="/stat_data"))["text"]) is None
    assert json.loads((await call(owner, "read_state", scope="other"))["text"]) == {"weather": "rain"}
    with pytest.raises(ValueError, match='scope="mvu"'):
        await call(owner, "patch_state", operations=[{"op": "replace", "path": "/hp", "value": 7}], comment="Injury")
    result = await call(owner, "read_state", scope="full", path="/stat_data")
    assert json.loads(result["text"]) == {"hp": 8}
    owner.tools.reads.require_state()


def test_read_state_schema_only_advertises_new_scopes_with_mvu_default():
    schema = StateRead.model_json_schema()["properties"]["scope"]
    assert schema["enum"] == ["mvu", "full", "other"] and schema["default"] == "mvu"
    assert StateRead().scope == "mvu"


@pytest.mark.parametrize(("scope", "current"), [("variables", "mvu"), ("story", "full")])
async def test_legacy_scopes_execute_and_retained_history_still_grants_evidence(owner, scope, current):
    result = await call(owner, "read_state", scope=scope)
    assert result["scope"] == current
    # Already-persisted results retain their old metadata; replay must understand
    # the old call arguments without altering either stored calls or results.
    records = exchange("read_state", {"scope": scope}, {**result, "scope": scope})
    original = copy.deepcopy(records)
    context(owner, records)
    owner.tools.reads.require_state()
    assert records == original


def test_other_scope_replay_never_counts_as_complete_mvu_even_when_values_match(owner):
    owner.snapshot["state"] = {"stat_data": {"hp": 8}, "hp": 8}
    records = exchange("read_state", {"scope": "other"}, {"text": '{"hp":8}'})
    context(owner, records)
    with pytest.raises(ValueError, match="read_state"):
        owner.tools.reads.require_state()


def exchange(name, args, data=None, *, ok=True):
    messages = [{"role": "assistant", "content": "", "tool_calls": [{"id": name, "type": "function",
        "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}]}]
    if data is not None:
        messages.append({"role": "tool", "tool_call_id": name,
            "content": json.dumps({"ok": ok, "data": data}, ensure_ascii=False)})
    return messages


def context(owner, records=(), *, compact=False):
    owner.session.update(id="context", revision=0, persona="User", messages=[])
    owner.snapshot["full_agent"] = owner.state
    owner.expand = lambda text: macros(text, normalize(owner.document), "User", owner.snapshot["state"], {})
    owner.settings.context_strategy = "openai_compact" if compact else "new_context"
    if compact:
        owner.settings.api_mode = "responses"
    result = compile_window(owner.document, owner.session, [], owner.snapshot, "Continue",
        owner.settings, owner.definitions, continuation=records, input_id="input")
    owner.tools.reads.observe_context(context_messages(result))
    return result


def note(owner):
    owner.state["notes"]["clue"] = {"key": "clue", "content": "Complete current note", "kind": "hypothesis",
        "status": "active", "revision": 2, "sources": []}
    return owner.state["notes"]["clue"]


def lore(owner, *, constant=False):
    raw = {"id": "entry", "comment": "Harbor", "content": "Complete {{char}} lore",
           "keys": ["port"], "constant": constant}
    owner.document["character_book"] = {"entries": [raw]}
    return raw


async def test_note_tool_call_text_counts_without_a_read_or_repeated_body_in_result(owner):
    current = note(owner)
    records = exchange("write_note", {"key": "clue", "content": current["content"], "kind": "hypothesis"},
                       {"key": "clue", "saved_to_draft": True})
    context(owner, records)
    await call(owner, "write_note", key="clue", content="Next complete note", kind="hypothesis")
    # The just-authored replacement also supports another write in this context.
    await call(owner, "archive_note", key="clue")
    await call(owner, "write_note", key="clue", content="Reactivated note", kind="hypothesis")
    assert current is not owner.state["notes"]["clue"]
    assert owner.state["notes"]["clue"]["status"] == "active"


@pytest.mark.parametrize("notify", [False, True])
@pytest.mark.parametrize("edited_role", ["user", "assistant"])
async def test_story_edit_only_retires_note_write_evidence_when_notifying(story, notify, edited_role):
    store, session = story
    service = runtime(store)
    try:
        result, session = await turn(service, session, Model(
            ("write_note", {"key": "clue", "content": "Complete current note", "kind": "fact", "sources": ["H2"]}),
            "Original reply"))
        assert result["status"] == "committed", result["error"]
        before = copy.deepcopy(session["snapshot"]["full_agent"])
        target = session["messages"][-2 if edited_role == "user" else -1]
        edited = store.edit_message(session["id"], target["id"], target["raw"] + "。",
                                    session["revision"], notify_ai=notify)
        current = store.session(edited["id"])
        state = current["snapshot"]["full_agent"]
        assert (state["window"]["id"] != before["window"]["id"]) is notify
        assert state["notes"] == before["notes"]
        assert state["window"]["number"] == before["window"]["number"]
        # Exercise actual compilation and tool execution, not just window flags.
        write = ("write_note", {"key": "clue", "content": "Updated note", "kind": "hypothesis"})
        responses = [write]
        if notify:
            responses.extend([("read_note", {"key": "clue"}), write])
        model = Model(*responses, "Next reply")
        result, current = await turn(service, current, model)
        assert result["status"] == "committed", result["error"]
        previous_calls = [call for message in model.requests[0]["messages"]
                          for call in message.get("tool_calls", [])]
        assert any(call["function"]["name"] == "write_note" for call in previous_calls) is not notify
        first_result = next(json.loads(message["content"]) for message in reversed(model.requests[1]["messages"])
                            if message.get("role") == "tool" and message["tool_call_id"] == "call1")
        assert first_result["ok"] is not notify
        if notify:
            assert first_result["error"]["code"] == "READ_REQUIRED"
        assert current["snapshot"]["full_agent"]["notes"]["clue"]["content"] == "Updated note"
    finally:
        await service.close()


@pytest.mark.parametrize("source", ["directory", "preview", "old_call", "opaque"])
async def test_note_requires_current_full_text_not_directory_preview_or_old_version(owner, source):
    current = note(owner)
    records = {
        "directory": [{"role": "system", "source": "note_list", "content": "clue"}],
        "preview": exchange("search_notes", {"query": "clue"}, {"matches": [{"key": "clue", "preview": current["content"]}]}),
        "old_call": exchange("write_note", {"key": "clue", "content": "Previous version", "kind": "hypothesis"}),
        "opaque": [{"role": "assistant", "opaque": True, "content": current["content"]}],
    }[source]
    context(owner, records)
    before = copy.deepcopy(current)
    with pytest.raises(ValueError, match="read_note"):
        await call(owner, "write_note", key="clue", content="Replacement", kind="hypothesis")
    with pytest.raises(ValueError, match="read_note"):
        await call(owner, "archive_note", key="clue")
    assert owner.state["notes"]["clue"] == before


async def test_complete_read_without_metadata_and_external_change(owner):
    current = note(owner)
    records = exchange("read_note", {"key": "clue"}, {"text": current["content"]})
    context(owner, records)
    owner.tools.reads.require_note("clue")
    current["content"] = "External edit"
    context(owner, records)
    with pytest.raises(ValueError, match="read_note"):
        owner.tools.reads.require_note("clue")
    await call(owner, "read_note", key="clue")
    await call(owner, "write_note", key="clue", content="Updated after read", kind="hypothesis")


async def test_fixed_fields_and_macro_expanded_post_count_after_adapter_and_rollover(owner):
    owner.document["description"] = "Complete {{char}} description"
    owner.document["post_history_instructions"] = "Complete {{char}} post"
    owner.settings.message_adapter.mappings[owner.settings.api_mode] = {"post": AdapterRule(role="user", wrap=False)}
    result = context(owner)
    assert any(m["content"] == "Complete Guide post" for m in result["messages"])
    await call(owner, "revise_content", operations=[{"op": "set_character", "field": "description", "value": "New description"}], comment="Edit")
    rollover(owner.state, [], "Continue", 0)
    context(owner)
    await call(owner, "revise_content", operations=[{"op": "set_character", "field": "post_history_instructions", "value": "New post"}], comment="Edit")


async def test_character_and_lore_own_tool_call_arguments_are_current_version_evidence(owner):
    raw = lore(owner)
    # name has no dedicated fixed field block; no returned tool body is needed.
    records = exchange("revise_content", {"operations": [
        {"op": "set_character", "field": "name", "value": owner.document["name"]},
        {"op": "update_lore", "ref": "L1", "changes": {"content": raw["content"]}},
    ], "comment": "Earlier complete texts"})
    context(owner, records)
    await call(owner, "revise_content", operations=[
        {"op": "set_character", "field": "name", "value": "New name"},
        {"op": "update_lore", "ref": "L1", "changes": {"content": "New lore"}},
    ], comment="Edit retained versions")


@pytest.mark.parametrize("source", ["constant", "anchored", "recall"])
async def test_complete_lore_injection_or_recall_allows_edit_without_read_tool(owner, source):
    raw = lore(owner, constant=source == "constant")
    records = []
    if source == "anchored":
        owner.snapshot["lore_injections"] = [{"id": raw["id"], "anchor_id": "input", "title": "Harbor", "content": "Complete Guide lore"}]
    elif source == "recall":
        records = exchange("recall_lore", {"query": "port"}, {"matches": [{"text": "<lorebook>\nComplete Guide lore\n</lorebook>"}]})
    context(owner, records)
    owner.definitions = [t for t in owner.definitions if t["function"]["name"] != "read_lore_entry"]
    await call(owner, "revise_content", operations=[{"op": "update_lore", "ref": "L1", "changes": {"content": "Changed lore"}}], comment="Edit")
    assert owner.document["character_book"]["entries"][0]["content"] == "Changed lore"


def test_stale_frozen_lore_does_not_authorize_current_version(owner):
    raw = lore(owner)
    owner.snapshot["lore_injections"] = [{"id": raw["id"], "anchor_id": "input", "title": "Harbor", "content": "Old frozen lore"}]
    context(owner)
    with pytest.raises(ValueError, match="read_lore_entry"):
        owner.tools.reads.require_entry("L1", raw)


def test_current_variables_from_own_call_or_visible_read_and_patch(owner):
    own_call = exchange("patch_state", {"operations": [{"op": "replace", "path": "", "value": {"hp": 8}}]})
    context(owner, own_call)
    owner.tools.reads.require_state()
    records = exchange("read_state", {}, {"text": '{"hp":9}'}) + exchange("patch_state",
        {"operations": [{"op": "replace", "path": "/hp", "value": 8}]}, {"saved_to_draft": True})
    context(owner, records)
    owner.tools.reads.require_state()
    # Callback changes that cannot be reconstructed from those visible patches
    # must not inherit the old permission.
    owner.snapshot["state"]["stat_data"]["script_value"] = 1
    context(owner, records)
    with pytest.raises(ValueError, match="read_state"):
        owner.tools.reads.require_state()


def test_cloud_compaction_removes_old_evidence_but_retains_fixed_sources(owner):
    current = note(owner)
    records = exchange("write_note", {"key": "clue", "content": current["content"], "kind": "hypothesis"}, {"saved_to_draft": True})
    first = context(owner, records, compact=True)
    owner.tools.reads.require_note("clue")
    records.extend(first["context_additions"])
    owner.state["compaction"] = {"version": 1, "origin": response_origin(owner.settings),
        "signature": first["_compact_signature"], "covered": hashes(first["_compact_body"]),
        "items": [{"type": "compaction", "encrypted_content": "opaque-note"}]}
    after = context(owner, records, compact=True)
    assert after["compaction_opaque"]
    with pytest.raises(ValueError, match="read_note"):
        owner.tools.reads.require_note("clue")
    owner.tools.reads.require_character("description")
    records.extend(exchange("read_note", {"key": "clue"}, {"text": current["content"]}))
    context(owner, records, compact=True)
    owner.tools.reads.require_note("clue")


def test_plaintext_tool_arguments_and_sources_after_cloud_checkpoint_still_count(owner):
    from tavern.scripting.protocols import response_input

    current = note(owner)
    raw = lore(owner)
    owner.snapshot["lore_injections"] = [{"id": raw["id"], "anchor_id": "input", "title": "Harbor", "content": "Complete Guide lore"}]
    records = exchange("write_note", {"key": "clue", "content": current["content"], "kind": "hypothesis"}, {"saved_to_draft": True})
    first = context(owner, records, compact=True)
    records.extend(first["context_additions"])
    owner.state["compaction"] = {"version": 1, "origin": response_origin(owner.settings),
        "signature": first["_compact_signature"], "covered": hashes(first["_compact_body"]),
        "items": [{"type": "compaction", "encrypted_content": "older-exchanges"}, *response_input(first["_compact_body"])]}
    after = context(owner, records, compact=True)
    assert after["compaction_opaque"]
    assert not any(row.get("tool_calls") for row in after["trace"])
    owner.tools.reads.require_note("clue")
    owner.tools.reads.require_entry("L1", raw)


async def test_cross_turn_own_note_write_survives_fork_and_requires_read_after_rollover(story):
    store, session = story
    service = runtime(store)
    def write(text):
        return ("write_note", {"key": "clue", "content": text, "kind": "hypothesis"})
    try:
        result, session = await turn(service, session, Model(write("First version"), "Narrative"))
        assert result["status"] == "committed"
        fork = store.fork_session(session["id"], session["head"], session["revision"])
        model = Model(write("Fork version"), "Fork narrative")
        result, fork = await turn(service, fork, model)
        assert result["status"] == "committed"
        assert fork["snapshot"]["full_agent"]["notes"]["clue"]["content"] == "Fork version"
        assert "First version" in json.dumps(model.requests[0]["messages"])
        model = Model(("new_context", {"handoff": "Continue", "keep_recent": 0}), write("Denied version"),
                      ("read_note", {"key": "clue"}), write("After reread"), "Narrative")
        result, session = await turn(service, session, model)
        assert result["status"] == "committed"
        errors = [e["payload"]["result"]["error"]["code"] for e in store.events(result["id"])
                  if e["kind"] == "full_agent_tool" and not e["payload"]["result"]["ok"]]
        assert errors == ["READ_REQUIRED"]
        assert session["snapshot"]["full_agent"]["notes"]["clue"]["content"] == "After reread"
    finally:
        await service.close()


async def test_complete_variables_survive_turn_without_read_tool(story):
    store, session = story
    service = runtime(store)
    try:
        result, session = await turn(service, session, Model(("read_state", {}), "First narrative"))
        assert result["status"] == "committed"
        with store.connection() as db:
            db.execute("UPDATE sessions SET disabled_tools=? WHERE id=?", ('["read_state"]', session["id"]))
        model = Model(("patch_state", {"operations": [{"op": "test", "path": "", "value": {}}], "comment": "Verify current state"}), "Narrative")
        result, session = await turn(service, session, model, update_variables=True)
        assert result["status"] == "committed", result["error"]
        assert all(e["payload"]["result"]["ok"] for e in store.events(result["id"]) if e["kind"] == "full_agent_tool")
        assert "read_state" not in {t["function"]["name"] for t in model.requests[0]["extra_body"]["tools"]}
    finally:
        await service.close()
