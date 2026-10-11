import copy

import pytest

from tavern.conversation.context import CARD_FIELDS, compile_context
from tavern.core.models import ContextBlock, ContextRecipe, Settings
from tavern.full_agent.context import compile_window
from tavern.full_agent.state import full_state, rollover
from tavern.memories.memory_evidence import evidence_pool
from tavern.memories.memory_protocol import References
from tests.backend.test_half_agent_alignment import Story


def card():
    return {"name": "向导", **{field: f"{field}: {{{{user}}}}" for field in CARD_FIELDS}}


def session(recipe=None):
    return {"id": "story", "persona": "用户", "recipe": (recipe or ContextRecipe()).model_dump(),
            "lore_injection_mode": "conversation"}


def compile(recipe=None, **kwargs):
    return compile_context(card(), session(recipe), [], {"state": {}}, "继续",
                           Settings(retrieval={"channels": ["keyword"]}), "fixture",
                           retrieval_protocol=False, **kwargs)


@pytest.mark.parametrize("legacy", [False, True])
def test_all_card_fields_are_tagged_and_runtime_rules_are_plain(legacy):
    recipe = ContextRecipe()
    if legacy:
        recipe.blocks = [b for b in recipe.blocks if b.source not in
                         ("system_prompt", "description", "personality", "scenario")]
        recipe.blocks.insert(1, ContextBlock(id="character", source="character"))
    out = compile(recipe)
    text = "\n".join(m["content"] for m in out["messages"])
    for field, tag in CARD_FIELDS.items():
        assert text.count(f"<{tag}>\n{field}: 用户\n</{tag}>") == 1
    assert next(m["content"] for m in out["trace"] if m["source"] == "rules") == Settings().system_prompt
    assert not any(m["source"] == "character" for m in compile(ContextRecipe(blocks=[]))["trace"])


def test_compatibility_overrides_and_ordered_fields_keep_tags():
    out = compile(generate={"overrides": {
        "char_description": "新描述", "char_personality": "新性格", "scenario": "新场景",
        "dialogue_examples": "新示例",
    }, "ordered_prompts": ["char_personality", "scenario", "char_description", "dialogue_examples"]})
    assert [m["content"] for m in out["messages"][1:]] == [
        "<card_personality>\n新性格\n</card_personality>",
        "<card_scenario>\n新场景\n</card_scenario>",
        "<card_description>\n新描述\n</card_description>",
        "<card_mes_example>\n新示例\n</card_mes_example>",
    ]


def test_individual_blocks_and_dynamic_sources_obey_exact_saved_order():
    recipe = ContextRecipe(blocks=[
        ContextBlock(id="scene", source="scenario"),
        ContextBlock(id="status", source="state"),
        ContextBlock(id="request", source="input", role="user"),
        ContextBlock(id="desc", source="description"),
        ContextBlock(id="dialogue", source="history"),
        ContextBlock(id="note", source="author_note"),
        ContextBlock(id="app", source="rules"),
        ContextBlock(id="personality", source="personality"),
    ])
    saved = recipe.model_dump()
    assert ContextRecipe.model_validate(saved).model_dump() == saved
    out = compile_context(card(), session(recipe), [{"id": "h", "role": "assistant", "raw": "旧文"}],
                          {"state": {}}, "继续", Settings(), "fixture", retrieval_protocol=False,
                          generate={"overrides": {"chat_history": {"author_note": "作者注"}}})
    assert [m["source"] for m in out["trace"]] == [
        b.id if b.source != "history" else "history" for b in recipe.blocks]
    note = next(m["content"] for m in out["trace"] if m["source"] == "note")
    assert note == "<author_note>\n作者注\n</author_note>"
    status = next(m["content"] for m in out["trace"] if m["source"] == "status")
    assert status.startswith("<tavern_state>\n") and status.endswith("\n</tavern_state>")


def test_depth_positions_use_custom_input_ids_and_preserve_lore_anchor():
    recipe = ContextRecipe(blocks=[
        ContextBlock(id="dialogue", source="history"),
        ContextBlock(id="request", source="input", role="user"),
        ContextBlock(id="status", source="state", depth=1),
        ContextBlock(id="desc", source="description", depth=2),
    ])
    doc = card()
    doc["character_book"] = {"entries": [{"id": "l", "keys": ["继续"], "content": "设定"}]}
    out = compile_context(doc, session(recipe), [{"id": "h", "role": "user", "raw": "之前"}],
                          {"state": {}}, "继续", Settings(), "fixture", retrieval_protocol=False)
    assert [m["source"] for m in out["trace"]] == [
        "desc", "history", "status", "lore:l", "request"]
    assert next(m for m in out["trace"] if m["source"] == "status")["context_source"] == "state"


