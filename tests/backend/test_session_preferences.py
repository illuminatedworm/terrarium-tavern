import sqlite3
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from tavern.application.api import create_app
from tavern.conversation.context import compile_context
from tavern.core.models import SessionCreate, Settings
from tavern.storage.store import Store


@pytest.fixture
def story(tmp_path):
    store = Store(tmp_path)
    card = store.import_card(Path('samples/雾港来信.json').read_bytes(), 'card.json')
    session = store.new_session(SessionCreate(card_id=card['id']))
    return store, session


def preview(store, session, **kwargs):
    return compile_context(store.card(session['card'])['document'], session,
                           session['messages'], session['snapshot'], '继续', Settings(), 'preferences', **kwargs)


def test_preference_is_optional_required_system_supplement_even_with_custom_order(story):
    store, session = story
    assert not any(m['source'] == 'session_preference' for m in preview(store, session)['trace'])
    changed = store.update_session(session['id'], 0, turn_preference='  节奏放缓，多用对话推进。  ')
    compiled = preview(store, changed, generate={'ordered_prompts': ['user_input']})
    assert [m['source'] for m in compiled['trace'][:3]] == [
        'rules', 'retrieval_protocol', 'session_preference']
    preference = next(m for m in compiled['trace'] if m['source'] == 'session_preference')
    assert preference['role'] == 'system' and preference['required']
    assert preference['content'].endswith('节奏放缓，多用对话推进。\n</tavern_preference>')
    assert store.settings().system_prompt == Settings().system_prompt
    other = store.new_session(SessionCreate(card_id=session['card']))
    assert other['turn_preference'] == ''
    cleared = store.update_session(session['id'], changed['revision'], turn_preference='')
    assert not any(m['source'] == 'session_preference' for m in preview(store, cleared)['trace'])


def test_preference_persists_and_is_inherited(story):
    store, session = story
    changed = store.update_session(session['id'], 0, turn_preference='保留悬念，给玩家留出行动空间。',
                                   update_variables=True)
    reopened = Store(store.root).session(session['id'])
    assert reopened['turn_preference'] == changed['turn_preference']
    assert reopened['update_variables'] is True
    fork = store.fork_session(session['id'], session['head'], changed['revision'])
    assert fork['turn_preference'] == changed['turn_preference']
    assert fork['update_variables'] is True
    store.update_session(fork['id'], fork['revision'], update_variables=False)
    assert store.session(session['id'])['update_variables'] is True


def test_variable_preference_migration_backs_up_existing_stories(story):
    store, session = story
    with store.connection() as db:
        db.execute('ALTER TABLE sessions DROP COLUMN update_variables')
    reopened = Store(store.root).session(session['id'])
    assert reopened['update_variables'] is False
    assert reopened['snapshot'] == session['snapshot']
    backups = list(store.root.glob('before-variable-preference-*.sqlite3'))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as db:
        assert 'update_variables' not in {row[1] for row in db.execute('PRAGMA table_info(sessions)')}
    Store(store.root)
    assert list(store.root.glob('before-variable-preference-*.sqlite3')) == backups


async def test_options_save_clear_and_partial_updates_preserve_preference(story):
    store, session = story
    app = create_app(store.root)
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://testserver') as client:
        url = f"/api/sessions/{session['id']}/options"
        saved = await client.put(url, json={'expected_revision': 0, 'turn_preference': '保持慢节奏。',
                                           'persona': '旅人', 'persona_data': {'description': '远道而来'},
                                           'policy': 'review'})
        assert saved.status_code == 200 and saved.json()['turn_preference'] == '保持慢节奏。'
        partial = await client.put(url, json={'expected_revision': 1, 'update_variables': True})
        assert partial.status_code == 200 and partial.json()['turn_preference'] == '保持慢节奏。'
        assert partial.json()['persona'] == '旅人'
        assert partial.json()['persona_data'] == {'description': '远道而来'}
        assert partial.json()['policy'] == 'review'
        assert partial.json()['update_variables'] is True
        opened = await client.get(f"/api/sessions/{session['id']}")
        assert opened.json()['update_variables'] is True
        other = store.new_session(SessionCreate(card_id=session['card']))
        assert other['update_variables'] is False
        cleared = await client.put(url, json={'expected_revision': 2, 'turn_preference': ''})
        assert cleared.status_code == 200 and cleared.json()['turn_preference'] == ''
        assert cleared.json()['update_variables'] is True
        disabled = await client.put(url, json={'expected_revision': 3, 'update_variables': False})
        assert disabled.status_code == 200 and disabled.json()['update_variables'] is False


async def test_messages_preview_exports_full_writer_array_without_committing(story):
    store, session = story
    app = create_app(store.root)
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://testserver') as client:
        path = f"/api/sessions/{session['id']}"
        context = (await client.post(path + '/preview', json={'text': '继续'})).json()
        response = await client.post(path + '/messages-preview', json={'text': '继续'})
        assert response.status_code == 200
        messages = response.json()
        assert messages == context['messages']
        assert all('MVU' not in message['content'] for message in messages)
        assert all(set(message) == {'role', 'content'} for message in messages)
    assert store.session(session['id'])['revision'] == session['revision']
