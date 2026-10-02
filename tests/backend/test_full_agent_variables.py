"""MVU rule delivery, durable snapshots and shared authoring contracts."""
import copy
import json

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from tavern.application.api import create_app
from tavern.application.modes import ModeRuntime
from tavern.conversation.variable_rules import collect_variable_rules
from tavern.core.models import SessionCreate, Settings, TurnRequest
from tavern.full_agent.context import compile_window, load_trace
from tavern.full_agent.state import new_state
from tavern.full_agent.tools import Patch, StatePatch
from tavern.storage.store import Store
from tests.backend.test_full_agent import Model, runtime, turn
from tests.backend.test_full_agent_alignment import strip_local


def document():
    return {"name": "Rule fixture", "first_mes": "Opening", "character_book": {"entries": [
        {"id": "story", "content": "NARRATIVE_ONLY", "constant": True},
        {"id": "rule", "content": "RULE_A {{user}}", "constant": True,
         "extensions": {"terrarium": {"audience": "tracker_mvu"}}},
        {"id": "conditional", "content": "HP_AT_START={{getvar::stat_data.hp}}", "keys": ["unmatched"],
         "probability": 0, "useProbability": True,
         "extensions": {"terrarium": {"audience": "tracker_mvu"}}},
        {"id": "disabled", "content": "DISABLED_RULE", "enabled": False,
         "extensions": {"terrarium": {"audience": "tracker_mvu"}}},
        {"id": "init", "content": "hp: 8", "extensions": {"terrarium": {"audience": "runtime_init"}}},
    ]}}


def game(tmp_path, *, real=False):
    store = Store(tmp_path)
    card = store.import_card(json.dumps(document()).encode(), "fixture.json")
    settings = store.settings()
    settings.full_agent_config.auto_retrieval = False
    settings.retrieval.channels = ["keyword"]
    store.save_settings(settings)
    session = store.new_session(SessionCreate(card_id=card["id"], agent_mode="full_agent"))
    return store, ModeRuntime(store) if real else runtime(store), session


def frames(messages):
    return [m["content"] for m in messages if m.get("content", "").startswith("<mvu_rules>\n")]


def empty_patch():
    return ("patch_state", {"operations": [], "reason": "No changes in this turn"})


@pytest.mark.parametrize("object_entries", [False, True])
def test_rules_are_complete_authorized_and_independent_of_lore_admission(object_entries):
    doc = document()
    long_rule = "Long rule " * 3000 + "END_RULE"
    doc["character_book"]["entries"][1]["content"] += " " + long_rule
    if object_entries:
        doc["character_book"]["entries"] = {str(i): row for i, row in enumerate(doc["character_book"]["entries"])}
    settings = Settings()
    session = {"id": "fixture", "persona": "Reader", "agent_mode": "full_agent"}
    snapshot = {"state": {"stat_data": {"hp": 8}}, "full_agent": new_state()}
    before = copy.deepcopy(snapshot)
    plain = compile_window(doc, session, [], snapshot, "Continue", settings, [])
    assert not frames(plain["messages"])
    assert "RULE_A" not in json.dumps(plain["messages"])
    loaded = compile_window(doc, session, [], snapshot, "Continue", settings, [], require_state_patch=True)
    [rules] = frames(loaded["messages"])
    assert "RULE_A Reader " + long_rule in rules and "HP_AT_START=8" in rules
    assert all(value not in rules for value in ("NARRATIVE_ONLY", "DISABLED_RULE", "hp: 8"))
    assert snapshot == before  # preview compilation must not persist injection state
    assert loaded["budget_sources"]["mvu_rules"] > 0


async def test_delayed_injection_deduplicates_across_tools_and_turns_without_granting_permission(tmp_path):
    store, service, session = game(tmp_path)
    try:
        ordinary = Model("First narrative")
        _, session = await turn(service, session, ordinary)
        assert not frames(ordinary.requests[0]["messages"])
        required = Model(("read_state", {}), empty_patch(), empty_patch(), "Second narrative")
        result, session = await turn(service, session, required, update_variables=True)
        assert result["status"] == "committed", result["error"]
        requests = [strip_local(r["messages"]) for r in required.requests]
        assert all(len(frames(r)) == 1 for r in requests)
        for first, second in zip(requests, requests[1:]):
            assert first == second[:len(first)]
        returns = [json.loads(m["content"]) for m in requests[-1] if m["role"] == "tool"]
        assert returns[-2]["saved_to_draft"] and "error" in returns[-1]

        unrequested = Model(empty_patch(), "Third narrative")
        result, session = await turn(service, session, unrequested)
        assert result["status"] == "committed", result["error"]
        assert len(frames(unrequested.requests[-1]["messages"])) == 1
        assert "未要求更新" in [m["content"] for m in unrequested.requests[-1]["messages"] if m["role"] == "tool"][-1]
        repeated = Model(empty_patch(), "Fourth narrative")
        result, session = await turn(service, session, repeated, update_variables=True)
        assert result["status"] == "committed", result["error"]
        assert len(frames(repeated.requests[0]["messages"])) == 1
        assert len([r for r in load_trace(store, session["id"], session["messages"])
                    if r["message"].get("source") == "mvu_rules"]) == 1
    finally:
        await service.close()


