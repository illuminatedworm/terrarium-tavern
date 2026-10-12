"""Retire broad card exchanges at the model boundary without deleting audit history."""
import copy
import json

import pytest

from tavern.core.models import TurnRequest
from tavern.full_agent.content_history import CONTENT_TOOLS_VERSION, content_history
from tavern.full_agent.context import compile_window, load_trace
from tavern.full_agent.tools import tool_definitions
from tests.backend import test_full_agent
from tests.backend.test_full_agent import Model, runtime, turn

story = test_full_agent.story


def tool_call(cid, name, arguments):
    return {"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


@pytest.mark.parametrize("api", ["responses", "anthropic"])
@pytest.mark.parametrize("ref", ["*", "character_book", "extensions"])
def test_retired_read_in_mixed_batch_preserves_pairs_and_invalidates_native_suffix(api, ref):
    messages = [{"role": "assistant", "content": "Working", "tool_calls": [
        tool_call("old", "read_character", {"ref": ref}), tool_call("state", "read_state", {})],
        "_native_response": {"origin": {"api": api}, "items": [{"content": "NATIVE_PRIVATE_CARD"}]},
        "reasoning_content": "PRIVATE_CARD_REASONING"},
        {"role": "tool", "tool_call_id": "old", "content": json.dumps({"text": "PRIVATE_CARD_SOURCE"})},
        {"role": "tool", "tool_call_id": "state", "content": '{"text":"{}"}'},
        {"role": "assistant", "content": "Narrative", "_native_response": {"items": ["NATIVE_PRIVATE_CARD"]}}]
    original = copy.deepcopy(messages)
    result, changed = content_history(messages)
    assert changed and messages == original
    assert [m["role"] for m in result] == ["assistant", "tool", "assistant"]
    assert result[0]["tool_calls"] == [messages[0]["tool_calls"][1]]
    assert result[1]["tool_call_id"] == "state"
    assert all("_native_response" not in m and "reasoning_content" not in m for m in result)
    assert "PRIVATE_CARD" not in json.dumps(result)


def test_legacy_generic_card_patches_and_reused_call_ids_do_not_corrupt_safe_history():
    messages = [{"role": "assistant", "content": "Working", "tool_calls": [
        tool_call("same", "revise_content", {"operations": [{"op": "replace", "path": "", "value": "PRIVATE_CARD"}]})]},
        {"role": "tool", "tool_call_id": "same", "content": '{"saved_to_draft":true}'},
        {"role": "assistant", "content": "Working", "tool_calls": [tool_call("same", "read_character", {"ref": "description"})]},
        {"role": "tool", "tool_call_id": "same", "content": '{"text":"Allowed description"}'}]
    result, changed = content_history(messages)
    assert changed and result == messages[2:]
    current = [{**messages[0], "_content_tools_version": CONTENT_TOOLS_VERSION},
        {"role": "tool", "tool_call_id": "same", "content": '{"error":"Use restricted actions"}'}]
    assert content_history(current) == (current, False)  # Current schema feedback remains actionable.


def test_old_bulk_exchanges_cannot_reach_compiled_model_requests(story):
    store, session = story
    settings = store.settings().for_role("full_agent")
    snapshot = copy.deepcopy(session["snapshot"])
    window = snapshot["full_agent"]["window"]["id"]
    document = store.session_card(session["id"])["document"]
    req = TurnRequest(operation_id="preview", expected_revision=0)
    messages = [{"role": "assistant", "content": "Old work", "tool_calls": [
        tool_call("old", "read_character", {"ref": "*"})]},
        {"role": "tool", "tool_call_id": "old", "content": '{"text":"PRIVATE_CARD_SOURCE"}'}]
    trace = [{"message_id": session["head"], "window_id": window, "message": message} for message in messages]
    original = copy.deepcopy(trace)
    result = compile_window(document, session, session["messages"], snapshot, "Continue", settings,
        tool_definitions(req, session), trace=trace, continuation=messages)
    assert "PRIVATE_CARD_SOURCE" not in json.dumps(result["messages"])
    assert not any(m.get("tool_calls") or m["role"] == "tool" for m in result["messages"])
    assert trace == original


async def test_entry_visibility_survives_failure_and_fork_but_not_excluded_regeneration_trace(story):
    store, session = story
    service = runtime(store)
    def update(text):
        return ("revise_content", {"operations": [{"op": "update_lore", "ref": "L1", "changes": {"content": text}}], "comment": "Setting"})
    try:
        failed, unchanged = await turn(service, session, Model(("read_lore_entry", {"ref": "L1"}), RuntimeError("Disconnected")), allow_content_edit=True)
        assert failed["status"] == "error" and unchanged["head"] == session["head"]
        progress = store.full_agent_progress(failed["id"])
        assert "L1" in progress["tool_reads"]["entries"]
        service.full.provider_factory = lambda: Model(update("Original branch update"), "Narrative")
        resumed = service.submit(session["id"], TurnRequest(text=failed["request"]["text"], operation_id="resume-entry",
            expected_revision=0, allow_content_edit=True, resume_turn_id=failed["id"]))
        await service.tasks[resumed["id"]]
        assert store.turn(resumed["id"])["status"] == "committed"
        current = store.session(session["id"])
        branch = store.fork_session(current["id"], current["head"], current["revision"])
        result, branch = await turn(service, branch, Model(update("Fork update"), "Branch narrative"), allow_content_edit=True)
        assert result["status"] == "committed", result["error"]
        assert not any(e["kind"] == "full_agent_tool" and "error" in e["payload"]["result"] for e in store.events(result["id"]))
        assert branch["snapshot"]["content"]["document"]["data"]["character_book"]["entries"][0]["content"] == "Fork update"
        assert store.session(current["id"])["snapshot"]["content"]["document"]["data"]["character_book"]["entries"][0]["content"] == "Original branch update"
        result, current = await turn(service, current, Model(update("Unqualified regeneration"),
            ("read_lore_entry", {"ref": "L1"}), update("Regenerated setting"), "Replacement"),
            mode="regenerate", allow_content_edit=True)
        assert result["status"] == "committed", result["error"]
        assert any(e["kind"] == "full_agent_tool" and "error" in e["payload"]["result"] for e in store.events(result["id"]))
    finally:
        await service.close()


async def test_old_progress_broad_calls_are_not_executed_or_replayed_and_audit_is_preserved(story):
    store, session = story
    service = runtime(store)
    try:
        failed, _ = await turn(service, session, Model(("read_character", {"ref": "description"}), RuntimeError("Disconnected")), allow_content_edit=True)
        progress = store.full_agent_progress(failed["id"])
        legacy = {"role": "assistant", "content": "Old work", "tool_calls": [tool_call("legacy", "read_character", {"ref": "*"})],
                  "_native_response": {"items": ["NATIVE_PRIVATE_CARD"]}}
        old_result = {"role": "tool", "tool_call_id": "legacy", "content": '{"text":"PRIVATE_CARD_SOURCE"}'}
        pending = {"role": "assistant", "content": "Old pending work", "tool_calls": [
            tool_call("legacy-pending", "revise_content", {"operations": [{"op": "replace", "path": "", "value": "PRIVATE_CARD_SOURCE"}]})]}
        progress["continuation"].extend([legacy, old_result, pending])
        window = progress["snapshot"]["full_agent"]["window"]["id"]
        progress["new_trace"].extend({"window_id": window, "message": message} for message in [legacy, old_result, pending])
        progress["tool_reads"].pop("version")
        store.save_full_agent_progress(failed["id"], progress)
        model = Model(("revise_content", {"operations": [{"op": "set_character", "field": "description", "value": "Updated"}], "comment": "Setting"}),
            ("read_character", {"ref": "description"}),
            ("revise_content", {"operations": [{"op": "set_character", "field": "description", "value": "Updated"}], "comment": "Setting"}), "Narrative")
        service.full.provider_factory = lambda: model
        resumed = service.submit(session["id"], TurnRequest(text=failed["request"]["text"], operation_id="resume-old-content",
            expected_revision=0, allow_content_edit=True, resume_turn_id=failed["id"]))
        await service.tasks[resumed["id"]]
        result = store.turn(resumed["id"])
        assert result["status"] == "committed", result["error"]
        assert "PRIVATE_CARD" not in json.dumps(model.requests)
        assert "PRIVATE_CARD" in json.dumps(store.full_agent_progress(failed["id"]))
        current = store.session(session["id"])
        assert "PRIVATE_CARD_SOURCE" in json.dumps(load_trace(store, session["id"], current["messages"]))
        # Store reconstructs earlier audit events on resume. Only newly executed
        # calls carry measured durations; the retired pending call has no result.
        executed = [e for e in store.events(resumed["id"]) if e["kind"] == "full_agent_tool"
                    and "duration_s" in e["payload"]]
        assert [e["payload"]["name"] for e in executed] == ["revise_content", "read_character", "revise_content"]
    finally:
        await service.close()
