import json

from tavern.core.models import SessionCreate, TurnRequest
from tavern.storage.retrieval_cleanup import prune_orphan_retrieval
from tavern.storage.store import Store


def test_delete_full_session_removes_owned_records_and_exclusive_indexes(tmp_path):
    store = Store(tmp_path)
    card = store.import_card(b'{"name":"fixture","first_mes":"opening"}', 'fixture.json')
    full = store.new_session(SessionCreate(card_id=card['id'], agent_mode='full_agent'))
    live = store.new_session(SessionCreate(card_id=card['id']))
    tid, _ = store.create_turn(full['id'], TurnRequest(operation_id='delete-test', expected_revision=0))
    store.set_turn(tid, status='cancelled')
    store.event(tid, 'diagnostic', {'test': True})
    with store.connection() as db:
        db.execute('INSERT INTO full_agent_trace VALUES (?,?,?,?,?)', (full['id'], full['head'], 0, 'window', '{}'))
        db.execute('CREATE TABLE retrieval_collections(session TEXT PRIMARY KEY, keys TEXT NOT NULL)')
        for table in ('retrieval_indexes', 'retrieval_vectors'):
            db.execute(f'CREATE TABLE {table}(key TEXT PRIMARY KEY,value TEXT NOT NULL)')
            for key in ('shared', 'deleted-only', 'live-only', 'being-built'):
                db.execute(f'INSERT INTO {table} VALUES (?,?)', (key, '{}'))
        db.execute('INSERT INTO retrieval_collections VALUES (?,?)', (full['id'], json.dumps(['shared', 'deleted-only'])))
        db.execute('INSERT INTO retrieval_collections VALUES (?,?)', (live['id'], json.dumps(['shared', 'live-only'])))
    store.delete_session(full['id'])
    assert store.session(live['id'])['messages'] == live['messages']
    with store.connection() as db:
        for table in ('messages', 'turns', 'full_agent_trace', 'retrieval_collections'):
            assert db.execute(f'SELECT count(*) FROM {table} WHERE session=?', (full['id'],)).fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM events WHERE turn=?', (tid,)).fetchone()[0] == 0
        for table in ('retrieval_indexes', 'retrieval_vectors'):
            assert {row[0] for row in db.execute(f'SELECT key FROM {table}')} == {'shared', 'live-only', 'being-built'}
        assert prune_orphan_retrieval(db) == {'collections': 0, 'indexes': 0, 'vectors': 0}


def test_repair_deleted_session_bindings_without_removing_current_story(tmp_path):
    store = Store(tmp_path)
    with store.connection() as db:
        db.execute('CREATE TABLE retrieval_collections(session TEXT PRIMARY KEY, keys TEXT NOT NULL)')
        db.execute('INSERT INTO retrieval_collections VALUES (?,?)', ('already-deleted', '["old-index"]'))
        db.execute('CREATE TABLE retrieval_indexes(key TEXT PRIMARY KEY,value TEXT NOT NULL)')
        db.execute('INSERT INTO retrieval_indexes VALUES (?,?)', ('old-index', '{}'))
        assert prune_orphan_retrieval(db) == {'collections': 1, 'indexes': 1, 'vectors': 0}
        assert db.execute('SELECT count(*) FROM retrieval_collections').fetchone()[0] == 0


async def test_late_index_preparation_cannot_restore_a_deleted_session_reference(tmp_path):
    from tavern.core.models import Settings
    from tavern.search.retrieval import RetrievalEngine

    store = Store(tmp_path)
    card = store.import_card(b'{"name":"fixture","first_mes":"opening"}', 'fixture.json')
    removed = store.new_session(SessionCreate(card_id=card['id']))
    store.delete_session(removed['id'])

    class Models:
        def revision(self, _settings):
            return 'fixture'

        def schedule_index(self, *args):
            pass

    engine = RetrievalEngine(store, Models())
    await engine.prepare({'name': 'fixture', 'character_book': {'entries': [
        {'id': 'tower', 'keys': ['tower'], 'content': 'tower setting', 'enabled': True}]}},
        [], '', {}, Settings(retrieval={'channels': ['embedding']}), removed, 'late-index', index_only=True)
    with store.connection() as db:
        assert db.execute('SELECT count(*) FROM retrieval_collections').fetchone()[0] == 0
