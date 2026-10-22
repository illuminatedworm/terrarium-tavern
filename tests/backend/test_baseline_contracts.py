"""First-release storage and model protocol contracts."""

import hashlib
import sqlite3

import pytest

from tavern.core.models import PersonaEdit, SessionCreate, Settings
from tavern.memories.memory_protocol import References
from tavern.memories.memory_records import read_memory
from tavern.storage.store import Store


def test_fresh_store_reopens_without_rewriting_settings(tmp_path):
    store = Store(tmp_path)
    cfg = Settings(memory_timeout=60, tracker_system_prompt="自定义原则")
    store.save_settings(cfg)
    card = store.import_card(b'{"name":"fixture","first_mes":"hello"}', "card.json")
    session = store.new_session(SessionCreate(card_id=card["id"]))
    assert session["persona"] == "用户"
    assert read_memory(session["snapshot"]) == {"version": 3, "records": []}
    reopened = Store(tmp_path)
    assert reopened.settings_document() == store.settings_document()
    assert reopened.settings().tracker_system_prompt == cfg.tracker_system_prompt
    assert reopened.session(session["id"]) == store.session(session["id"])
    with reopened.connection() as db:
        assert db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == "11"
        assert dict((r[1], r[3]) for r in db.execute("PRAGMA table_info(sessions)"))["card_data"] == 1


@pytest.mark.parametrize("mode", ["half_agent", "full_agent"])
def test_saved_persona_names_survive_reopen_and_fork(tmp_path, mode):
    name = "旅人"
    store = Store(tmp_path)
    card = store.import_card(b'{"name":"fixture","first_mes":"hello {{user}}"}', "card.json")
    saved = store.save_persona(None, PersonaEdit(name=name, description="保留的用户介绍"))
    explicit = store.new_session(SessionCreate(card_id=card["id"], agent_mode=mode, persona=name))
    selected = store.new_session(SessionCreate(card_id=card["id"], agent_mode=mode, persona_id=saved["id"]))

    reopened = Store(tmp_path)
    assert reopened.persona(saved["id"]) == saved
    for original in (explicit, selected):
        session = reopened.session(original["id"])
        assert session["persona"] == name
        assert session["persona_data"] == original["persona_data"]
        fork = reopened.fork_session(session["id"], session["head"], session["revision"])
        assert fork["persona"] == name and fork["persona_data"] == session["persona_data"]
    fresh = reopened.new_session(SessionCreate(card_id=card["id"], agent_mode=mode))
    assert fresh["persona"] == "用户" and fresh["persona_data"] == {}


@pytest.mark.parametrize("version", [None, "0", "999", "invalid"])
def test_unsupported_database_is_rejected_before_any_write(tmp_path, version):
    path = tmp_path / "tavern.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
        if version is not None:
            db.execute("INSERT INTO meta VALUES ('schema_version',?)", (version,))
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(ValueError, match="版本"):
        Store(tmp_path)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["tavern.sqlite3"]


def test_source_protocol_accepts_only_exposed_short_references():
    message = {"id": "private-message-id", "role": "assistant", "raw": "hello"}
    refs = References([message])
    refs.message(message)
    with pytest.raises(ValueError, match="短编号"):
        refs.resolve(
            {
                "operations": [
                    {
                        "op": "add",
                        "record": {"sources": [{"message_id": "private-message-id", "excerpt": "hello"}]},
                    }
                ]
            }
        )


def test_memory_document_version_is_validated():
    # Version 1 predates per-message archive deltas and is not migrated.
    with pytest.raises(ValueError, match="版本 1 不受支持.*新建对话"):
        read_memory({"memory_document": {"version": 1, "records": []}})
