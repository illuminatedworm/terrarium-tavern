import asyncio
import copy
import json

import pytest
from httpx import ASGITransport, AsyncClient
from kohakuterrarium.llm.base import NativeToolCall
from kohakuterrarium.testing.llm import ScriptedLLM, ScriptEntry

from tavern.application.api import create_app
from tavern.application.service import RoleplayRuntime
from tavern.conversation.context import activate_lore, compile_context, lore_recovery
from tavern.core.models import ContextRecipe, SessionCreate, TurnRequest
from tavern.core.models import Settings as AppSettings
from tavern.storage.store import Store


def Settings(**values):
    return AppSettings(retrieval={"channels": ["keyword"]}, **values)


def entry(eid="e", **fields):
    return {"id": eid, "keys": ["灯塔"], "content": "灯塔设定", "cooldown": 4, **fields}


def document(*entries):
    return {"name": "测试角色", "first_mes": "欢迎", "character_book": {"entries": list(entries)}}


def history(n, text="继续"):
    return [{"id": f"m{i}", "role": "user", "raw": text, "model": text, "retrieval_query": text} for i in range(n)]


def compile_lore(entries, *, prior=None, messages=None, text="灯塔", recipe=None, **kwargs):
    session = {"id": "session", "persona": "玩家", "recipe": recipe or ContextRecipe().model_dump()}
    snapshot = {"state": {}, "lore": prior or {}}
    before = copy.deepcopy(snapshot)
    result = compile_context(document(*entries), session, messages or [], snapshot,
                             text, kwargs.pop("settings", Settings()), kwargs.pop("seed", "seed"), **kwargs)
    assert snapshot == before
    return result


def recovery_note(result, eid="e"):
    return next(n for n in result["diagnostics"] if n["id"] == eid and n["reason"] == "冷却恢复判断")


@pytest.mark.parametrize("elapsed,expected", [(1, 15.910358), (4, 50), (8, 75), (12, 87.5), (16, 100), (30, 100)])
def test_recovery_curve_and_direct_boost(elapsed, expected):
    old = {"removed_at": 3}
    normal = lore_recovery(entry(), old, 3 + elapsed, False, "s", "p")
    direct = lore_recovery(entry(), old, 3 + elapsed, True, "s", "p")
    assert normal["recovery_elapsed"] == elapsed
    assert normal["recovery_probability"] == pytest.approx(expected, abs=1e-5)
    assert direct["recovery_probability"] == pytest.approx(expected + .75 * (100 - expected), abs=1e-5)
    assert direct["recovery_roll"] == normal["recovery_roll"]


def test_curve_is_monotonic_and_zero_or_unused_has_no_suppression():
    probabilities = [lore_recovery(entry(), {"removed_at": 1}, t, False, "s", "p")
                     ["recovery_probability"] for t in range(2, 25)]
    assert probabilities == sorted(probabilities)
    for e, old in [(entry(), {}), (entry(cooldown=0), {"removed_at": 1})]:
        assert lore_recovery(e, old, 2, False, "s", "p")["recovery_probability"] == 100


def test_recovery_draw_is_stable_and_independent_of_generation_seed():
    args = {"prior": {"e": {"removed_at": 1}}, "messages": history(1)}
    a = compile_lore([entry()], seed="original", **args)
    b = compile_lore([entry()], seed="regenerated", **args)
    assert recovery_note(a) == recovery_note(b)
    assert compile_lore([entry()], seed="original", **args) == a
    draws = {lore_recovery(entry(), args["prior"]["e"], 2, False, s, p)["recovery_roll"]
             for s, p in [("s", "p"), ("s2", "p"), ("s", "p2")]}
    draws.add(lore_recovery(entry("other"), args["prior"]["e"], 2, False, "s", "p")["recovery_roll"])
    draws.add(lore_recovery(entry(), args["prior"]["e"], 3, False, "s", "p")["recovery_roll"])
    assert len(draws) == 5
    a = compile_lore([entry(cooldown=0)], seed="original", **args)
    b = compile_lore([entry(cooldown=0)], seed="regenerated", **args)
    assert a["lore"][0]["roll"] != b["lore"][0]["roll"]
    assert a["lore"][0]["recovery_roll"] == b["lore"][0]["recovery_roll"]


@pytest.mark.parametrize("key,fields,text,direct", [
    ("灯塔", {}, "灯塔", True),
    ("LIGHT", {"caseSensitive": True}, "light", False),
    ("LIGHT", {}, "light", True),
    ("light", {"matchWholeWords": True}, "lighting", False),
    ("/灯[塔台]/", {}, "灯台", True),
])
def test_direct_mention_uses_normal_matching(key, fields, text, direct):
    result = compile_lore([entry(keys=[key], **fields)], text=text, requested=("e",), retrieval={"ids": ["e"], "query": text, "diagnostics": []})
    assert recovery_note(result)["direct_mention"] is direct