def test_reordering_trailing_frames_rebuilds_in_the_new_order():
    story = Story()
    first = story.compile()
    story.commit(first)
    blocks = story.session["recipe"]["blocks"]
    note = next(b for b in blocks if b["source"] == "author_note")
    blocks.remove(note)
    blocks.append(note)
    changed = story.compile()
    assert changed["context_ledger"]["rebuild_reason"]
    assert [m["context_source"] for m in changed["trace"][-2:]] == ["state", "author_note"]
    story.commit(changed)
    blocks.remove(note)
    blocks.insert(0, note)
    changed = story.compile()
    assert changed["context_ledger"]["rebuild_reason"]
    assert next(m["context_source"] for m in changed["trace"]
                if m["source"] != "retrieval_protocol") == "author_note"


def test_full_agent_tags_card_fields_and_keeps_greeting_plain_without_mutating_story():
    history = [{"id": "opening", "parent": None, "role": "assistant", "raw": "备选开场", "model": "备选开场"}]
    original = copy.deepcopy(history)
    settings = Settings()
    document = card()
    document["post_history_instructions"] = "{{getvar::stat_data.post}}"
    snapshot = {"state": {"stat_data": {"post": "post_history_instructions: 用户"}}}
    full_state(snapshot)
    out = compile_window(document, session(), history, snapshot, "继续", settings, [])
    text = "\n".join(m["content"] for m in out["messages"])
    for field, tag in CARD_FIELDS.items():
        assert text.count(f"<{tag}>\n{field}: 用户\n</{tag}>") == 1
    assert next(m["content"] for m in out["trace"] if m.get("message_id") == "opening") == "备选开场"
    assert history == original
    assert next(m["content"] for m in out["trace"] if m["source"] == "rules") == settings.full_agent_system_prompt
    snapshot["state"]["stat_data"]["post"] = ""
    updated = compile_window(document, session(), history, snapshot, "继续", settings, [],
                             continuation=out["context_additions"])
    assert updated["context_ledger"]["rebuild_reason"] is None
    empty_post = [m["content"] for m in updated["trace"] if m["source"] == "post"][-1]
    assert empty_post == "<card_post_history_instructions>\n\n</card_post_history_instructions>"


def test_tracker_card_references_are_tagged_without_changing_citation_excerpts():
    refs = References()
    history = refs.message({"id": "opening", "parent": None, "role": "assistant", "raw": "开场"})
    assert history["passages"][0]["text"] == "开场"
    assert refs.sources["S1"]["excerpt"] == "开场"
    evidence = [
        {"id": "desc", "kind": "character", "identity": "description", "text": "设定"},
        {"id": "lore", "kind": "frozen_lore", "text": "世界书"},
        {"id": "state", "kind": "state_reference_only", "text": "状态"},
    ]
    original = copy.deepcopy(evidence)
    assert [item["text"] for item in refs.evidence(evidence)] == [
        "<card_description>\n设定\n</card_description>",
        "<lorebook>\n世界书\n</lorebook>", "状态"]
    assert evidence == original


@pytest.mark.parametrize("greeting", ["初始开场", "备选开场"])
def test_half_agent_keeps_greeting_plain_in_context(greeting):
    history = [{"id": "opening", "parent": None, "role": "assistant", "raw": greeting, "model": greeting}]
    original = copy.deepcopy(history)
    out = compile_context(card(), session(), history, {"state": {}}, "继续", Settings(), "fixture")
    assert next(m["content"] for m in out["trace"] if m.get("message_id") == "opening") == greeting
    assert history == original


def compile_mode(mode, document, history=(), *, snapshot=None, api="chat_completions", **settings):
    cfg = Settings(runtime_mode=mode, api_mode=api, retrieval={"channels": ["keyword"]}, **settings)
    cfg._adapter_lane = "full_agent" if mode == "full_agent" else "foreground"
    snap = snapshot if snapshot is not None else {"state": {}}
    if mode == "full_agent":
        return compile_window(document, session(), list(history), snap, "继续", cfg, [])
    return compile_context(document, session(), list(history), snap, "继续", cfg, "fixture",
                           retrieval_protocol=False, replay_records={})


