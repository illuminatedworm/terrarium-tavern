"""First-release storage and model protocol contracts."""

import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from copy import deepcopy
from threading import Barrier

import pytest

from tavern.core.models import PersonaEdit, SessionCreate, Settings, TurnRequest
from tavern.storage import schema
from tavern.storage.legacy_story import OLD_STORY_KEY, RETIRED_STORY_KEY
from tavern.storage.store import SCHEMA_VERSION, Store, dump


def test_fresh_store_reopens_without_rewriting_settings(tmp_path):
    store = Store(tmp_path)
    cfg = Settings(story_system_prompt="自定义原则")
    store.save_settings(cfg)
    card = store.import_card(b'{"name":"fixture","first_mes":"hello"}')
    session = store.new_session(SessionCreate(card_id=card["id"]))
    assert session["persona"] == "用户"
    assert session["snapshot"]["story"]["window"]["number"] == 1
    reopened = Store(tmp_path)
    assert reopened.settings_document() == store.settings_document()
    assert reopened.settings().story_system_prompt == cfg.story_system_prompt
    assert reopened.session(session["id"]) == store.session(session["id"])
    with reopened.connection() as db:
        assert db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == str(
            SCHEMA_VERSION
        )
        assert dict((r[1], r[3]) for r in db.execute("PRAGMA table_info(sessions)"))["card_data"] == 1


def test_saved_persona_names_survive_reopen_and_fork(tmp_path):
    name = "旅人"
    store = Store(tmp_path)
    card = store.import_card(b'{"name":"fixture","first_mes":"hello {{user}}"}')
    saved = store.save_persona(None, PersonaEdit(name=name, description="保留的用户介绍"))
    explicit = store.new_session(SessionCreate(card_id=card["id"], persona=name))
    selected = store.new_session(SessionCreate(card_id=card["id"], persona_id=saved["id"]))

    reopened = Store(tmp_path)
    assert reopened.persona(saved["id"]) == saved
    for original in (explicit, selected):
        session = reopened.session(original["id"])
        assert session["persona"] == name
        assert session["persona_data"] == original["persona_data"]
        fork = reopened.fork_session(session["id"], session["head"], session["revision"])
        assert fork["persona"] == name and fork["persona_data"] == session["persona_data"]
    fresh = reopened.new_session(SessionCreate(card_id=card["id"]))
    assert fresh["persona"] == "用户" and fresh["persona_data"] == {}