def test_history_and_recursion_do_not_get_direct_boost():
    result = compile_lore([entry()], messages=history(1, "灯塔"), text="继续")
    assert not recovery_note(result)["direct_mention"]
    result = compile_lore([entry("source", keys=["港口"], content="灯塔", insertion_order=200),
                           entry(insertion_order=100)], text="港口")
    assert {e["id"] for e in result["lore"]} == {"source"}
    assert not any(n.get("id") == "e" and n["reason"] == "冷却恢复判断" for n in result["diagnostics"])


def test_auxiliary_conditions_and_original_probability_still_gate_activation():
    assert not compile_lore([entry(secondary_keys=["夜晚"])])["lore"]
    result = compile_lore([entry(secondary_keys=["夜晚"])], messages=history(1, "夜晚"))
    assert result["lore"] and recovery_note(result)["direct_mention"]
    result = compile_lore([entry(probability=0)])
    assert recovery_note(result)["recovery_passed"]
    assert not result["lore"] and result["next_lore"] == {}
    assert compile_lore([entry(probability=0, useProbability=False)])["lore"]
    assert not compile_lore([entry()], text="无关话题")["lore"]


def test_failed_draws_do_not_reset_state_and_requests_still_obey_cooldown():
    old = {"e": {"removed_at": 1, "cooldown_until": 999}}
    # Choose an actual deterministic failing draw rather than rely on a lucky fixture.
    parent = next(f"p{i}" for i in range(100)
                  if not lore_recovery(entry(), old["e"], 2, False, "session", f"p{i}")["recovery_passed"])
    messages = [{"id": parent, "role": "user", "raw": "灯塔", "retrieval_query": "灯塔"}]
    for fields, requested in [({}, ()), ({"keys": []}, ("e",))]:
        result = compile_lore([entry(**fields)], prior=old, messages=messages, text="继续", requested=requested,
                              retrieval={"ids": ["e"], "query": "灯塔", "diagnostics": []})
        assert not result["lore"]
        assert result["next_lore"] == {"e": {"removed_at": 1}}
        assert not recovery_note(result)["direct_mention"]
    assert old["e"]["cooldown_until"] == 999


def test_sticky_is_ignored_and_constant_bypasses_recovery():
    prior = {"e": {"present": False, "removed_at": 1}}
    assert not compile_lore([entry(sticky=100)], prior=prior, messages=history(2), text="继续")["lore"]
    result = compile_lore([entry(constant=True, probability=0)], prior=prior, messages=history(1), text="继续")
    assert result["lore"][0]["reason"] == "常驻"
    assert result["next_lore"] == {"e": {"present": True}}


def test_reactivation_resets_only_actual_presence():
    prior = {"e": {"present": False, "removed_at": 1}}
    result = compile_lore([entry(sticky=3)], prior=prior, messages=history(20))
    assert result["next_lore"] == {"e": {"present": True}}
    recipe = ContextRecipe().model_dump()
    recipe["blocks"][1]["enabled"] = False
    omitted = compile_lore([entry(sticky=3)], prior=prior, messages=history(20), recipe=recipe)
    assert omitted["next_lore"] == prior


@pytest.mark.parametrize("position", [0, 1, 2, 3, 4, 5, 6, 7])
def test_actual_injections_record_all_supported_positions(position):
    recipe = ContextRecipe().model_dump()
    recipe["blocks"].append({"id": "custom-outlet", "source": "outlet:port", "depth": 1})
    result = compile_lore([entry(position=position, outletName="port", sticky=3)], recipe=recipe)
    assert result["next_lore"] == {"e": {"present": True}}
    assert {eid for m in result["trace"] for eid in m.get("lore_ids", [])} == {"e"}
    assert all(set(m) == {"role", "content"} for m in result["messages"])


@pytest.mark.parametrize("omission", ["disabled", "condition",
                                     "outlet", "unnamed_outlet", "override", "author_override",
                                     "ordered", "depth_disabled", "empty"])
