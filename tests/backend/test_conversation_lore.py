import json

import pytest

from tavern.conversation.context import activate_lore, matches
from tavern.core.models import Settings
from tavern.search.retrieval import lexical_recall
from tavern.storage.store import Store
from tavern.story.context import compile_window
from tavern.story.lore import inject_lore


def document(**entry):
    return {
        "name": "角色",
        "character_book": {
            "entries": [
                {"id": "e", "keys": ["灯塔"], "content": "灯塔：{{getvar::place}}", "cooldown": 4, **entry}
            ]
        },
    }


def test_constants_cannot_recursively_activate_unrelated_entries():
    doc = {
        "name": "角色",
        "character_book": {
            "entries": [
                {"id": "rules", "constant": True, "content": "咏唱，BLEACH"},
                {"id": "spell", "keys": ["咏唱"], "content": "鬼道"},
                {"id": "knight", "keys": ["B"], "content": "哈斯沃德"},
            ]
        },
    }
    settings = Settings(retrieval={"channels": ["keyword"]})
    result = lexical_recall(doc, [], "继续", {}, settings, {"id": "fixture"}, "seed")
    assert result["rankings"]["keyword"] == []


def test_short_latin_codes_require_author_opt_in_and_word_boundaries():
    assert not matches("B", "BLEACH", {})
    assert not matches("B", "BLEACH", {"use_regex": True})
    assert not matches("B", "BLEACH", {"use_regex": True, "matchWholeWords": True})
    assert matches("/B/", "BLEACH", {})
    assert not matches("U", "{{user}}", {})
    assert not matches("B", "圣文字 B", {})
    assert matches("B", "圣文字 B", {"matchWholeWords": True})
    assert not matches("B", "BLEACH", {"matchWholeWords": True})
    assert not matches("AI", "said", {})
    assert matches("AI", "AI 扮演", {})


def test_exported_lore_flags_preserve_matching_recursion_and_source():
    from tavern.character.cards import export_card, lore_entries, parse_card
    from tavern.search.lore_admission import recursive_candidates

    original = {
        "spec": "chara_card_v3",
        "spec_version": "3.0",
        "data": {
            "name": "角色",
            "character_book": {
                "entries": [
                    {
                        "id": "a",
                        "keys": ["LIGHT", "B"],
                        "content": "海港：灯塔完整设定",
                        "extensions": {
                            "match_whole_words": True,
                            "case_sensitive": True,
                            "prevent_recursion": True,
                        },
                    },
                    {"id": "b", "keys": ["海港"], "content": "远山完整设定"},
                    {
                        "id": "c",
                        "keys": ["海港"],
                        "content": "排除递归的完整设定",
                        "extensions": {"exclude_recursion": True},
                    },
                ]
            },
        },
    }
    raw = json.dumps(original).encode()
    doc, kind, assets, _ = parse_card(raw)
    entries = lore_entries(doc)
    assert entries[0]["matchWholeWords"] is True
    assert matches("B", "B", {**entries[0], "use_regex": True})
    assert not matches("B", "BLEACH", {**entries[0], "use_regex": True})
    assert not matches("LIGHT", "LIGHTING", entries[0])
    assert not matches("LIGHT", "light", entries[0])
    assert activate_lore(doc, [], "LIGHTING", {}, "seed")[0] == []
    assert activate_lore(doc, [], "light", {}, "seed")[0] == []
    assert recursive_candidates(entries, ["a"], {"a"}, 1) == {}
    selected, _, _ = activate_lore(doc, [], "LIGHT", {}, "seed")
    assert [e["id"] for e in selected] == ["a"]

    def delivered(ids):
        snapshot = {"state": {}}
        session = {"id": "fixture", "persona": "用户", "messages": []}
        settings = Settings(lore_injection_title=False)
        inject_lore(
            doc,
            session,
            [],
            snapshot,
            "LIGHT",
            settings,
            "seed",
            "input",
            {"ids": ids, "query": "LIGHT"},
            set(),
        )
        return [
            message["content"]
            for message in compile_window(
                doc, session, [], snapshot, "LIGHT", settings, [], input_id="input"
            )["messages"]
        ]

    assert "<lorebook>\n[L1 · 全文]\n海港：灯塔完整设定\n</lorebook>" in delivered(["a"])
    assert doc == original
    assert json.loads(export_card(doc, kind, raw, assets)[0]) == original

    # Existing explicit camelCase values must keep precedence over exported aliases.
    source = doc["data"]["character_book"]["entries"][0]
    source["preventRecursion"] = False
    entries = lore_entries(doc)
    assert entries[0]["preventRecursion"] is False
    assert recursive_candidates(entries, ["a"], {"a"}, 1) == {
        "b": {"recursive_parents": ["a"], "recursion_depth": 1}
    }
    bodies = delivered(["a", "b"])
    assert "<lorebook>\n[L2 · 全文]\n远山完整设定\n</lorebook>" in bodies
    assert all("排除递归的完整设定" not in body for body in bodies)
    assert [e["id"] for e in activate_lore(doc, [], "海港", {}, "seed")[0]] == ["b", "c"]
    source["matchWholeWords"] = False
    source["caseSensitive"] = None
    assert matches("LIGHT", "lighting", lore_entries(doc)[0])
    assert source["extensions"] == original["data"]["character_book"]["entries"][0]["extensions"]


