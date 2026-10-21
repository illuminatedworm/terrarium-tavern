"""New Helper macros obey the same model-data boundary as existing state blocks."""

import copy
import json

import pytest
import yaml

from tavern.conversation.context import compile_context, macros
from tavern.core.models import ContextRecipe, Settings
from tavern.core.variable_macros import replace_variable_macros
from tavern.full_agent.context import compile_window

OPAQUE = "12345678-90ab-cdef-1234-567890abcdef"


def state():
    return {
        "stat_data": {
            "hp": [45, "当前生命"],
            "items": ["钥匙", "地图"],
            "money": 0,
            "alive": False,
            "a.b": 7,
            "$internal": {"token": OPAQUE},
        },
        "initialized_lorebooks": {OPAQUE: ["初始化账本"]},
        "schema": {"id": OPAQUE},
    }


def test_scalar_pair_array_paths_and_scopes_are_not_coerced():
    original = state()
    source = copy.deepcopy(original)
    template = "{{format_message_variable::stat_data.hp.0}}|{{get_message_variable::stat_data.hp}}|{{get_chat_variable::stat_data.items}}|{{get_message_variable::stat_data.money}}|{{format_chat_variable::stat_data.alive}}"
    assert replace_variable_macros(template, source) == '45|[45,"当前生命"]|["钥匙","地图"]|0|false'
    assert (
        replace_variable_macros(
            "{{get_message_variable::stat_data[&quot;a.b&quot;]}}|{{get_global_variable::score}}",
            source,
            {"global": {"score": 3}},
        )
        == "7|3"
    )
    assert source == original


def test_yaml_prefix_quoting_null_and_internal_projection():
    source = state()
    assert (
        replace_variable_macros("物品：{{format_message_variable::stat_data.items}}", source)
        == "物品：- 钥匙\n   - 地图"
    )
    assert (
        replace_variable_macros("{{format_message_variable_quoted::stat_data.items}}", source)
        == '- "钥匙"\n- "地图"'
    )
    text = replace_variable_macros("{{format_message_variable_quoted::stat_data}}", source)
    assert yaml.safe_load(text) == {
        key: value for key, value in source["stat_data"].items() if not key.startswith("$")
    }
    assert "hp:" in text and '"hp"' not in text
    assert (
        replace_variable_macros(
            "{{get_message_variable::stat_data.$internal}}|{{get_message_variable::initialized_lorebooks}}|{{get_message_variable::}}",
            source,
        )
        == "null|null|null"
    )
    assert replace_variable_macros("{{unknown}}", source) == "{{unknown}}"


@pytest.mark.parametrize("mode", ["half_agent", "full_agent"])
def test_final_compiled_messages_expand_macros_without_ledger_or_hash_leaks(mode):
    value = state()
    template = "生命={{get_message_variable::stat_data.hp.0}}；全局={{format_global_variable::score}}；账本={{get_message_variable::initialized_lorebooks}}；内部={{get_message_variable::stat_data.$internal}}"
    document = {"name": "变量卡", "description": template, "first_mes": "开场。"}
    session = {"id": "story", "persona": "用户", "recipe": ContextRecipe().model_dump()}
    snapshot = {
        "state": value,
        "scopes": {"global": {"score": 3}},
        "lore": {},
        "memory_document": {"version": 3, "records": []},
    }
    history = [
        {
            "id": "opening",
            "role": "assistant",
            "raw": "开场。",
            "model": "开场。",
            "snapshot": copy.deepcopy(snapshot),
        }
    ]
    cfg = Settings()
    if mode == "half_agent":
        result = compile_context(document, session, history, snapshot, "继续", cfg, "turn")
    else:
        result = compile_window(document, session, history, snapshot, "继续", cfg, [])
    wire = json.dumps(result["messages"], ensure_ascii=False)
    assert "生命=45；全局=3；账本=null；内部=null" in wire
    assert OPAQUE not in wire and "初始化账本" not in wire
    assert "{{get_message_variable" not in wire and "{{format_global_variable" not in wire
    assert value == state()
    assert macros("{{char}}/{{user}}", document, "玩家", value) == "变量卡/玩家"
