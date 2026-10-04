import copy
import json
from pathlib import Path

from tavern.scripting.runtime import ScriptRuntime


async def test_selected_opening_initializes_state_and_preserves_it_on_reopen():
    runtime = ScriptRuntime()
    document = {'name': 'fixture', 'character_book': {'entries': [
        {'id': 1, 'comment': '[initvar]',
         'content': 'location: default\nhp: 500\nextra: true\nabilities: {sense: true}\nitems: [a, b]',
         'enabled': False},
    ]}}
    snapshot = {'state': {}, 'scopes': {}}
    history = [{'id': 'greeting', 'role': 'assistant',
                'raw': '<initvar>location: alternate\nhp: 100\nname: "{{user}}"\nabilities: {magic: true}\nitems: []</initvar>',
                'snapshot': snapshot}]

    async def host(*args):
        return None

    session = None
    try:
        session = await runtime.open(document, history, snapshot, False, host, persona='测试玩家', opening=True)
        mirror = await session.call('snapshot')
        expected = {'location': 'alternate', 'hp': 100, 'name': '测试玩家',
                    'abilities': {'magic': True}, 'items': []}
        assert mirror['state']['stat_data'] == expected
        await session.close()
        snapshot['state'] = mirror['state']
        snapshot['state']['stat_data']['hp'] = 75
        document['character_book']['entries'].append(
            {'id': 2, 'comment': '[initvar]', 'content': 'new_default: true', 'enabled': True}
        )
        session = await runtime.open(document, history, snapshot, False, host)
        assert (await session.call('snapshot'))['state']['stat_data'] == {**expected, 'hp': 75}
    finally:
        if session:
            await session.close()
        await runtime.close()


async def test_disabled_initvar_entries_initialize_without_enabling_other_lore():
    runtime = ScriptRuntime()
    document = {'name': 'fixture', 'character_book': {'entries': [
        {'id': 'off', 'comment': '[InitVar]初始化', 'enabled': False,
         'content': 'hp: 100\nname: "{{user}}"\ncharacter: "{{char}}"'},
        {'id': 'legacy-off', 'comment': '[initvar]', 'disable': True,
         'content': '<initvar>\n```yaml\nmp: 50\nitems: []\n```\n</initvar>'},
        {'id': 'native', 'comment': '初始数据', 'enabled': True, 'content': 'native: true',
         'extensions': {'terrarium': {'audience': 'runtime_init'}}},
        {'id': 'native-off', 'comment': '初始数据', 'enabled': False, 'content': 'unexpected: true',
         'extensions': {'terrarium': {'audience': 'runtime_init'}}},
        {'id': 'native-legacy-off', 'comment': '初始数据', 'enabled': True, 'disable': True,
         'content': 'unexpected: true', 'extensions': {'terrarium': {'audience': 'runtime_init'}}},
        {'id': 'story', 'comment': '普通设定', 'enabled': False, 'content': 'unexpected: true'},
        {'id': 'rule', 'comment': '[initvar]', 'enabled': True, 'content': 'unexpected: true',
         'extensions': {'terrarium': {'audience': 'tracker_mvu'}}},
    ]}}

    async def host(*args):
        return None

    session = None
    try:
        session = await runtime.open(document, [], {'state': {}, 'scopes': {}}, False, host, persona='测试玩家')
        state = (await session.call('snapshot'))['state']
        assert state['stat_data'] == {'hp': 100, 'name': '测试玩家', 'character': 'fixture',
                                      'mp': 50, 'items': [], 'native': True}
        assert set(state['initialized_lorebooks']) == {'card:off', 'card:legacy-off', 'card:native'}
    finally:
        if session:
            await session.close()
        await runtime.close()


async def test_opening_initialization_callback_sees_replacement_and_runs_once():
    runtime = ScriptRuntime()
    document = {'name': 'fixture', 'character_book': {'entries': [
        {'id': 'default', 'comment': '[initvar]', 'content': 'hp: 500\nextra: true', 'enabled': False},
    ]}, 'extensions': {'tavern_helper': {'scripts': [
        {'id': 'init', 'enabled': True, 'content': """
            eventOn(Mvu.events.VARIABLE_INITIALIZED, data => {
                if ('extra' in data.stat_data) throw Error('worldbook defaults leaked');
                data.stat_data.callback_count = (data.stat_data.callback_count || 0) + 1;
            });
        """},
    ]}}}
    snapshot = {'state': {}, 'scopes': {}}
    history = [{'id': 'greeting', 'role': 'assistant', 'raw': '<initvar>hp: 100</initvar>',
                'snapshot': snapshot}]

    async def host(*args):
        return None

    session = None
    try:
        session = await runtime.open(document, history, snapshot, True, host, opening=True)
        snapshot['state'] = (await session.call('snapshot'))['state']
        assert snapshot['state']['stat_data'] == {'hp': 100, 'callback_count': 1}
        await session.close()
        snapshot['state']['stat_data']['hp'] = 75
        session = await runtime.open(document, history, snapshot, True, host, opening=True)
        assert (await session.call('snapshot'))['state']['stat_data'] == {'hp': 75, 'callback_count': 1}
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