@pytest.mark.parametrize(
    "values",
    [
        {"lore_recursion_depth": -1},
        {"lore_recursion_depth": 17},
        {"lore_recursion_depth": 1.5},
        {"lore_recursive": "false"},
    ],
)
def test_recursion_settings_reject_invalid_values(values):
    with pytest.raises(ValueError):
        Settings(**values)


def test_recursion_settings_persist_and_defaults(tmp_path):
    store = Store(tmp_path)
    assert store.settings().lore_recursive is True
    assert store.settings().lore_recursion_depth == 1
    store.save_settings(Settings(lore_recursive=False, lore_recursion_depth=7))
    assert store.settings().lore_recursive is False
    assert store.settings().lore_recursion_depth == 7


def test_body_only_short_keywords_do_not_activate_named_subjects():
    doc = {
        "name": "角色",
        "character_book": {
            "entries": [
                {"id": "a", "keys": ["力量", "米妮娜"], "use_regex": True, "content": "【米妮娜】\n力量"},
                {"id": "b", "keys": ["弱点", "纳纳纳"], "content": "【纳纳纳】\n弱点"},
            ]
        },
    }
    selected, diagnostics, _ = activate_lore(doc, [], "把力量注入刀中，攻击弱点", {}, "seed")
    assert selected == []
    assert len([note for note in diagnostics if "短关键词" in note["reason"]]) == 2
    assert [e["id"] for e in activate_lore(doc, [], "米妮娜的力量", {}, "seed")[0]] == ["a"]
    doc["character_book"]["entries"][0]["matchWholeWords"] = True
    assert [e["id"] for e in activate_lore(doc, [], "力量", {}, "seed")[0]] == ["a"]


def test_explicit_timeline_tags_exclude_later_arc_entries():
    doc = document(keys=["灭却师"], content="后期设定", timelines=["千年血战篇"])
    selected, diagnostics, _ = activate_lore(doc, [], "灭却师", {}, "seed", timeline="死神代行篇")
    assert selected == []
    assert any("时间线" in note["reason"] for note in diagnostics)
    selected, _, _ = activate_lore(doc, [], "灭却师", {}, "seed", timeline="千年血战篇")
    assert [e["id"] for e in selected] == ["e"]
    assert [e["id"] for e in activate_lore(doc, [], "灭却师", {}, "seed")[0]] == ["e"]


def test_weak_match_cannot_bypass_original_query_gate_through_recursion():
    doc = {
        "name": "角色",
        "character_book": {
            "entries": [
                {"id": "a", "keys": ["灯塔"], "content": "米妮娜"},
                {"id": "b", "keys": ["力量", "米妮娜"], "content": "【米妮娜】\n力量"},
            ]
        },
    }
    settings = Settings(retrieval={"channels": ["keyword"]})
    result = lexical_recall(doc, [], "灯塔的力量", {}, settings, {"id": "fixture"}, "seed")
    assert result["rankings"]["keyword"] == ["a"]
