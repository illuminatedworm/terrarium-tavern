"""First-release storage and model protocol contracts."""
import hashlib
import sqlite3

import pytest

from tavern.core.models import SessionCreate, Settings
from tavern.memories.memory_protocol import References
from tavern.memories.memory_records import read_memory
from tavern.storage.store import Store


def test_fresh_store_reopens_without_rewriting_settings(tmp_path):
    store = Store(tmp_path)
    cfg = Settings(memory_timeout=60, memory_system_prompt='自定义原则')
    store.save_settings(cfg)
    card = store.import_card(b'{"name":"fixture","first_mes":"hello"}', 'card.json')
    session = store.new_session(SessionCreate(card_id=card['id']))
    assert read_memory(session['snapshot']) == {'version': 1, 'records': []}
    reopened = Store(tmp_path)
    assert reopened.settings().model_dump() == cfg.model_dump()
    assert reopened.session(session['id']) == store.session(session['id'])
    with reopened.connection() as db:
        assert db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == '1'
        assert dict((r[1], r[3]) for r in db.execute('PRAGMA table_info(sessions)'))['card_data'] == 1


@pytest.mark.parametrize('version', [None, '0', '2', 'invalid'])
def test_unsupported_database_is_rejected_before_any_write(tmp_path, version):
    path = tmp_path / 'tavern.sqlite3'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)')
        if version is not None:
            db.execute("INSERT INTO meta VALUES ('schema_version',?)", (version,))
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(ValueError, match='版本'):
        Store(tmp_path)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ['tavern.sqlite3']


def test_source_protocol_accepts_only_exposed_short_references():
    refs = References()
    refs.message({'id': 'private-message-id', 'role': 'assistant', 'raw': 'hello'})
    with pytest.raises(ValueError, match='短编号'):
        refs.resolve({'operations': [{'op': 'add', 'record': {
            'sources': [{'message_id': 'private-message-id', 'excerpt': 'hello'}]}}]})


def test_memory_document_version_is_validated():
    with pytest.raises(ValueError, match='格式'):
        read_memory({'memory_document': {'version': 2, 'records': []}})