async def test_rollover_reloads_rules_and_regeneration_and_forks_use_their_own_trace(tmp_path):
    store, service, session = game(tmp_path)
    opening = session["head"]
    try:
        result, session = await turn(service, session, Model(empty_patch(), "First"), update_variables=True)
        assert result["status"] == "committed", result["error"]
        branch = store.fork_session(session["id"], opening, session["revision"])
        isolated = Model("Branch without variable update")
        _, branch = await turn(service, branch, isolated)
        assert not frames(isolated.requests[0]["messages"])

        regenerated = Model(empty_patch(), "Replacement")
        result, session = await turn(service, session, regenerated, mode="regenerate", update_variables=True)
        assert result["status"] == "committed", result["error"]
        assert len(frames(regenerated.requests[0]["messages"])) == 1
        switched = Model(("new_context", {"content": "Keep current facts", "keep_recent": 0}),
                         empty_patch(), "After rollover")
        result, session = await turn(service, session, switched, update_variables=True)
        assert result["status"] == "committed", result["error"]
        assert all(len(frames(r["messages"])) == 1 for r in switched.requests)
        assert not any(m["role"] == "tool" for m in switched.requests[1]["messages"])
        assert len({r["window_id"] for r in load_trace(store, session["id"], session["messages"])
                    if r["message"].get("source") == "mvu_rules"}) == 2
    finally:
        await service.close()


@pytest.mark.parametrize("operation,expected", [
    ({"op": "replace", "path": "/character_book/entries/1/content", "value": "RULE_B"}, ["RULE_B", "HP_AT_START="]),
    ({"op": "add", "path": "/character_book/entries/-", "value": {
        "content": "RULE_NEW", "extensions": {"terrarium": {"audience": "tracker_mvu"}}}}, ["RULE_A", "RULE_NEW"]),
    ({"op": "add", "path": "/character_book/entries/1/enabled", "value": False}, ["HP_AT_START="]),
    ({"op": "replace", "path": "/character_book/entries", "value": []}, ["当前没有启用"]),
])
async def test_rule_changes_take_effect_next_turn_and_empty_rules_revoke_previous_rules(tmp_path, operation, expected):
    store, service, session = game(tmp_path)
    try:
        changed = Model(("read_character", {"ref": "character_book"}),
                        ("revise_content", {"operations": [operation], "reason": "User requested rule update"}),
                        empty_patch(), "Narrative")
        result, session = await turn(service, session, changed, update_variables=True, allow_content_edit=True)
        assert result["status"] == "committed", result["error"]
        for request in changed.requests:
            [rules] = frames(request["messages"])
            assert "RULE_A" in rules and "RULE_B" not in rules and "RULE_NEW" not in rules
        follow = Model(empty_patch(), "Next narrative")
        result, session = await turn(service, session, follow, update_variables=True, allow_content_edit=True)
        assert result["status"] == "committed", result["error"]
        rules = frames(follow.requests[0]["messages"])[-1]
        assert all(value in rules for value in expected)
        if "RULE_A" not in expected:
            assert "RULE_A" not in rules
    finally:
        await service.close()


