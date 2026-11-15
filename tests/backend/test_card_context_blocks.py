import copy

import pytest

from tavern.conversation.context import CARD_FIELDS
from tavern.core.models import Settings
from tavern.story.context import compile_window
from tavern.story.state import rollover, story_state
from tests.backend.test_story import written


def session():
    return {"id": "story", "persona": "用户", "head": None}


def card():
    return {"name": "向导", **{field: f"{field}: {{{{user}}}}" for field in CARD_FIELDS}}


def test_story_tags_card_fields_and_keeps_greeting_plain_without_mutating_story():
    history = [{"id": "opening", "parent": None, "role": "assistant", "raw": "备选开场", "model": "备选开场"}]
    original = copy.deepcopy(history)
    settings = Settings()
    document = card()
    document["post_history_instructions"] = "{{getvar::stat_data.post}}"
    snapshot = {"state": {"stat_data": {"post": "post_history_instructions: 用户"}}}
    story_state(snapshot)
    out = compile_window(document, session(), history, snapshot, "继续", settings, [])
    text = "\n".join(m["content"] for m in out["messages"])
    for field, tag in CARD_FIELDS.items():
        assert text.count(f"<{tag}>\n{field}: 用户\n</{tag}>") == 1
    # Assistant prose, including the greeting, has no runtime prefix.
    assert next(m["content"] for m in out["trace"] if m.get("message_id") == "opening") == "备选开场"
    assert history == original
    assert next(m["content"] for m in out["trace"] if m["source"] == "rules") == settings.story_system_prompt
    snapshot["state"]["stat_data"]["post"] = ""
    updated = compile_window(
        document, session(), history, snapshot, "继续", settings, [], continuation=out["context_additions"]
    )
    assert updated["context_ledger"]["rebuild_reason"] is None
    empty_post = [m["content"] for m in updated["trace"] if m["source"] == "post"][-1]
    assert empty_post == "<card_post_history_instructions>\n\n</card_post_history_instructions>"


def compile_card(document, history=(), *, snapshot=None, api="chat_completions", **settings):
    cfg = Settings(api_mode=api, retrieval={"channels": ["keyword"]}, **settings)
    snap = snapshot if snapshot is not None else {"state": {}}
    return compile_window(document, session(), list(history), snap, "继续", cfg, [])


@pytest.mark.parametrize("api", ["chat_completions", "responses", "anthropic", "gemini"])
def test_card_layout_survives_final_api_projection(api):
    document = {
        "spec": "chara_card_v2",
        "spec_version": "2.0",
        "data": {
            **card(),
            "first_mes": "UNSELECTED_DEFAULT",
            "alternate_greetings": ["所选开场", "UNSELECTED_ALTERNATIVE"],
            "creator_notes": "READER_ONLY",
            "creator": "AUTHOR_ONLY",
            "tags": ["TAG_ONLY"],
            "character_version": "VERSION_ONLY",
            "extensions": {"opaque": "EXTENSION_ONLY"},
        },
    }
    history = [{"id": "opening", "role": "assistant", "raw": "所选开场"}]
    original = copy.deepcopy((document, history))
    out = compile_card(
        document,
        history,
        api=api,
        story_system_prompt="CUSTOM_APP_RULES",
    )
    text = "\n".join(m["content"] for m in out["messages"])
    assert not any(m["source"] == "card_context_protocol" for m in out["trace"])
    assert "角色卡资料说明（由运行时提供）" not in text
    for field, tag in CARD_FIELDS.items():
        assert text.count(f"<{tag}>") == text.count(f"</{tag}>") == 1
        assert f"{field}: 用户" in text
    ordered = [
        "CUSTOM_APP_RULES",
        "<card_system_prompt>",
        "<card_description>",
        "<card_personality>",
        "<card_scenario>",
        "<user_character>",
        "<card_mes_example>",
        "所选开场",
        "[H2]\n继续\n",
        "<card_post_history_instructions>",
    ]
    positions = [text.index(value) for value in ordered]
    assert positions == sorted(positions)
    assert not any(
        value in text
        for value in (
            "UNSELECTED_DEFAULT",
            "UNSELECTED_ALTERNATIVE",
            "READER_ONLY",
            "AUTHOR_ONLY",
            "TAG_ONLY",
            "VERSION_ONLY",
            "EXTENSION_ONLY",
        )
    )
    assert (document, history) == original
    # Separate story fields retain their visible source identities.
    sources = {row["source"] for row in out["trace"]}
    assert "character" not in sources
    assert {"system_prompt", "description", "personality", "scenario"} <= sources


def test_card_fields_are_complete_over_budget_and_empty_fields_emit_nothing():
    document = {
        "name": "向导",
        "description": "开始\n" + "长设定。" * 4000 + "\n完整尾部",
        "personality": " \n",
        "scenario": "",
        "creator_notes": "NOT_RUNTIME",
    }
    original = copy.deepcopy(document)
    out = compile_card(document, context_tokens=4096, reply_tokens=128)
    text = "\n".join(m["content"] for m in out["messages"])
    assert f"<card_description>\n{document['description']}\n</card_description>" in text
    assert "<card_personality>" not in text and "<card_scenario>" not in text
    assert "NOT_RUNTIME" not in text
    assert document == original
    document["description"] = ""
    empty = compile_card(document)
    assert not any(f"<{tag}>" in m["content"] for m in empty["messages"] for tag in CARD_FIELDS.values())


def test_full_card_context_persists_across_tools_edits_and_rollover():
    document = card()
    document["scenario"] = "开局只有六件货物。"
    history = [
        {"id": "u1", "role": "user", "raw": "采购四件。"},
        {"id": "a1", "role": "assistant", "raw": "采购完成，现有十件。"},
    ]
    snapshot = {"state": {}}
    cfg = Settings()
    first = compile_window(document, session(), history, snapshot, "继续", cfg, [])
    again = compile_window(
        document, session(), history, snapshot, "继续", cfg, [], continuation=first["context_additions"]
    )
    assert again["context_ledger"]["rebuild_reason"] is None
    assert again["messages"] == first["messages"]
    document["personality"] = "新的性格设定"
    changed = compile_window(
        document, session(), history, snapshot, "继续", cfg, [], continuation=first["context_additions"]
    )
    assert changed["context_ledger"]["rebuild_reason"] == "context_sources_changed"
    rollover(story_state(snapshot), history, written(history, "采购已完成，当前十件，不回到开局的六件。"), 0)
    after = compile_window(document, session(), history, snapshot, "继续", cfg, [])
    text = "\n".join(m["content"] for m in after["messages"])
    assert "<card_scenario>\n开局只有六件货物。\n</card_scenario>" in text
    assert "<card_personality>\n新的性格设定\n</card_personality>" in text
    assert "采购已完成，当前十件，不回到开局的六件。" in text
    assert not any(m["source"] == "history" for m in after["trace"])
    assert any(m["source"] == "post" for m in after["trace"])
