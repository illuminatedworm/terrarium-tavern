"""SQLite schema initialization and upgrades for the application database."""

import json
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path
from typing import TYPE_CHECKING

from tavern.core.settings import SettingsDocument, migrate_settings

if TYPE_CHECKING:
    from tavern.storage.store import Store


# Persistent database format; bump together with docs/guides/maintenance.md.
SCHEMA_VERSION = 23
SETTINGS_VERSIONS = range(2, 20)
# Columns added to sessions after schema 1, in introduction order. Missing ones are
# appended on open; fresh databases get the original CREATE TABLE plus these.
SESSION_COLUMNS = {
    "update_variables": "INTEGER NOT NULL DEFAULT 0",
    "plot_enabled": "INTEGER NOT NULL DEFAULT 0",
    "model_selection": "TEXT NOT NULL DEFAULT '{}'",
    "channel_origin": "TEXT",
    "context_strategy": "TEXT NOT NULL DEFAULT 'new_context'",
    "compact_auto": "INTEGER NOT NULL DEFAULT 1",
    "title": "TEXT NOT NULL DEFAULT ''",
    "disabled_tools": "TEXT NOT NULL DEFAULT '[]'",
    "tool_result_options": "TEXT NOT NULL DEFAULT '{}'",
    "cache_breakpoint_auto": "INTEGER NOT NULL DEFAULT 1",
    "cache_breakpoint_message": "TEXT",
    "cache_breakpoint_follow": "TEXT NOT NULL DEFAULT '{}'",
    # Existing stories remain Half Agent.
    "agent_mode": "TEXT NOT NULL DEFAULT 'half_agent'",
    # Existing conversations stay ordinary; Kemini is chosen only at creation.
    "kemini": "INTEGER NOT NULL DEFAULT 0",
    # Old stories have no durable first-request fact; never infer it from prunable logs.
    "opening_locked": "INTEGER NOT NULL DEFAULT 1",
}
MESSAGE_COLUMNS = {
    "auxiliary": "TEXT NOT NULL DEFAULT '[]'",
    "story_segments": 'TEXT NOT NULL DEFAULT \'{"raw":[],"display":[]}\'',
    # The user's out-of-character text, kept apart from the story text card scripts process.
    "ooc": "TEXT NOT NULL DEFAULT ''",
}


def add_columns(db: sqlite3.Connection, table: str, columns: dict[str, str]) -> set[str]:
    present = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
    missing = [name for name in columns if name not in present]
    for name in missing:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {columns[name]}")
    return set(missing)


def _inspect_existing(root: Path, path: Path) -> SettingsDocument | None:
    """Validate an existing database read-only and back it up once before any upgrade.

    Returns the migrated settings document to persist, or None when it is current.
    Unsupported files raise before anything is created or written.
    """
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        has_meta = db.execute("SELECT 1 FROM sqlite_master WHERE name='meta'").fetchone()
        row = db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone() if has_meta else None
        if row is None or not row[0].isdigit() or not 1 <= int(row[0]) <= SCHEMA_VERSION:
            raise ValueError(f"数据库版本不受支持；需要 schema 1–{SCHEMA_VERSION}")
        version = int(row[0])
        if version < 9 and db.execute("SELECT 1 FROM sessions LIMIT 1").fetchone():
            raise ValueError(
                "旧对话不支持整轮重试格式；数据库未修改。请先停止酒馆服务，"
                f'在项目根目录运行 .\\clear-conversations.cmd --data "{root.resolve()}"'
                "（Linux 使用 .venv/bin/python clear-conversations.py，并传入相同的 --data 参数）。"
                "脚本会完整备份后删除全部剧情对话，保留角色卡、用户角色、设置和制卡资料；完成后重新启动。"
                "如需继续旧剧情，请使用兼容旧版打开原数据。"
            )
        raw = json.loads(db.execute("SELECT value FROM meta WHERE key='settings'").fetchone()[0])
        # Schema 1 predates the settings document version field.
        if version > 1 and raw.get("version") not in SETTINGS_VERSIONS:
            raise ValueError("设置版本与数据库版本不一致")
        settings = migrate_settings(raw)
        migrated = settings if settings.model_dump() != raw else None
        session_columns = {r[1] for r in db.execute("PRAGMA table_info(sessions)")}
        if version < SCHEMA_VERSION or migrated is not None or set(SESSION_COLUMNS) - session_columns:
            name = f"before-upgrade-schema{version}-settings{raw.get('version', 1)}-{uuid.uuid4().hex[:12]}"
            with closing(sqlite3.connect(root / f"{name}.sqlite3")) as backup_db:
                db.backup(backup_db)
    return migrated


