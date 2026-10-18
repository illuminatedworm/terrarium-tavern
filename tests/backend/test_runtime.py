import copy
import json
from pathlib import Path

import pytest
from kohakuterrarium.testing.llm import ScriptedLLM
from playwright.async_api import Error as PlaywrightError

from tavern.character.card_workflow import validation_report
from tavern.core.models import SessionCreate
from tavern.full_agent.context import compile_window
from tavern.scripting.runtime import ScriptRuntime
from tavern.scripting.variables import analyze_variables
from tavern.storage.store import Store, dump
from tavern.tracker.task import TrackerTask
from tests.support.tracker_model import TrackerModel


async def test_full_agent_patch_failure_restores_server_and_message_frame_mirrors():
    runtime = ScriptRuntime()
    document = {"name": "rollback", "extensions": {"tavern_helper": {"scripts": [
        {"id": "failure", "enabled": True, "content": """
            globalThis.patchRollbackListener = eventOn('MESSAGE_RECEIVED', () => {
                replaceVariables({dirty: true}, {type: 'character'});
                throw Error('rollback-test');
            });
        """}]}}}
    snapshot = {"state": {"stat_data": {"hp": 8}}, "scopes": {"character": {"original": True}}}
    html = """<script>
        eventOn(Mvu.events.VARIABLE_UPDATE_STARTED, () => {
            replaceVariables({frameDirty: true}, {type: 'character'});
        });
    </script>"""
    history = [{"id": "greeting", "role": "assistant", "raw": html, "snapshot": snapshot}]

    async def host(*args):
        return None

    session = None
    try:
        session = await runtime.open(document, history, snapshot, True, host)
        before = await session.call("snapshot")
        frame = session.frames[0]
        frame_before = await frame.evaluate("() => TavernRuntime.snapshot()")
        operations = [{"op": "replace", "path": "/hp", "value": 7}]
        with pytest.raises(PlaywrightError, match="rollback-test"):
            await session.call("process", "", operations, "")
        assert await session.call("snapshot") == before
        assert await frame.evaluate("() => TavernRuntime.snapshot()") == frame_before
        await session.page.evaluate("() => patchRollbackListener.stop()")
        result = await session.call("process", "", operations, "")
        assert result["state"]["stat_data"]["hp"] == 7
        assert result["scopes"] == before["scopes"]
        assert await frame.evaluate("() => TavernRuntime.snapshot().scopes.character") == {"frameDirty": True}
    finally:
        if session:
            await session.close()
        await runtime.close()


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


async def test_disabled_initvar_entries_initialize_without_enabling_other_lore(tmp_path):
    runtime = ScriptRuntime()
    tracker_id = '8ebd384f-9379-4ec9-8147-b32808d06bdb'
    document = {'name': 'fixture', 'first_mes': '开门。', 'character_book': {'entries': [
        {'id': 'off', 'comment': '[InitVar]初始化', 'enabled': False,
         'content': 'hp: 100\nname: "{{user}}"\ncharacter: "{{char}}"',
         'terrarium': {'audience': 'tracker_mvu'}},
        {'id': 'legacy-off', 'comment': '[initvar]', 'disable': True,
         'content': '<initvar>\n```yaml\nmp: 50\nitems: []\n```\n</initvar>'},
        {'id': 'story', 'comment': '普通设定', 'enabled': False, 'content': 'unexpected: true'},
        {'id': tracker_id, 'comment': '[initvar]', 'enabled': False,
         'content': 'tracked: [8, "仅在 {{user}} 实际收到款项后增加"]',
         'extensions': {'terrarium': {'audience': 'tracker_mvu'}}},
        {'id': 'disabled-rule', 'enabled': False, 'content': 'DISABLED_RULE',
         'extensions': {'terrarium': {'audience': 'tracker_mvu'}}},
    ]}}
    original = copy.deepcopy(document)
    store = Store(tmp_path)
    card = store.import_card(dump(document).encode(), 'fixture.json')
    story = store.new_session(SessionCreate(card_id=card['id']))
    settings = store.settings()

    async def host(*args):
        return None

    session = None
    try:
        session = await runtime.open(document, [], {'state': {}, 'scopes': {}}, False, host, persona='测试玩家')
        state = (await session.call('snapshot'))['state']
        assert state['stat_data'] == {'hp': 100, 'name': '测试玩家', 'character': 'fixture',
                                      'mp': 50, 'items': [],
                                      'tracked': [8, '仅在 测试玩家 实际收到款项后增加']}
        assert set(state['initialized_lorebooks']) == {'card:off', 'card:legacy-off', f'card:{tracker_id}'}
        # Both diagnostics must recognize disabled Tracker-marked initialization
        # and rules, even when a display requires the complete variable contract.
        display = {**document, 'first_mes': '<div>TIME: 08:20 MONEY: 12</div>'}
        assert not any('未发现' in warning or '忽略' in warning for warning in validation_report(display)['warnings'])
        snapshot = {**story['snapshot'], 'state': state}
        story['persona'] = '测试玩家'
        model = TrackerModel(('patch_state', {'operations': [], 'comment': '无变化'}), '完成')
        work = TrackerTask(store, 'fixture', settings, story, document, story['messages'], snapshot,
                           phase='post', variables=True, provider=model)
        try:
            await work.run()
        finally:
            await work.close()
        provider = ScriptedLLM(['[]'])
        assert await analyze_variables(store, 'fixture', settings, [], state, '继续', '客人进门。',
                                       lambda: provider, document=document, persona=story['persona']) == []
        ordinary = compile_window(document, story, story['messages'], snapshot, '继续', settings, [])
        assert not any(m['content'].startswith('<mvu_rules>') for m in ordinary['messages'])
        full = compile_window(document, story, story['messages'], snapshot, '继续', settings, [],
                              require_state_patch=True)
        for messages in (model.requests[0]['messages'], provider.last_messages, full['messages']):
            [rules] = [m['content'] for m in messages if m['content'].startswith('<mvu_rules>')]
            assert 'tracked: [8, "仅在 测试玩家 实际收到款项后增加"]' in rules
            assert 'hp: 100\nname: "测试玩家"\ncharacter: "fixture"' in rules
            assert all(text not in rules for text in ('DISABLED_RULE', 'unexpected', 'mp: 50'))
            assert tracker_id not in dump(messages)
        assert document == original
        assert store.card(card['id'])['document'] == original
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