def test_concurrent_database_upgrade_backs_up_and_preserves_story_progress(tmp_path, monkeypatch):
    from tavern.character.card_author_store import EditorStore

    store = Store(tmp_path)
    card = store.import_card(b'{"name":"fixture","first_mes":"hello"}')
    kept = store.new_session(SessionCreate(card_id=card["id"]))
    retired = store.new_session(SessionCreate(card_id=card["id"]))
    editor = EditorStore(store)
    draft = editor.create_conversation()
    draft = editor.attach(draft["id"], card["id"], draft["revision"])
    item = draft["cards"][0]
    old_settings = store.settings_document().model_dump()
    old_settings.update(version=20)
    old_settings[RETIRED_STORY_KEY] = {"foreground": {"model": "old"}}
    old_settings[OLD_STORY_KEY] = old_settings.pop("story")
    old_settings["revisions"] = {OLD_STORY_KEY: 3, RETIRED_STORY_KEY: 2, "shared": 0}
    old_settings["shared"].pop("debug_story_prefix")
    old_settings["shared"][f"debug_{OLD_STORY_KEY}_prefix"] = True
    old_settings["shared"]["send_shortcut"] = "shift_enter"
    kept_turn, _ = store.create_turn(kept["id"], TurnRequest(operation_id="keep", expected_revision=0))
    old_turn, _ = store.create_turn(retired["id"], TurnRequest(operation_id="drop", expected_revision=0))
    store.set_turn(kept_turn, status="interrupted")
    store.set_turn(old_turn, status="awaiting_review")
    store.event(old_turn, "legacy_tracker_event", {})
    snapshot = deepcopy(kept["snapshot"])
    snapshot["state"]["stat_data"] = {OLD_STORY_KEY: "用户原有变量"}
    legacy_snapshot = {**snapshot, OLD_STORY_KEY: snapshot["story"]}
    legacy_snapshot.pop("story")
    with store.connection() as db:
        db.execute(f"ALTER TABLE sessions ADD COLUMN agent_mode TEXT NOT NULL DEFAULT '{OLD_STORY_KEY}'")
        db.execute("ALTER TABLE sessions ADD COLUMN recipe TEXT NOT NULL DEFAULT '{}'")
        db.execute("ALTER TABLE sessions ADD COLUMN lore_injection_mode TEXT NOT NULL DEFAULT 'conversation'")
        db.execute("UPDATE sessions SET agent_mode=? WHERE id=?", (RETIRED_STORY_KEY, retired["id"]))
        db.execute("UPDATE messages SET snapshot='{}' WHERE session=?", (retired["id"],))
        db.execute("UPDATE messages SET snapshot=? WHERE id=?", (dump(legacy_snapshot), kept["head"]))
        db.execute("INSERT INTO advisor_conversations(session) VALUES (?)", (retired["id"],))
        db.execute(
            "UPDATE card_author_conversations SET payload=json_set(payload,'$.cards[0].target_mode',?) "
            "WHERE id=?",
            (RETIRED_STORY_KEY, draft["id"]),
        )
        editor.version(db, draft["id"], {**item, "target_mode": RETIRED_STORY_KEY}, "旧草稿版本")
        db.execute(f"CREATE TABLE {RETIRED_STORY_KEY}_trace(message_id TEXT PRIMARY KEY)")
        db.execute(f"INSERT INTO {RETIRED_STORY_KEY}_trace VALUES (?)", (retired["head"],))
        for suffix in ("trace", "progress"):
            db.execute(f"ALTER TABLE story_{suffix} RENAME TO {OLD_STORY_KEY}_{suffix}")
        db.execute("UPDATE meta SET value='25' WHERE key='schema_version'")
        db.execute("UPDATE meta SET value=? WHERE key='settings'", (dump(old_settings),))
    initialized = Barrier(2, timeout=5)
    initialize_features = schema._initialize_feature_tables

    def align_upgrades(store):
        initialize_features(store)
        initialized.wait()

    with monkeypatch.context() as patch, ThreadPoolExecutor(max_workers=2) as pool:
        patch.setattr(schema, "_initialize_feature_tables", align_upgrades)
        upgraded, _ = pool.map(Store, (tmp_path, tmp_path))
    assert [row["id"] for row in upgraded.sessions()] == [kept["id"]]
    assert upgraded.session(kept["id"])["snapshot"] == snapshot
    assert upgraded.turn(kept_turn)["status"] == "interrupted"
    assert upgraded.card(card["id"])["document"] == card["document"]
    assert upgraded.settings_document().shared.send_shortcut == "shift_enter"
    assert upgraded.settings_document().shared.debug_story_prefix
    assert upgraded.settings_document().revisions == {"story": 3, "shared": 0}
    kept_editor = EditorStore(upgraded)
    reopened_draft = kept_editor.conversation(draft["id"])
    assert reopened_draft["cards"][0] == {**item, "can_undo": True}
    undone = kept_editor.undo_card(draft["id"], item["ref"], reopened_draft["revision"])
    assert undone["cards"][0] == item
    with upgraded.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM events WHERE turn=?", (old_turn,)).fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM advisor_conversations").fetchone()[0] == 0
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert f"{RETIRED_STORY_KEY}_trace" not in tables
        assert {"story_trace", "story_progress"} <= tables
        assert not db.execute("PRAGMA foreign_key_check").fetchall()
    backups = list(tmp_path.glob("before-upgrade-schema25-*.sqlite3"))
    assert backups
    for path in backups:
        with closing(sqlite3.connect(path)) as backup:
            assert backup.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 2
    Store(tmp_path)
    assert list(tmp_path.glob("before-upgrade-schema25-*.sqlite3")) == backups


