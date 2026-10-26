"""Token budgets must not regress into byte or character counts."""

import json

from tavern.conversation.context import compile_context
from tavern.core.models import ContextRecipe, Settings
from tavern.core.tokens import count_text, counting_info, encoding, request_tokens


def test_bpe_counts_english_chinese_and_literal_special_markers_offline(monkeypatch):
    import tiktoken

    monkeypatch.setattr(
        tiktoken, "get_encoding", lambda *_: (_ for _ in ()).throw(AssertionError("no download"))
    )
    encoding.cache_clear()
    assert count_text("hello world") == 2
    assert count_text("你好，世界！") == 4
    assert count_text("") == 0
    assert count_text("<|endoftext|>") > 1
    assert count_text("hello " * 100) < len("hello " * 100)


def test_unknown_model_is_explicit_reference_estimate_and_roles_are_independent():
    cfg = Settings(
        foreground={"model": "gemini-example"}, background={"model": "custom", "tokenizer": "cl100k_base"}
    )
    foreground, background = cfg.for_role("foreground"), cfg.for_role("background")
    assert counting_info(foreground)["source"] == "reference_fallback"
    assert counting_info(background)["source"] == "configured"
    assert counting_info(background)["tokenizer"] == "cl100k_base"
    assert count_text("你好，世界！", foreground) != count_text("你好，世界！", background)
    assert counting_info(Settings(model="gpt-4o"))["source"] == "model_mapping"


def test_compile_budget_uses_tokens_and_breakdown_matches_final_wire():
    cfg = Settings(context_tokens=4096, reply_tokens=128, chunk_mode="off", system_prompt="规则")
    session = {"id": "s", "persona": "人", "recipe": ContextRecipe().model_dump()}
    text = "界" * 1000  # 3,000 UTF-8 bytes but 1,000 o200k tokens.
    context = compile_context(
        {}, session, [], {}, text, cfg, "t", retrieval={"ids": [], "query": "", "diagnostics": []}
    )
    assert context["estimated_tokens"] < 1920 < len(text.encode())
    assert context["estimated_tokens"] == request_tokens(context["messages"], cfg)
    assert sum(context["budget_sources"].values()) == context["estimated_tokens"]
    assert context["token_counting"]["unit"] == "tokens"
    assert all(row["unit"] == "tokens" for row in context["trace"])


def test_recipe_token_warning_counts_expanded_macros_without_omission():
    recipe = ContextRecipe().model_dump()
    recipe["blocks"].append({"id": "bounded", "source": "custom", "content": "{{user}}", "budget": 20})
    session = {"id": "s", "persona": "界" * 100, "recipe": recipe}
    context = compile_context(
        {}, session, [], {}, "继续", Settings(), "t", retrieval={"ids": [], "query": "", "diagnostics": []}
    )
    assert any(item["source"] == "bounded" and item["content"] == "界" * 100 for item in context["trace"])
    assert any(item["id"] == "bounded" and "仍完整注入" in item["reason"] for item in context["diagnostics"])


def test_tool_definition_and_native_continuation_costs_are_included():
    messages = [{"role": "user", "content": "你好"}]
    tool = [{"type": "function", "function": {"name": "read", "description": "界" * 200}}]
    continued = messages + [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "call", "function": {"name": "read", "arguments": json.dumps({"key": "界" * 100})}}
            ],
        },
        {"role": "tool", "tool_call_id": "call", "content": "界" * 100},
    ]
    assert request_tokens(messages, tools=tool) > request_tokens(messages) + 200
    assert request_tokens(continued, tools=tool) > request_tokens(messages, tools=tool) + 200
    reasoning = "核对完整原文，再继续处理。" * 100
    thought = {"role": "assistant", "content": "已核对", "reasoning_content": reasoning}
    delta = request_tokens([thought]) - request_tokens(
        [{k: v for k, v in thought.items() if k != "reasoning_content"}]
    )
    # Missing reasoning undercounts capacity; counting ASCII escapes instead of
    # Chinese causes premature rollover. Keep both errors outside these bounds.
    assert 500 < delta < 1600
    native = {"items": [{"type": "thinking", "thinking": reasoning}]}
    assert request_tokens([{**thought, "_native_response": native}]) == request_tokens(
        [{"role": "assistant", "content": "已核对", "_native_response": native}]
    )