@pytest.mark.parametrize("mode", ["half_agent", "full_agent"])
@pytest.mark.parametrize("api", ["chat_completions", "responses", "anthropic", "gemini"])
def test_card_layout_survives_final_api_projection(mode, api):
    document = {"spec": "chara_card_v2", "spec_version": "2.0", "data": {
        **card(), "first_mes": "UNSELECTED_DEFAULT", "alternate_greetings": ["所选开场", "UNSELECTED_ALTERNATIVE"],
        "creator_notes": "READER_ONLY", "creator": "AUTHOR_ONLY", "tags": ["TAG_ONLY"],
        "character_version": "VERSION_ONLY", "extensions": {"opaque": "EXTENSION_ONLY"},
    }}
    history = [{"id": "opening", "role": "assistant", "raw": "所选开场"}]
    original = copy.deepcopy((document, history))
    out = compile_mode(mode, document, history, api=api, system_prompt="CUSTOM_APP_RULES",
                       full_agent_system_prompt="CUSTOM_APP_RULES")
    text = "\n".join(m["content"] for m in out["messages"])
    assert not any(m["source"] == "card_context_protocol" for m in out["trace"])
    assert "角色卡资料说明（由运行时提供）" not in text
    for field, tag in CARD_FIELDS.items():
        assert text.count(f"<{tag}>") == text.count(f"</{tag}>") == 1
        assert f"{field}: 用户" in text
    ordered = ["CUSTOM_APP_RULES", "<card_system_prompt>", "<card_description>",
               "<card_personality>", "<card_scenario>", "<user_character>", "<card_mes_example>",
               "所选开场", "\n继续\n", "<card_post_history_instructions>"]
    positions = [text.index(value) for value in ordered]
    assert positions == sorted(positions)
    assert not any(value in text for value in ("UNSELECTED_DEFAULT", "UNSELECTED_ALTERNATIVE", "READER_ONLY",
                   "AUTHOR_ONLY", "TAG_ONLY", "VERSION_ONLY", "EXTENSION_ONLY"))
    assert (document, history) == original
    # Separate full-agent fields retain identities in preview/token accounting.
    if mode == "full_agent":
        assert "character" not in out["budget_sources"]
        assert all(out["budget_sources"][field] > 0
                   for field in ("system_prompt", "description", "personality", "scenario"))


@pytest.mark.parametrize("mode", ["half_agent", "full_agent"])
def test_card_fields_are_complete_over_budget_and_empty_fields_emit_nothing(mode):
    document = {"name": "向导", "description": "开始\n" + "长设定。" * 4000 + "\n完整尾部",
                "personality": " \n", "scenario": "", "creator_notes": "NOT_RUNTIME"}
    original = copy.deepcopy(document)
    out = compile_mode(mode, document, context_tokens=4096, reply_tokens=128)
    text = "\n".join(m["content"] for m in out["messages"])
    assert f"<card_description>\n{document['description']}\n</card_description>" in text
    assert out["estimated_tokens"] > 4096
    assert "<card_personality>" not in text and "<card_scenario>" not in text
    assert "NOT_RUNTIME" not in text
    assert document == original
    document["description"] = ""
    empty = compile_mode(mode, document)
    assert not any(f"<{tag}>" in m["content"] for m in empty["messages"] for tag in CARD_FIELDS.values())


def test_disabled_half_card_recipe_does_not_restore_fields():
    recipe = ContextRecipe()
    for block in recipe.blocks:
        if block.source in (*CARD_FIELDS, "examples", "post"):
            block.enabled = False
    out = compile(recipe)
    text = "\n".join(m["content"] for m in out["messages"])
    assert not any(f"<{tag}>" in text for tag in CARD_FIELDS.values())


def test_full_card_context_persists_across_tools_edits_and_rollover():
    document = card()
    document["scenario"] = "开局只有六件货物。"
    history = [{"id": "u1", "role": "user", "raw": "采购四件。"},
               {"id": "a1", "role": "assistant", "raw": "采购完成，现有十件。"}]
    snapshot = {"state": {}}
    cfg = Settings(runtime_mode="full_agent")
    first = compile_window(document, session(), history, snapshot, "继续", cfg, [])
    again = compile_window(document, session(), history, snapshot, "继续", cfg, [],
                           continuation=first["context_additions"])
    assert again["context_ledger"]["rebuild_reason"] is None
    assert again["messages"] == first["messages"]
    document["personality"] = "新的性格设定"
    changed = compile_window(document, session(), history, snapshot, "继续", cfg, [],
                             continuation=first["context_additions"])
    assert changed["context_ledger"]["rebuild_reason"] == "context_sources_changed"
    rollover(full_state(snapshot), history, "采购已完成，当前十件，不回到开局的六件。", 0)
    after = compile_window(document, session(), history, snapshot, "继续", cfg, [])
    text = "\n".join(m["content"] for m in after["messages"])
    assert "<card_scenario>\n开局只有六件货物。\n</card_scenario>" in text
    assert "<card_personality>\n新的性格设定\n</card_personality>" in text
    assert "采购已完成，当前十件，不回到开局的六件。" in text
    assert not any(m["source"] == "history" for m in after["trace"])
    assert any(m["source"] == "post" for m in after["trace"])


def test_memory_evidence_never_promotes_examples_or_other_openings():
    document = {**card(), "first_mes": "未选开场", "alternate_greetings": ["实际开场", "另一个开场"]}
    history = [{"id": "opening", "role": "assistant", "raw": "实际开场"}]
    pool = evidence_pool(history, document, 0, 1)
    assert {item["identity"] for item in pool} == {"description", "personality", "scenario"}
    assert all(item["kind"] == "character" for item in pool)