def test_omitted_candidates_are_not_used(omission):
    e, recipe, kwargs = entry(sticky=3), ContextRecipe().model_dump(), {}
    block = recipe["blocks"][1]
    if omission == "disabled":
        block["enabled"] = False
    elif omission == "condition":
        block["condition"] = "missing"
    elif omission == "block_budget":
        block["budget"] = 1
    elif omission == "total_budget":
        e["content"] = "界" * 10000
        kwargs["settings"] = Settings(context_tokens=4096, reply_tokens=128, lore_max_chars=20000)
    elif omission in ("outlet", "unnamed_outlet"):
        e.update(position=7, outletName="unreferenced" if omission == "outlet" else "")
    elif omission == "override":
        kwargs["generate"] = {"overrides": {"world_info_before": "替代内容"}}
    elif omission == "author_override":
        e["position"] = 2
        kwargs["generate"] = {"overrides": {"chat_history": {"author_note": "替代内容"}}}
    elif omission == "ordered":
        kwargs["generate"] = {"ordered_prompts": ["user_input"]}
    elif omission == "depth_disabled":
        e["position"] = 4
        kwargs["generate"] = {"overrides": {"chat_history": {"with_depth_entries": False}}}
    else:
        e["content"] = "  "
    result = compile_lore([e], recipe=recipe, **kwargs)
    assert result["lore"] and result["next_lore"] == {}
    assert any(n["reason"] == "候选未注入，未计入使用记录" for n in result["diagnostics"])


@pytest.mark.parametrize("reason", ["block_budget", "total_budget"])
def test_budget_warning_keeps_selected_lore_complete(reason):
    e, recipe, kwargs = entry(sticky=3), ContextRecipe().model_dump(), {}
    if reason == "block_budget":
        recipe["blocks"][1]["budget"] = 1
    else:
        e["content"] = "界" * 10000
        kwargs["settings"] = Settings(context_tokens=4096, reply_tokens=128)
    result = compile_lore([e], recipe=recipe, **kwargs)
    assert result["next_lore"]["e"]["present"]
    assert e["content"] in "\n".join(m["content"] for m in result["messages"])


def test_merged_and_duplicate_blocks_record_each_entry_once():
    recipe = ContextRecipe().model_dump()
    recipe["blocks"].append({"id": "duplicate", "source": "lore_before"})
    result = compile_lore([entry("a", sticky=2), entry("b", sticky=4)], recipe=recipe)
    assert result["next_lore"] == {"a": {"present": True}, "b": {"present": True}}
    assert sum("a" in m.get("lore_ids", []) for m in result["trace"]) == 2


def test_omitted_candidate_cannot_trigger_unapproved_recursive_entry():
    recipe = ContextRecipe().model_dump()
    recipe["blocks"][1]["enabled"] = False
    result = compile_lore([entry("source", keys=["港口"], content="灯塔"), entry(position=4)],
                          recipe=recipe, text="港口")
    assert {e["id"] for e in result["lore"]} == {"source"}
    assert result["next_lore"] == {}


def test_candidate_activation_does_not_consume_usage():
    prior = {"e": {"removed_at": 1, "cooldown_until": 999}}
    selected, _, proposed = activate_lore(document(entry()), history(20), "灯塔", prior, "nested")
    assert selected
    assert proposed == {"e": {"removed_at": 1}}


@pytest.fixture
def story(tmp_path):
    store = Store(tmp_path)
    card = store.import_card(json.dumps(document(entry(sticky=0))).encode(), "card.json")
    session = store.new_session(SessionCreate(card_id=card["id"]))
    return store, session


async def send(service, session, **kwargs):
    turn = service.submit(session["id"], TurnRequest(
        text=kwargs.pop("text", "灯塔"), operation_id=kwargs.pop("operation_id", "send"),
        expected_revision=session["revision"], **kwargs))
    await service.tasks[turn["id"]]
    return service.store.turn(turn["id"])


async def test_regeneration_and_branch_restore_keep_recovery_stable(story):
    store, initial = story
    service = RoleplayRuntime(store)
    service.provider_factory = lambda: ScriptedLLM(["正文"])
    try:
        first = await send(service, initial)
        assert first["status"] == "committed", first["error"]
        branch = store.session(initial["id"])
        assert branch["snapshot"]["lore"] == {"e": {"present": True}}
        second = await send(service, branch, operation_id="second")
        assert second["status"] == "committed", second["error"]
        second_preview = second["preview"]
        current = store.session(initial["id"])
        regenerated = await send(service, current, mode="regenerate", operation_id="regen")
        assert regenerated["status"] == "committed", regenerated["error"]
        assert regenerated["preview"]["next_lore_injections"] == second_preview["next_lore_injections"]
        current = store.session(initial["id"])
        restored = store.update_session(initial["id"], current["revision"], head=branch["head"])
        assert restored["snapshot"]["lore"] == branch["snapshot"]["lore"]
        third = await send(service, restored, operation_id="branch")
        assert third["status"] == "committed", third["error"]
        assert third["preview"]["next_lore_injections"] == second_preview["next_lore_injections"]
    finally:
        await service.close()


