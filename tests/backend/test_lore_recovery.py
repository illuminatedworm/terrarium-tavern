import json

import pytest
from httpx import ASGITransport, AsyncClient

from tavern.application.api import create_app
from tavern.conversation.context import activate_lore, lore_recovery
from tavern.core.models import SessionCreate
from tavern.core.models import Settings as AppSettings


def Settings(**values):
    values.setdefault("chunk_mode", "messages")
    return AppSettings(retrieval={"channels": ["keyword"]}, **values)


def entry(eid="e", **fields):
    return {"id": eid, "keys": ["灯塔"], "content": "灯塔设定", "cooldown": 4, **fields}


def document(*entries):
    return {"name": "测试角色", "first_mes": "欢迎", "character_book": {"entries": list(entries)}}


def history(n, text="继续"):
    return [
        {"id": f"m{i}", "role": "user", "raw": text, "model": text, "retrieval_query": text} for i in range(n)
    ]


def compile_lore(entries, *, prior=None, messages=(), text="灯塔", seed="turn", retrieval=None):
    selected, diagnostics, unchanged = activate_lore(
        document(*entries),
        messages,
        text,
        prior or {},
        seed,
        session_id="session",
        retrieval=retrieval,
        query=retrieval["query"] if retrieval else None,
    )
    return {"lore": selected, "diagnostics": diagnostics, "next_lore": unchanged}


def recovery_note(result):
    return next(n for n in result["diagnostics"] if n.get("id") == "e" and n["reason"] == "冷却恢复判断")


@pytest.mark.parametrize(
    "elapsed,expected", [(1, 15.910358), (4, 50), (8, 75), (12, 87.5), (16, 100), (30, 100)]
)
def test_recovery_curve_and_direct_boost(elapsed, expected):
    old = {"removed_at": 3}
    normal = lore_recovery(entry(), old, 3 + elapsed, False, "s", "p")
    direct = lore_recovery(entry(), old, 3 + elapsed, True, "s", "p")
    assert normal["recovery_elapsed"] == elapsed
    assert normal["recovery_probability"] == pytest.approx(expected, abs=1e-5)
    assert direct["recovery_probability"] == pytest.approx(expected + 0.75 * (100 - expected), abs=1e-5)
    assert direct["recovery_roll"] == normal["recovery_roll"]


def test_curve_is_monotonic_and_zero_or_unused_has_no_suppression():
    probabilities = [
        lore_recovery(entry(), {"removed_at": 1}, t, False, "s", "p")["recovery_probability"]
        for t in range(2, 25)
    ]
    assert probabilities == sorted(probabilities)
    for e, old in [(entry(), {}), (entry(cooldown=0), {"removed_at": 1})]:
        assert lore_recovery(e, old, 2, False, "s", "p")["recovery_probability"] == 100


def test_recovery_draw_is_stable_and_independent_of_generation_seed():
    args = {"prior": {"e": {"removed_at": 1}}, "messages": history(1)}
    a = compile_lore([entry()], seed="original", **args)
    b = compile_lore([entry()], seed="regenerated", **args)
    assert recovery_note(a) == recovery_note(b)
    assert compile_lore([entry()], seed="original", **args) == a
    draws = {
        lore_recovery(entry(), args["prior"]["e"], 2, False, s, p)["recovery_roll"]
        for s, p in [("s", "p"), ("s2", "p"), ("s", "p2")]
    }
    draws.add(lore_recovery(entry("other"), args["prior"]["e"], 2, False, "s", "p")["recovery_roll"])
    draws.add(lore_recovery(entry(), args["prior"]["e"], 3, False, "s", "p")["recovery_roll"])
    assert len(draws) == 5
    a = compile_lore([entry(cooldown=0)], seed="original", **args)
    b = compile_lore([entry(cooldown=0)], seed="regenerated", **args)
    assert a["lore"][0]["roll"] != b["lore"][0]["roll"]
    assert a["lore"][0]["recovery_roll"] == b["lore"][0]["recovery_roll"]


@pytest.mark.parametrize(
    "key,fields,text,direct",
    [
        ("灯塔", {}, "灯塔", True),
        ("LIGHT", {"caseSensitive": True}, "light", False),
        ("LIGHT", {}, "light", True),
        ("light", {"matchWholeWords": True}, "lighting", False),
        ("/灯[塔台]/", {}, "灯台", True),
    ],
)
def test_direct_mention_uses_normal_matching(key, fields, text, direct):
    result = compile_lore(
        [entry(keys=[key], **fields)],
        text=text,
        retrieval={"ids": ["e"], "query": text, "diagnostics": []},
    )
    assert recovery_note(result)["direct_mention"] is direct


def test_history_and_recursion_do_not_get_direct_boost():
    result = compile_lore([entry()], messages=history(1, "灯塔"), text="继续")
    assert not recovery_note(result)["direct_mention"]
    result = compile_lore(
        [entry("source", keys=["港口"], content="灯塔", insertion_order=200), entry(insertion_order=100)],
        text="港口",
    )
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


def test_failed_draws_do_not_reset_state_and_recall_still_obeys_cooldown():
    old = {"e": {"removed_at": 1, "cooldown_until": 999}}
    # Choose an actual deterministic failing draw rather than rely on a lucky fixture.
    parent = next(
        f"p{i}"
        for i in range(100)
        if not lore_recovery(entry(), old["e"], 2, False, "session", f"p{i}")["recovery_passed"]
    )
    messages = [{"id": parent, "role": "user", "raw": "灯塔", "retrieval_query": "灯塔"}]
    for fields in ({}, {"keys": []}):
        result = compile_lore(
            [entry(**fields)],
            prior=old,
            messages=messages,
            text="继续",
            retrieval={"ids": ["e"], "query": "灯塔", "diagnostics": []},
        )
        assert not result["lore"]
        assert result["next_lore"] == {"e": {"removed_at": 1}}
        assert not recovery_note(result)["direct_mention"]
    assert old["e"]["cooldown_until"] == 999


def test_sticky_is_ignored_and_constant_bypasses_recovery():
    prior = {"e": {"present": False, "removed_at": 1}}
    assert not compile_lore([entry(sticky=100)], prior=prior, messages=history(2), text="继续")["lore"]
    result = compile_lore(
        [entry(constant=True, probability=0)], prior=prior, messages=history(1), text="继续"
    )
    assert result["lore"][0]["reason"] == "常驻"
    assert result["next_lore"] == prior


def test_candidate_activation_does_not_consume_usage():
    prior = {"e": {"removed_at": 1, "cooldown_until": 999}}
    selected, _, proposed = activate_lore(document(entry()), history(20), "灯塔", prior, "nested")
    assert selected
    assert proposed == {"e": {"removed_at": 1}}


async def test_api_preview_is_read_only(tmp_path):
    app = create_app(tmp_path)
    store = app.state.store
    card = store.import_card(json.dumps(document(entry())).encode())
    initial = store.new_session(SessionCreate(card_id=card["id"]))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        a = await client.post(f"/api/sessions/{initial['id']}/preview", json={"text": "灯塔"})
        b = await client.post(f"/api/sessions/{initial['id']}/preview", json={"text": "灯塔"})
    assert a.status_code == b.status_code == 200
    assert a.json()["messages"] == b.json()["messages"]
    assert store.session(initial["id"]) == initial
    await app.state.service.close()