async def test_frozen_macros_and_rules_survive_real_mvu_rollover_reopen_and_model_change(tmp_path):
    store, service, session = game(tmp_path, real=True)
    try:
        session = await service.initialize_session(session["id"])
        assert session["snapshot"]["state"]["stat_data"]["hp"] == 8
        model = Model(("read_state", {}),
            ("patch_state", {"operations": [{"op": "replace", "path": "/hp", "value": 5}], "reason": "Injury"}),
            ("read_character", {"ref": "character_book"}),
            ("revise_content", {"operations": [{"op": "replace", "path": "/character_book/entries/1/content", "value": "RULE_B"}], "reason": "Next turn rules"}),
            ("new_context", {"content": "Injury applied, continue narrative", "keep_recent": 0}),
            RuntimeError("Disconnected"))
        failed, unchanged = await turn(service, session, model, update_variables=True, allow_content_edit=True)
        assert failed["status"] == "error", failed
        assert unchanged["snapshot"]["state"]["stat_data"]["hp"] == 8
        for request in model.requests:
            [rules] = frames(request["messages"])
            assert "HP_AT_START=8" in rules and "RULE_A" in rules and "RULE_B" not in rules
        progress = store.full_agent_progress(failed["id"])
        assert "HP_AT_START=8" in progress["variable_rules"]
    finally:
        await service.close()

    reopened = Store(store.root)
    settings = reopened.settings_document()
    settings.full_agent.model.model = "another-local-model"
    reopened.save_settings(settings)
    service = ModeRuntime(reopened)
    try:
        resumed_model = Model("Narrative after recovery")
        service.full.provider_factory = lambda: resumed_model
        resumed = service.submit(session["id"], TurnRequest(text=failed["request"]["text"],
            operation_id="resume-rules", expected_revision=session["revision"], update_variables=True,
            allow_content_edit=True, resume_turn_id=failed["id"]))
        await service.tasks[resumed["id"]]
        result = reopened.turn(resumed["id"])
        assert result["status"] == "committed", result["error"]
        [rules] = frames(resumed_model.requests[0]["messages"])
        assert "HP_AT_START=8" in rules and "RULE_A" in rules and "RULE_B" not in rules
        current = reopened.session(session["id"])
        assert current["snapshot"]["state"]["stat_data"]["hp"] == 5
        following = Model(("read_state", {}), empty_patch(), "Next narrative")
        result, current = await turn(service, current, following, update_variables=True, allow_content_edit=True)
        assert result["status"] == "committed", result["error"]
        rules = frames(following.requests[0]["messages"])[-1]
        assert "HP_AT_START=5" in rules and "RULE_B" in rules and "RULE_A" not in rules
        assert current["snapshot"]["state"]["stat_data"]["hp"] == 5
    finally:
        await service.close()


@pytest.mark.parametrize("allow_content_edit", [False, True])
@pytest.mark.parametrize("update_variables", [False, True])
async def test_preview_matches_first_request_without_consuming_rule_injection(
        tmp_path, allow_content_edit, update_variables):
    store, service, session = game(tmp_path)
    app = create_app(store.root)
    try:
        async with AsyncClient(transport=ASGITransport(app), base_url="http://testserver") as client:
            url = f"/api/sessions/{session['id']}/preview"
            plain = await client.post(url, json={"text": "Continue"})
            assert plain.status_code == 200 and not frames(plain.json()["messages"])
            assert not any(m["source"] == "turn_instruction" for m in plain.json()["trace"])
            payload = {"text": "Continue", "update_variables": update_variables,
                       "allow_content_edit": allow_content_edit}
            loaded = await client.post(url, json=payload)
            assert loaded.status_code == 200
            instructions = [m["content"] for m in loaded.json()["trace"] if m["source"] == "turn_instruction"]
            assert instructions == ([f"允许修改设定：{allow_content_edit}。"]
                                    if update_variables or allow_content_edit else [])
            repeated = await client.post(url, json=payload)
            assert frames(repeated.json()["messages"]) == frames(loaded.json()["messages"])
        assert load_trace(store, session["id"], session["messages"]) == []
        model = Model(*([empty_patch()] if update_variables else []), "Narrative")
        result, session = await turn(service, session, model,
                                     update_variables=update_variables, allow_content_edit=allow_content_edit)
        assert result["status"] == "committed", result["error"]
        assert frames(model.requests[0]["messages"]) == frames(loaded.json()["messages"])
        assert [m["content"] for m in result["preview"]["trace"] if m["source"] == "turn_instruction"] == instructions
        for request in model.requests:
            assert [m["content"] for m in request["messages"]
                    if m.get("content", "").startswith("允许修改设定：")] == instructions
        tools = {tool["function"]["name"] for tool in model.requests[0]["extra_body"]["tools"]}
        assert ("revise_content" in tools) == allow_content_edit
        async with AsyncClient(transport=ASGITransport(app), base_url="http://testserver") as client:
            unchecked = await client.post(url, json={"text": "Continue"})
            assert unchecked.status_code == 200
            assert not any(m["source"] == "turn_instruction" for m in unchecked.json()["trace"])
        unchecked_model = Model("Next narrative")
        result, _ = await turn(service, session, unchecked_model)
        assert result["status"] == "committed", result["error"]
        assert not any(m["source"] == "turn_instruction" for m in result["preview"]["trace"])
        assert not any("允许修改设定" in m.get("content", "") for m in unchecked_model.requests[0]["messages"])
    finally:
        await service.close()
        await app.state.editor.close()
        await app.state.service.close()


def test_empty_patch_is_specific_to_variable_updates():
    assert StatePatch(operations=[], reason="No change").operations == []
    with pytest.raises(ValidationError):
        Patch(operations=[], reason="No content edit")
    rules = collect_variable_rules(document(), "Reader", {"stat_data": {"hp": 8}})
    assert rules == ["RULE_A Reader", "HP_AT_START=8"]
