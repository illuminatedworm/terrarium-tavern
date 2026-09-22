import copy
import json
from pathlib import Path

from tavern.scripting.runtime import ScriptRuntime


async def test_selected_opening_initializes_state_and_preserves_it_on_reopen():
    runtime = ScriptRuntime()
    document = {'name': 'fixture', 'character_book': {'entries': [
        {'id': 1, 'comment': '[initvar]', 'content': 'location: default\nhp: 500', 'enabled': True},
    ]}}
    snapshot = {'state': {}, 'scopes': {}}
    history = [{'id': 'greeting', 'role': 'assistant',
                'raw': '<initvar>location: alternate\nhp: 100\nname: "{{user}}"</initvar>',
                'snapshot': snapshot}]

    async def host(*args):
        return None

    session = None
    try:
        session = await runtime.open(document, history, snapshot, False, host, persona='测试玩家', opening=True)
        mirror = await session.call('snapshot')
        assert mirror['state']['stat_data'] == {'location': 'alternate', 'hp': 100, 'name': '测试玩家'}
        await session.close()
        snapshot['state'] = mirror['state']
        snapshot['state']['stat_data']['hp'] = 75
        session = await runtime.open(document, history, snapshot, False, host)
        assert (await session.call('snapshot'))['state']['stat_data']['hp'] == 75
    finally:
        if session:
            await session.close()
        await runtime.close()


async def test_message_closure_callback_and_isolation():
    runtime = ScriptRuntime()
    document = json.loads(Path("samples/星航状态卡.json").read_text(encoding="utf-8"))
    snapshot = {"state": {}, "scopes": {}}
    html = """<div id='limit'>2</div><script>
    const floorLimit=Number(document.getElementById('limit').textContent);
    eventOn(Mvu.events.VARIABLE_UPDATE_ENDED,data=>{data.stat_data.能量=Math.max(floorLimit,data.stat_data.能量)});
    </script>"""
    history = [{"id": "greeting", "role": "assistant", "raw": html, "display": html, "snapshot": snapshot}]

    async def host(method, args):
        if method == "log":
            return None
        raise ValueError("no external actions in test")

    session = None
    try:
        session = await runtime.open(document, history, snapshot, True, host)
        assert (await session.call("process", "_.add('能量',-10);"))["state"]["stat_data"]["能量"] == 2
        assert await session.page.evaluate("typeof process") == "undefined"
        result = await session.page.evaluate(
            "async()=>{try{await fetch('http://127.0.0.1:8000/api/settings');return 'leak'}catch{return 'blocked'}}"
        )
        assert result == "blocked"
    finally:
        if session:
            await session.close()
        await runtime.close()


async def test_upstream_differential_for_common_operations():
    runtime = ScriptRuntime()
    doc = {"name": "fixture"}

    async def host(method, args):
        return None

    session = None
    try:
        session = await runtime.open(
            doc, [], {"state": {"stat_data": {"hp": 5, "items": ["a"]}}, "scopes": {}}, True, host
        )
        cases = ["_.add('hp', 2);", "_.set('hp', 8);", "_.insert('items','b');"]
        for text in cases:
            initial = {
                "stat_data": {"hp": 5, "items": ["a"]},
                "initialized_lorebooks": {},
                "schema": {
                    "type": "object",
                    "extensible": True,
                    "properties": {
                        "hp": {"type": "number"},
                        "items": {"type": "array", "extensible": True, "elementType": {"type": "string"}},
                    },
                },
            }
            actual = await session.page.evaluate(
                "async ({text,data})=>{await MVUReference.updateVariables(text,data);return data.stat_data}",
                {"text": text, "data": copy.deepcopy(initial)},
            )
            await session.call("update", {"state": initial, "messages": [], "scopes": {}, "serverMain": True})
            adapted = await session.call("process", text)
            assert adapted["state"]["stat_data"] == actual
    finally:
        if session:
            await session.close()
        await runtime.close()
