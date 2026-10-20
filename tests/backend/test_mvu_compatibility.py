"""Exercise the shipped SDK/core rather than a handwritten variable mock."""

import copy

import pytest
from playwright.async_api import Error as PlaywrightError

from tavern.character.editor_sandbox import test_card as probe_card
from tavern.scripting.runtime import ScriptRuntime
from tavern.storage.store import Store


@pytest.fixture
async def runtime():
    instance = ScriptRuntime()
    yield instance
    await instance.close()


async def host(*args):
    return None


async def test_helper_namespace_scope_precedence_and_array_assignment(runtime):
    session = await runtime.open(
        {"name": "变量卡"}, [], {"state": {}, "scopes": {}}, False, host, persona="玩家"
    )
    try:
        data = {
            "state": {"stat_data": {"hp": [9, "说明"]}, "items": ["a", "b"]},
            "scopes": {
                "global": {"stat_data": {"ghost": True}, "globalOnly": 1},
                "character": {"stat_data": {"old": True}},
                "script:card": {"local": 3},
            },
            "messages": [
                {"variables": {"stat_data": {"hp": [5, "说明"]}}},
                {"variables": {"stat_data": {"hp": [7, "说明"]}}},
            ],
            "currentMessage": 0,
            "persona": "玩家",
            "character": "变量卡",
        }
        await session.call("install", data, install=True)
        result = await session.page.evaluate("""() => ({
            all: TavernHelper.getAllVariables(),
            latest: getVariables({type:'message'}),
            old: getVariables({type:'message',message_id:0}),
            legacy: getChatMessages(0)[0].data,
            macro: substitudeMacros('{{char}}/{{user}}/{{format_message_variable::stat_data.hp.0}}')
        })""")
        assert result["all"] == {"stat_data": {"hp": [5, "说明"]}, "globalOnly": 1, "items": ["a", "b"]}
        assert result["latest"]["stat_data"]["hp"] == [7, "说明"]
        assert result["old"] == result["legacy"] == {"stat_data": {"hp": [5, "说明"]}}
        assert result["macro"] == "变量卡/玩家/7"
        assert await session.page.evaluate("() => insertOrAssignVariables({items:['c']}).items") == ["c"]
        assert await session.page.evaluate("() => insertVariables({items:['x','y']}).items") == ["c"]
        data.pop("currentMessage")
        await session.call("install", data, install=True)
        script_view = await session.page.evaluate("() => getAllVariables()")
        assert script_view["stat_data"] == {"hp": [9, "说明"]} and script_view["local"] == 3
    finally:
        await session.close()


async def test_described_values_and_real_arrays_survive_core_updates(runtime):
    original = {"stat_data": {"hp": [5, "生命值"], "items": ["key", "map"], "plain": 3}}
    snapshot = {"state": copy.deepcopy(original), "scopes": {}}
    session = await runtime.open({"name": "变量卡"}, [], snapshot, False, host)
    try:
        updated = await session.call("process", "_.add('hp', 2);", [])
        assert updated["state"]["stat_data"] == {"hp": [7, "生命值"], "items": ["key", "map"], "plain": 3}
        patched = await session.call("process", "", [{"op": "replace", "path": "/hp/0", "value": 8}], "")
        assert patched["state"]["stat_data"]["hp"] == [8, "生命值"]
        before = await session.call("snapshot")
        with pytest.raises(PlaywrightError, match="类型不匹配"):
            await session.call("process", "", [{"op": "replace", "path": "/hp/0", "value": "invalid"}], "")
        assert await session.call("snapshot") == before
        assert snapshot["state"] == original
    finally:
        await session.close()


async def test_script_code_examples_do_not_run_as_message_callbacks(runtime):
    real = '<div id="source">3</div><script>eventOn(Mvu.events.VARIABLE_UPDATE_ENDED, data => {data.stat_data.hp = Number(document.getElementById("source").textContent)});</script>'
    text = (
        "```javascript\nconst example = \"<script>throw Error('not executable')</script>\";\n```\n\n```html\n"
        + real
        + "\n```\n尾部正文。"
    )
    snapshot = {"state": {"stat_data": {"hp": 5}}, "scopes": {}}
    session = await runtime.open(
        {"name": "变量卡"},
        [{"id": "g", "role": "assistant", "raw": text, "display": text, "snapshot": snapshot}],
        snapshot,
        True,
        host,
    )
    try:
        assert len(session.frames) == 1
        result = await session.call("process", "", [{"op": "replace", "path": "/hp", "value": 8}], "")
        assert result["state"]["stat_data"]["hp"] == 3
    finally:
        await session.close()


async def test_editor_probes_plain_markdown_and_all_css_only_frames_with_real_macros(tmp_path):
    doc = {
        "name": "多种状态显示",
        "first_mes": "**生命：{{format_message_variable::stat_data.hp.0}}**\n\n"
        '```html\n<style>.status{color:cyan}</style><div id="first">{{get_message_variable::stat_data.hp.0}}</div>\n```\n\n'
        '完整的尾部正文。\n\n```html\n<div id="second">{{get_message_variable::stat_data.items}}</div>\n```',
        "character_book": {
            "entries": [
                {
                    "comment": "[InitVar]",
                    "enabled": False,
                    "content": 'hp: [45, "生命值"]\nitems: [钥匙, 地图]',
                }
            ]
        },
    }
    result = await probe_card(
        Store(tmp_path),
        {"ref": "C1", "document": doc},
        display_assertions=[
            {"sample_index": 0, "selector": "strong", "text": "生命：45", "after_text": "生命：50"},
            {"sample_index": 0, "selector": "#first", "text": "45", "after_text": "50"},
            {
                "sample_index": 0,
                "selector": "#second",
                "text": '["钥匙","地图"]',
                "after_text": '["钥匙","地图"]',
            },
        ],
        state_patch=[{"op": "replace", "path": "/hp/0", "value": 50}],
    )
    assert result["status"] == "passed", result