@pytest.mark.parametrize("outcome", ["failure", "cancel", "reject", "accept"])
async def test_only_successful_or_approved_narrative_commits_usage(story, outcome):
    store, initial = story
    if outcome in ("reject", "accept"):
        initial = store.update_session(initial["id"], initial["revision"], policy="preview")
    service = RoleplayRuntime(store)
    service.provider_factory = lambda: ScriptedLLM(["" if outcome == "failure" else "正文"])
    try:
        if outcome == "cancel":
            service.provider_factory = lambda: ScriptedLLM([
                ScriptEntry(response="正文" * 100, delay_per_chunk=.1, chunk_size=1)])
            turn = service.submit(initial["id"], TurnRequest(
                text="灯塔", operation_id="cancel", expected_revision=initial["revision"]))
            for _ in range(200):
                if store.turn(turn["id"])["draft"]:
                    break
                await asyncio.sleep(.01)
            assert store.turn(turn["id"])["draft"]
            await service.cancel(turn["id"])
            assert store.turn(turn["id"])["status"] == "cancelled"
        else:
            turn = await send(service, initial)
            if outcome == "failure":
                assert turn["status"] == "error"
            else:
                assert turn["status"] == "awaiting_review", turn["error"]
                assert store.session(initial["id"])["snapshot"]["lore"] == {}
                service.review(turn["id"], outcome == "accept")
        assert store.session(initial["id"])["snapshot"]["lore"] == (
            {"e": {"present": True}} if outcome == "accept" else {})
    finally:
        await service.close()


async def test_maintenance_compilation_does_not_consume_narrative_usage(story):
    store, initial = story

    class MaintenanceLLM(ScriptedLLM):
        native = []

        @property
        def last_tool_calls(self):
            return self.native

        async def chat(self, messages, **kwargs):
            self.call_log.append(messages)
            if self.call_count == 0:
                self.native = [NativeToolCall(id="edit", name="revise_content", arguments=json.dumps({
                    "operations": [{"op": "add", "path": "/character_book/entries/0/enabled", "value": False}],
                    "reason": "停用条目",
                }))]
                self.call_count += 1
                yield ""
            else:
                self.native = []
                self.call_count += 1
                yield "维护完成" if self.call_count == 2 else "正文"

    provider = MaintenanceLLM()
    service = RoleplayRuntime(store)
    service.provider_factory = lambda: provider
    try:
        turn = await send(service, initial, allow_content_edit=True)
        assert turn["status"] == "committed", turn["error"]
        assert store.session(initial["id"])["snapshot"]["lore"] == {}
        assert provider.call_count == 3
        assert any("灯塔设定" in str(m.get("content")) for m in provider.call_log[0])
        assert all("灯塔设定" not in str(m.get("content")) for m in provider.call_log[-1])
    finally:
        await service.close()


async def test_nested_generate_does_not_consume_narrative_usage(story, monkeypatch):
    store, initial = story
    service = RoleplayRuntime(store)
    service.provider_factory = lambda: ScriptedLLM(["正文"])
    nested = ScriptedLLM(["嵌套回复"])

    async def nested_provider(settings, capture=None):
        return nested

    monkeypatch.setattr("tavern.scripting.agent.make_provider", nested_provider)
    runtime_open = service.runtime.open

    async def open_with_generation(doc, messages, snapshot, permission, host, *args):
        before = copy.deepcopy(snapshot)
        assert await host("generate", {"user_input": "灯塔"}) == "嵌套回复"
        assert snapshot == before
        return await runtime_open(doc, messages, snapshot, permission, host, *args)

    monkeypatch.setattr(service.runtime, "open", open_with_generation)
    try:
        turn = await send(service, initial, text="无关话题")
        assert turn["status"] == "committed", turn["error"]
        from uuid import UUID, uuid5
        assert nested.request_context.session_id == str(uuid5(UUID(initial["id"]), "half_agent:script"))
        assert any("灯塔设定" in str(m.get("content")) for m in nested.call_log[0])
        assert store.session(initial["id"])["snapshot"]["lore"] == {}
    finally:
        await service.close()


async def test_api_preview_is_read_only(tmp_path):
    app = create_app(tmp_path)
    store = app.state.store
    card = store.import_card(json.dumps(document(entry())).encode(), "card.json")
    initial = store.new_session(SessionCreate(card_id=card["id"]))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        a = await client.post(f"/api/sessions/{initial['id']}/preview", json={"text": "灯塔"})
        b = await client.post(f"/api/sessions/{initial['id']}/preview", json={"text": "灯塔"})
    assert a.status_code == b.status_code == 200
    assert a.json()["next_lore"] == {"e": {"present": True}}
    assert recovery_note(a.json()) == recovery_note(b.json())
    assert store.session(initial["id"]) == initial