def initialize_database(store: "Store") -> None:
    # Validate and back up before creating directories, tables or journal files.
    migrated = _inspect_existing(store.root, store.path) if store.path.exists() else None
    (store.root / "blobs").mkdir(parents=True, exist_ok=True)
    previous_schema = _initialize_core_tables(store, migrated)
    with store.connection() as db:
        _migrate_card_author_storage(db)
    _initialize_feature_tables(store)

    # An interrupted upgrade reruns the idempotent steps above before advancing the version.
    with store.connection() as db:
        if previous_schema is not None:
            _migrate_data(db, previous_schema)
        db.execute("INSERT OR REPLACE INTO meta VALUES ('schema_version',?)", (str(SCHEMA_VERSION),))


def _initialize_core_tables(store: "Store", migrated: SettingsDocument | None) -> int | None:
    with store.connection() as db:
        db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS context_token_samples(
                owner TEXT NOT NULL, lane TEXT NOT NULL, sequence INTEGER NOT NULL,
                payload TEXT NOT NULL, PRIMARY KEY(owner,lane));
            CREATE TABLE IF NOT EXISTS cards(id TEXT PRIMARY KEY,
                name TEXT NOT NULL, original TEXT NOT NULL, container TEXT NOT NULL,
                resources TEXT NOT NULL, permission INTEGER NOT NULL DEFAULT 0,
                document TEXT NOT NULL, report TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY, card TEXT NOT NULL,
                head TEXT, revision INTEGER NOT NULL DEFAULT 0,
                persona TEXT NOT NULL, policy TEXT, recipe TEXT NOT NULL, created REAL NOT NULL,
                title TEXT NOT NULL DEFAULT '',
                cache_breakpoint_auto INTEGER NOT NULL DEFAULT 1,
                cache_breakpoint_message TEXT,
                persona_data TEXT NOT NULL DEFAULT '{}', turn_preference TEXT NOT NULL DEFAULT '',
                card_data TEXT NOT NULL, lore_injection_mode TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS messages(id TEXT PRIMARY KEY, session TEXT NOT NULL,
                parent TEXT, role TEXT NOT NULL, raw TEXT NOT NULL, model TEXT NOT NULL,
                display TEXT NOT NULL, snapshot TEXT NOT NULL, created REAL NOT NULL,
                retrieval_query TEXT NOT NULL DEFAULT '');
            CREATE INDEX IF NOT EXISTS messages_session ON messages(session);
            CREATE TABLE IF NOT EXISTS turns(id TEXT PRIMARY KEY, session TEXT NOT NULL,
                operation TEXT NOT NULL, request TEXT NOT NULL, status TEXT NOT NULL,
                draft TEXT NOT NULL DEFAULT '', error TEXT, preview TEXT, pending TEXT,
                created REAL NOT NULL, UNIQUE(session,operation));
            CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT,
                turn TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL, created REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS events_turn ON events(turn,seq);
            CREATE TABLE IF NOT EXISTS changes(id TEXT PRIMARY KEY, turn TEXT NOT NULL,
                message TEXT NOT NULL, before_state TEXT NOT NULL, after_state TEXT NOT NULL,
                reason TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS personas(id TEXT PRIMARY KEY, version INTEGER NOT NULL,
                name TEXT NOT NULL, description TEXT NOT NULL);
        """)
        db.execute(
            "INSERT OR IGNORE INTO meta VALUES ('settings',?)", (SettingsDocument().model_dump_json(),)
        )
        version_row = db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        previous_schema = int(version_row[0]) if version_row is not None else None
        if previous_schema is not None and previous_schema < 16:
            # Schema 15 samples calibrated local estimates. Their opaque
            # identities cannot be backfilled into provider observations.
            # _inspect_existing has already made the pre-upgrade backup.
            db.execute("DELETE FROM context_token_samples")
        if migrated is not None:
            db.execute("UPDATE meta SET value=? WHERE key='settings'", (migrated.model_dump_json(),))
        db.execute("""CREATE TABLE IF NOT EXISTS context_operations (
            id TEXT PRIMARY KEY, session TEXT NOT NULL, operation TEXT NOT NULL,
            kind TEXT NOT NULL, status TEXT NOT NULL, expected_revision INTEGER NOT NULL,
            payload TEXT NOT NULL DEFAULT '{}', error TEXT, created REAL NOT NULL,
            UNIQUE(session, operation))""")
        added = add_columns(db, "sessions", SESSION_COLUMNS)
        add_columns(db, "messages", MESSAGE_COLUMNS)
        add_columns(db, "turns", {"draft_breaks": "TEXT NOT NULL DEFAULT '[]'"})
        if "title" in added:
            db.execute("""UPDATE sessions SET title=COALESCE(
                (SELECT name FROM cards WHERE cards.id=sessions.card),
                json_extract(card_data,'$.name'), '对话')""")
        if "cache_breakpoint_follow" in added:
            for session_row in db.execute("SELECT id,head FROM sessions").fetchall():
                store._update_cache_follow(db, session_row["id"], session_row["head"], reset=True)
        db.execute("""CREATE TABLE IF NOT EXISTS full_agent_trace (
            session TEXT NOT NULL, message_id TEXT NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
            seq INTEGER NOT NULL, window_id TEXT NOT NULL, payload TEXT NOT NULL,
            PRIMARY KEY(message_id, seq))""")
        db.execute("CREATE INDEX IF NOT EXISTS full_trace_session ON full_agent_trace(session)")
        db.execute("""CREATE TABLE IF NOT EXISTS half_agent_trace (
            session TEXT NOT NULL, message_id TEXT PRIMARY KEY REFERENCES messages(id) ON DELETE CASCADE,
            seq INTEGER NOT NULL, window_id TEXT NOT NULL, payload TEXT NOT NULL)""")
        db.execute("CREATE INDEX IF NOT EXISTS half_trace_session ON half_agent_trace(session)")
        db.execute("""CREATE TABLE IF NOT EXISTS full_agent_progress (
            turn TEXT PRIMARY KEY REFERENCES turns(id) ON DELETE CASCADE,
            payload TEXT NOT NULL)""")
        db.execute("""CREATE TABLE IF NOT EXISTS sent_cache_breakpoints (
            owner TEXT NOT NULL, scope TEXT NOT NULL, identity TEXT NOT NULL,
            prefix TEXT NOT NULL, value TEXT NOT NULL,
            PRIMARY KEY(owner, scope, identity, prefix))""")
    return previous_schema


def _migrate_card_author_storage(db: sqlite3.Connection) -> None:
    # Schema 6 renamed Card Editor storage to Card Author. Renaming in place keeps
    # rows, triggers and conversation identities; stored lane names follow.
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for suffix in ("conversations", "progress", "messages", "traces"):
        if f"card_editor_{suffix}" in tables:
            db.execute(f"ALTER TABLE card_editor_{suffix} RENAME TO card_author_{suffix}")
    db.execute("UPDATE sent_cache_breakpoints SET scope='card_author' WHERE scope='card_editor'")
    if "message_adapter_configs" in tables:
        for row in db.execute(
            "SELECT owner_id,config FROM message_adapter_configs WHERE owner_kind='editor'"
        ).fetchall():
            config = json.loads(row["config"])
            if "card_editor" in config:
                config["card_author"] = config.pop("card_editor")
                db.execute(
                    "UPDATE message_adapter_configs SET config=? WHERE owner_kind='editor' AND owner_id=?",
                    (json.dumps(config, ensure_ascii=False), row["owner_id"]),
                )


def _initialize_feature_tables(store: "Store") -> None:
    # These modules import Store helpers, so load them only after Store is defined.
    from tavern.advisor.storage import initialize as initialize_advisor
    from tavern.character.card_author_store import initialize_editor_tables
    from tavern.novel.storage import initialize as initialize_novel
    from tavern.storage.worldbooks import initialize as initialize_worldbooks

    initialize_editor_tables(store)
    initialize_advisor(store)
    initialize_novel(store)
    initialize_worldbooks(store)
    store.initialize_message_adapters()


def _migrate_data(db: sqlite3.Connection, previous_schema: int) -> None:
    if previous_schema < 16:
        from tavern.core.context_usage import retire_estimated_pressure

        retire_estimated_pressure(db)
    if previous_schema < 18:
        # Old investigations retain their explicit manual lifetime. The
        # format bump keeps older runtimes from ignoring the new policy.
        db.execute(
            "UPDATE novel_jobs SET config=json_set(config,'$.auto_destroy',json('false')) "
            "WHERE json_type(config,'$.auto_destroy') IS NULL"
        )
    if previous_schema < 20:
        # Opt-in capability: preserve every existing tool choice and add
        # an explicit disabled entry, rather than hiding a default override.
        rows = db.execute("SELECT id,disabled_tools FROM sessions WHERE agent_mode='full_agent'").fetchall()
        for row in rows:
            disabled = json.loads(row["disabled_tools"])
            if "present_ui" not in disabled:
                db.execute(
                    "UPDATE sessions SET disabled_tools=? WHERE id=?",
                    (json.dumps([*disabled, "present_ui"], ensure_ascii=False), row["id"]),
                )
