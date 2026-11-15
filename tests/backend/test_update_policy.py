"""Saving mode does not grant editing rights or review routine memory work."""

import copy
import json
from types import SimpleNamespace

import pytest

from tavern.conversation.update_review import review_changes
from tavern.core.model_view import ModelView
from tavern.core.models import SessionCreate, Settings, TurnRequest
from tavern.core.settings import StorySettings
from tavern.storage.store import Store
from tavern.story.protocol import ToolError
from tavern.story.tools import AgentTools, tool_definitions
from tests.backend import test_story
from tests.backend.test_story import Model, runtime, turn

story = test_story.story


@pytest.mark.parametrize(
    ("legacy", "expected"),
    [
        ("directed", "automatic"),
        ("evolving", "automatic"),
        ("preview", "review"),
    ],
)
def test_legacy_policy_migrates_without_losing_review(tmp_path, legacy, expected):
    assert Settings(policy=legacy).policy == StorySettings(policy=legacy).policy == expected
    store = Store(tmp_path)
    card = store.import_card(b'{"name":"Guide","first_mes":"Hello"}')
    session = store.new_session(SessionCreate(card_id=card["id"]))
    with store.connection() as db:
        db.execute("UPDATE sessions SET policy=? WHERE id=?", (legacy, session["id"]))
    current = store.session(session["id"])
    assert current["policy"] == current["policy_effective"] == expected
    assert store.fork_session(current["id"], current["head"], current["revision"])["policy"] == expected


def test_review_compares_real_values_and_content_not_metadata():
    card = {"name": "Guide", "description": "Before", "character_book": {"entries": []}}
    before = {"state": {}, "scopes": {}, "content": None}
    after = {
        "state": {"stat_data": {}, "schema": {"type": "object"}, "initialized_lorebooks": {"x": True}},
        "scopes": {"chat": {}},
        "content": {"document": card, "reason": "read", "turn": "new"},
        "story": {
            "notes": {"timeline": {"content": "A new note"}},
            "model_references": {"@D1": "opaque"},
        },
        "lore": {"1": {"present": True}},
        "lore_injections": [{"id": "1"}],
    }
    assert review_changes(before, after, card) == []
    after["state"]["stat_data"] = {"hp": 3, "$meta": {"changed": True}}
    changes = review_changes(before, after, card)
    assert [c["kind"] for c in changes] == ["variables"]
    assert "$meta" not in json.dumps(changes)
    after["content"]["document"] = copy.deepcopy(card)
    after["content"]["document"]["description"] = "After"
    after["content"]["document"]["character_book"]["entries"].append({"content": "New rule"})
    after["scopes"]["global"] = {"coins": 10}
    assert [c["kind"] for c in review_changes(before, after, card)] == [
        "variables",
        "script_variables",
        "character",
        "lorebook",
    ]


@pytest.mark.parametrize("policy", ["automatic", "review", "evolving", "preview"])
async def test_policy_never_grants_content_permission(policy):
    request = TurnRequest(operation_id="check", expected_revision=0)
    session = {"policy_effective": policy}
    definitions = tool_definitions(session)
    assert "revise_content" in {t["function"]["name"] for t in definitions}
    tools = AgentTools(SimpleNamespace(request=request, definitions=definitions, model_view=ModelView({})))
    with pytest.raises(ToolError) as denied:
        await tools.execute("revise_content", "{}")
    assert denied.value.result["error"]["code"] == "PERMISSION_DENIED"


async def test_no_mvu_card_notes_and_retrieval_commit_without_review(story):
    store, session = story
    session = store.update_session(session["id"], session["revision"], policy="review")
    cfg = store.settings()
    cfg.story_config.auto_retrieval = True
    store.save_settings(cfg)
    service = runtime(store)
    try:
        result, current = await turn(
            service,
            session,
            Model(
                ("write_note", {"key": "timeline/autumn", "content": "Meet tomorrow", "kind": "plan"}),
                "Story",
            ),
        )
        assert result["status"] == "committed", result["error"]
        assert current["snapshot"]["state"] == session["snapshot"]["state"]
        assert current["snapshot"]["story"]["notes"]["timeline/autumn"]["content"] == "Meet tomorrow"
        assert not any(e["kind"] == "awaiting_review" for e in store.events(result["id"]))
    finally:
        await service.close()


@pytest.mark.parametrize("accept", [True, False])
async def test_content_review_holds_entire_turn_and_survives_reload(story, accept):
    store, session = story
    session = store.update_session(session["id"], session["revision"], policy="review")
    service = runtime(store)
    try:
        result, current = await turn(
            service,
            session,
            Model(
                ("write_note", {"key": "timeline", "content": "Meet tomorrow", "kind": "plan"}),
                ("read_character", {"ref": "description"}),
                (
                    "revise_content",
                    {
                        "operations": [{"op": "set_character", "field": "description", "value": "Changed"}],
                        "comment": "Explicit request",
                    },
                ),
                "Story",
            ),
            allow_content_edit=True,
        )
        assert result["status"] == "awaiting_review", result["error"]
        assert current["snapshot"] == session["snapshot"] and current["head"] == session["head"]
        # Reading a pending turn (also used on reopen) must expose only real review targets.
        assert [c["kind"] for c in store.turn(result["id"])["pending"]["review_changes"]] == ["character"]
        service.store.review_turn(result["id"], accept)
        current = store.session(session["id"])
        assert (current["head"] != session["head"]) == accept
        assert bool(current["snapshot"]["story"]["notes"]) == accept
        assert (current["snapshot"].get("content") != session["snapshot"].get("content")) == accept
    finally:
        await service.close()
