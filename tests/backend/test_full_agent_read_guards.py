"""Read permissions protect draft edits without restricting branch note sources."""

import copy
import json
from types import SimpleNamespace

import pytest

from tavern.core.model_view import ModelView
from tavern.core.models import Settings, TurnRequest
from tavern.full_agent.state import new_state, note_view, rollover
from tavern.full_agent.tools import AgentTools, tool_definitions
from tests.backend import test_full_agent
from tests.backend.test_full_agent import Model, Script, runtime, turn

story = test_full_agent.story


@pytest.fixture
def owner():
    # State and note contracts are independent of the character-card envelope.
    # Card-format coverage belongs to test_full_agent_content.
    data = {
        "name": "Guide",
        "description": "Before",
        "personality": "Shy",
        "a/b~c": {"items": [1, 2]},
        "extensions": {"value": 3},
    }
    doc = data
    snapshot = {"state": {"stat_data": {"hp": 8}}, "scopes": {}}
    req = TurnRequest(
        operation_id="guards", expected_revision=0, update_variables=True, allow_content_edit=True
    )
    result = SimpleNamespace(
        document=doc,
        state=new_state(),
        snapshot=snapshot,
        request=req,
        session={},
        tid="guards",
        output_id="pending-reply",
        story=None,
        model_view=ModelView(doc),
        patch_state_done=False,
        script=Script([], snapshot),
        settings=Settings().for_role("full_agent"),
        source_history=[
            {"id": "history", "role": "assistant", "raw": "Before"},
            {"id": "input", "role": "user", "raw": "Now"},
        ],
    )
    result.definitions = tool_definitions(req, {"policy_effective": "auto"}, document=doc)
    result.tools = AgentTools(result)
    return result


async def call(owner, name, **args):
    result = await owner.tools.execute(name, json.dumps(args, ensure_ascii=False))
    assert result["ok"] is True
    return result["data"]


async def test_nonempty_patch_requires_current_complete_variables_read(owner):
    operations = [{"op": "replace", "path": "/hp", "value": 7}]

    async def denied():
        with pytest.raises(ValueError, match="完整最新变量"):
            await call(owner, "patch_state", operations=operations, comment="Injury")
        assert not owner.patch_state_done

    await denied()
    await call(owner, "read_state", path="/hp")
    await denied()
    await call(owner, "read_state", scope="full")
    owner.tools.reads.require_state()
    await call(owner, "read_state")
    owner.snapshot["state"]["stat_data"]["hp"] = 6
    await denied()
    await call(owner, "read_state")
    rollover(owner.state, [], None, 0)
    await denied()
    await call(owner, "read_state")
    assert (await call(owner, "patch_state", operations=operations, comment="Injury"))["saved_to_draft"]


async def test_empty_patch_needs_no_read_and_has_no_script_side_effects(owner):
    before = copy.deepcopy(owner.snapshot)

    async def forbidden(*args, **kwargs):
        raise AssertionError("Empty patch must not call the script runtime")

    owner.script.call = forbidden
    result = await call(owner, "patch_state", operations=[], comment="No changes")
    assert result["patch_state_completed"] and owner.patch_state_done
    assert owner.snapshot == before and owner.tools.reads.records["state"] is None


async def test_note_sources_resolve_immutable_references_without_reads(owner):
    await call(owner, "write_note", key="Fact", content="Before", kind="fact", sources=["H1", "H2", "H1"])
    note = owner.state["notes"]["Fact"]
    assert [row["message_id"] for row in note["sources"]] == ["history", "input"]
    assert note_view(note, owner.source_history)["sources"] == ["H1", "H2"]
    before = copy.deepcopy(note)
    for kind in ("fact", "hypothesis", "plan"):
        with pytest.raises(ValueError, match="无效的历史引用"):
            await call(owner, "write_note", key="Fact", content="Invalid", kind=kind, sources=["H999"])
        assert owner.state["notes"]["Fact"] == before


async def test_reads_and_writes_in_one_tool_batch_follow_execution_order(story):
    store, session = story
    service = runtime(store)

    class BatchModel(Model):
        async def chat_complete(self, messages, **kwargs):
            response = await super().chat_complete(messages, **kwargs)
            if len(self.requests) == 1:
                self.last_tool_calls.extend(
                    SimpleNamespace(id=f"batch-{i}", name=name, arguments=json.dumps(args))
                    for i, (name, args) in enumerate(
                        [
                            ("patch_state", {"operations": [], "comment": "No changes"}),
                            ("read_character", {"ref": "description"}),
                            (
                                "revise_content",
                                {
                                    "operations": [
                                        {
                                            "op": "set_character",
                                            "field": "description",
                                            "value": "Batch revision",
                                        }
                                    ],
                                    "comment": "User requested",
                                },
                            ),
                        ]
                    )
                )
            return response

    try:
        result, current = await turn(
            service,
            session,
            BatchModel(("read_state", {}), "Narrative"),
            update_variables=True,
            allow_content_edit=True,
        )
        assert result["status"] == "committed", result["error"]
        assert current["snapshot"]["content"]["document"]["data"]["description"] == "Batch revision"
        events = [
            e["payload"]["result"] for e in store.events(result["id"]) if e["kind"] == "full_agent_tool"
        ]
        assert len(events) == 4 and all("error" not in e for e in events)
    finally:
        await service.close()


@pytest.mark.parametrize("legacy", [False, True])
async def test_visibility_rebuilds_from_context_after_failure_even_with_legacy_progress(story, legacy):
    store, session = story
    service = runtime(store)
    try:
        failed, _ = await turn(
            service,
            session,
            Model(
                ("read_state", {}), ("read_character", {"ref": "description"}), RuntimeError("Disconnected")
            ),
            update_variables=True,
            allow_content_edit=True,
        )
        progress = store.full_agent_progress(failed["id"])
        assert progress["tool_reads"]["state"] and progress["tool_reads"]["fields"]["description"]
        if legacy:
            progress["tool_reads"].pop("version")
            store.save_full_agent_progress(failed["id"], progress)
        patch = ("patch_state", {"operations": [], "comment": "No changes"})
        edit = (
            "revise_content",
            {
                "operations": [{"op": "set_character", "field": "description", "value": "Revised"}],
                "comment": "User requested edit",
            },
        )
        # The complete field remains in the fixed context, even with no saved stamp.
        responses = [patch, edit]
        model = Model(*responses, "Narrative")
        service.full.provider_factory = lambda: model
        resumed = service.submit(
            session["id"],
            TurnRequest(
                text=failed["request"]["text"],
                operation_id="resume-reads",
                expected_revision=session["revision"],
                resume_turn_id=failed["id"],
                update_variables=True,
                allow_content_edit=True,
            ),
        )
        await service.tasks[resumed["id"]]
        result = store.turn(resumed["id"])
        assert result["status"] == "committed", result["error"]
        errors = [
            e["payload"]["result"]["error"]
            for e in store.events(resumed["id"])
            if e["kind"] == "full_agent_tool" and "error" in e["payload"]["result"]
        ]
        assert not errors
        current = store.session(session["id"])
        assert current["snapshot"]["content"]["document"]["data"]["description"] == "Revised"
        for mode in ("send", "regenerate"):
            model = Model(patch, ("read_state", {}), patch, "Next narrative")
            result, current = await turn(service, current, model, update_variables=True, mode=mode)
            assert result["status"] == "committed", result["error"]
            assert not any(
                e["kind"] == "full_agent_tool" and "error" in e["payload"]["result"]
                for e in store.events(result["id"])
            )
    finally:
        await service.close()
