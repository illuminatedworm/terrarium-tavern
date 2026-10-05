"""Read permissions protect draft edits without restricting branch note sources."""
import copy
import json
from types import SimpleNamespace

import pytest

from tavern.core.model_view import ModelView
from tavern.core.models import TurnRequest
from tavern.full_agent.state import new_state, note_view, rollover
from tavern.full_agent.tools import AgentTools, tool_definitions
from tests.backend import test_full_agent
from tests.backend.test_full_agent import Model, Script, runtime, turn

story = test_full_agent.story


@pytest.fixture(params=[False, True])
def owner(story, request):
    store, _ = story
    data = {"name": "Guide", "description": "Before", "personality": "Shy",
            "a/b~c": {"items": [1, 2]}, "extensions": {"value": 3}}
    doc = {"spec": "chara_card_v2", "spec_version": "2.0", "data": data} if request.param else data
    snapshot = {"state": {"stat_data": {"hp": 8}}, "scopes": {}}
    req = TurnRequest(operation_id="guards", expected_revision=0, update_variables=True, allow_content_edit=True)
    result = SimpleNamespace(document=doc, state=new_state(), snapshot=snapshot,
        request=req, session={}, tid="guards", model_view=ModelView(doc), patch_state_done=False,
        script=Script([], snapshot), settings=store.settings().for_role("full_agent"),
        source_history=[{"id": "history", "role": "assistant", "raw": "Before"},
                        {"id": "input", "role": "user", "raw": "Now"}])
    result.definitions = tool_definitions(req, {"policy_effective": "auto"}, document=doc)
    result.tools = AgentTools(result)
    return result


async def call(owner, name, **args):
    return await owner.tools.execute(name, json.dumps(args, ensure_ascii=False))


def path(owner, pointer):
    return ("/data" if owner.document.get("spec") else "") + pointer


async def revise(owner, *operations):
    return await call(owner, "revise_content", operations=list(operations), reason="User requested edit")


@pytest.mark.parametrize("read_path", ["", "stat_data"])
async def test_state_requires_current_complete_read_even_for_empty_patch(owner, read_path):
    async def denied():
        with pytest.raises(ValueError, match="完整读取最新变量"):
            await call(owner, "patch_state", operations=[], reason="No changes")
        assert not owner.patch_state_done

    await denied()
    await call(owner, "read_state", path="stat_data.hp")
    await denied()
    await call(owner, "read_state", path=read_path)
    owner.snapshot["state"]["stat_data"]["hp"] = 7
    await denied()
    await call(owner, "read_state", path=read_path)
    rollover(owner.state, [], "Continue", 0)
    await denied()
    await call(owner, "read_state", path=read_path)
    assert (await call(owner, "patch_state", operations=[], reason="No changes"))["saved_to_draft"]
    with pytest.raises(ValueError, match="不允许重复"):
        await call(owner, "patch_state", operations=[], reason="Repeat")


async def test_note_sources_resolve_without_reads_and_remain_stale_after_history_edits(owner):
    await call(owner, "write_note", key="Fact", content="Before", kind="fact", sources=["H1", "H2", "H1"])
    note = owner.state["notes"]["Fact"]
    assert [row["message_id"] for row in note["sources"]] == ["history", "input"]
    assert len(note["sources"][0]["hash"]) == 64
    owner.source_history[0]["raw"] = "Changed"
    assert note_view(note, owner.source_history)["stale"]
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
                self.last_tool_calls.extend(SimpleNamespace(id=f"batch-{i}", name=name,
                    arguments=json.dumps(args)) for i, (name, args) in enumerate([
                        ("patch_state", {"operations": [], "reason": "No changes"}),
                        ("read_character", {"ref": "description"}),
                        ("revise_content", {"operations": [{"op": "set_character", "field": "description",
                                             "value": "Batch revision"}], "reason": "User requested"}),
                    ]))
            return response

    try:
        result, current = await turn(service, session, BatchModel(("read_state", {}), "Narrative"),
                                     update_variables=True, allow_content_edit=True)
        assert result["status"] == "committed", result["error"]
        assert current["snapshot"]["content"]["document"]["data"]["description"] == "Batch revision"
        events = [e["payload"]["result"] for e in store.events(result["id"]) if e["kind"] == "full_agent_tool"]
        assert len(events) == 4 and all("error" not in e for e in events)
    finally:
        await service.close()


@pytest.mark.parametrize("legacy", [False, True])
async def test_read_permissions_survive_failure_resume_but_old_progress_requires_reread(story, legacy):
    store, session = story
    service = runtime(store)
    try:
        failed, _ = await turn(service, session, Model(("read_state", {}),
            ("read_character", {"ref": "description"}), RuntimeError("Disconnected")),
            update_variables=True, allow_content_edit=True)
        progress = store.full_agent_progress(failed["id"])
        assert progress["tool_reads"]["state"] and progress["tool_reads"]["fields"]["description"]
        if legacy:
            progress["tool_reads"].pop("version")
            store.save_full_agent_progress(failed["id"], progress)
        patch = ("patch_state", {"operations": [], "reason": "No changes"})
        edit = ("revise_content", {"operations": [{"op": "set_character", "field": "description",
                                 "value": "Revised"}], "reason": "User requested edit"})
        # The sample is a wrapped card. Missing legacy stamps cannot grant a write.
        responses = [patch, edit]
        if legacy:
            responses += [("read_character", {"ref": "description"}), edit]
        model = Model(*responses, "Narrative")
        service.full.provider_factory = lambda: model
        resumed = service.submit(session["id"], TurnRequest(text=failed["request"]["text"], operation_id="resume-reads",
            expected_revision=session["revision"], resume_turn_id=failed["id"],
            update_variables=True, allow_content_edit=True))
        await service.tasks[resumed["id"]]
        result = store.turn(resumed["id"])
        assert result["status"] == "committed", result["error"]
        errors = [e["payload"]["result"]["error"] for e in store.events(resumed["id"])
                  if e["kind"] == "full_agent_tool" and "error" in e["payload"]["result"]]
        assert len(errors) == (1 if legacy else 0)
        current = store.session(session["id"])
        assert current["snapshot"]["content"]["document"]["data"]["description"] == "Revised"
        for mode in ("send", "regenerate"):
            model = Model(patch, ("read_state", {}), patch, "Next narrative")
            result, current = await turn(service, current, model, update_variables=True, mode=mode)
            assert result["status"] == "committed", result["error"]
            assert any(e["kind"] == "full_agent_tool" and "error" in e["payload"]["result"]
                       for e in store.events(result["id"]))
    finally:
        await service.close()