def test_lore_purpose_upgrade_preserves_saved_versions_and_undo(tmp_path):
    from tavern.character.card_author_store import EditorStore
    from tavern.character.cards import lore_entries, variable_rule_entries

    legacy = {
        "name": "fixture",
        "first_mes": "hello",
        "character_book": {
            "entries": [
                {
                    "id": "rule",
                    "comment": "[InitVar]",
                    "enabled": False,
                    "content": 'hp: [3, "旧标记 tracker_mvu 原文"]',
                    "terrarium": {"audience": "tracker_mvu", "author_note": "keep"},
                },
                {
                    "id": "lore",
                    "content": "叙事设定",
                    "terrarium": {"audience": "tracker_mvu"},
                    "extensions": {"terrarium": {"audience": "writer"}},
                },
            ]
        },
    }
    store = Store(tmp_path)
    card = store.import_card(dump(legacy).encode())
    session = store.new_session(SessionCreate(card_id=card["id"]))
    editor = EditorStore(store)
    draft = editor.attach(editor.create_conversation()["id"], card["id"], 0)
    canonical = card["document"]
    assert canonical["character_book"]["entries"][0]["terrarium"] == {"author_note": "keep"}
    assert [e["id"] for e in variable_rule_entries(canonical)] == ["rule"]
    assert [e["id"] for e in lore_entries(canonical, "narrative")] == ["lore"]

    snapshot = {**session["snapshot"], "content": {"document": legacy}}
    tid, _ = store.create_turn(session["id"], TurnRequest(operation_id="resume", expected_revision=0))
    with store.connection() as db:
        db.execute("UPDATE cards SET document=? WHERE id=?", (dump(legacy), card["id"]))
        db.execute(
            "UPDATE sessions SET card_data=json_set(card_data,'$.document',json(?)) WHERE id=?",
            (dump(legacy), session["id"]),
        )
        db.execute("UPDATE messages SET snapshot=? WHERE id=?", (dump(snapshot), session["head"]))
        progress = {
            "version": 7,
            "output_id": "reply",
            "story_format": "plain",
            "snapshot": snapshot,
            "user": {"snapshot": snapshot},
            "document": legacy,
            "script_mirror": {"lore": lore_entries(legacy)},
        }
        db.execute("INSERT INTO story_progress VALUES (?,?)", (tid, dump(progress)))
        old = {
            **draft["cards"][0],
            "document": legacy,
            "base_document": legacy,
            "source": {**draft["cards"][0]["source"], "document": legacy},
        }
        editor.version(db, draft["id"], old, "旧草稿")
        db.execute(
            "UPDATE card_author_conversations SET payload=? WHERE id=?",
            (dump({"cards": [old], "state": draft["state"]}), draft["id"]),
        )
        db.execute("UPDATE meta SET value='27' WHERE key='schema_version'")

    upgraded = Store(tmp_path)
    assert upgraded.card(card["id"])["document"] == canonical
    assert upgraded.session_card(session["id"])["document"] == canonical
    assert upgraded.session(session["id"])["snapshot"]["content"]["document"] == canonical
    recovered = upgraded.story_progress(tid)
    assert recovered["user"]["snapshot"]["content"]["document"] == recovered["document"] == canonical
    assert recovered["script_mirror"]["lore"] == lore_entries(canonical)
    reopened = EditorStore(upgraded)
    item = reopened.conversation(draft["id"])["cards"][0]
    assert item["base_document"] == item["source"]["document"] == canonical
    undone = reopened.undo_card(draft["id"], item["ref"], draft["revision"])
    assert undone["cards"][0]["document"] == canonical
    assert (
        canonical["character_book"]["entries"][0]["content"]
        == legacy["character_book"]["entries"][0]["content"]
    )
    [backup] = tmp_path.glob("before-upgrade-schema27-*.sqlite3")
    with closing(sqlite3.connect(backup)) as db:
        assert json.loads(db.execute("SELECT document FROM cards").fetchone()[0]) == legacy


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
