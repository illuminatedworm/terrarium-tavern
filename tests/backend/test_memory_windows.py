from tavern.conversation.context import compile_context
from tavern.core.models import ContextRecipe, Settings
from tavern.memories.memory import fingerprint
from tests.support.memory_fixtures import memory_snapshot


def history(n):
    return [
        {
            "id": f"m{i}",
            "role": "assistant" if i % 2 == 0 else "user",
            "raw": f"原文{i}",
            "model": f"原文{i}",
        }
        for i in range(n)
    ]


def session():
    return {
        "id": "s",
        "persona": "用户",
        "persona_data": {},
        "lore_injection_mode": "conversation",
        "recipe": ContextRecipe().model_dump(),
    }


def config():
    return Settings(history_message_cap=12, chunk_size=4, memory_interval_turns=2)


def test_trimmed_history_keeps_memory_even_when_recipe_block_disabled():
    messages = history(9)
    state = {
        **memory_snapshot("已经发生的重要剧情"),
        "context_window": {"covered_count": 5, "fingerprint": fingerprint(messages[:5])},
    }
    current = session()
    for block in current["recipe"]["blocks"]:
        if block["source"] == "memory":
            block["enabled"] = False
    result = compile_context({}, current, messages, state, "继续", config(), "protected-memory")
    assert any(
        item["source"] == "memory" and "已经发生的重要剧情" in item["content"] for item in result["trace"]
    )
