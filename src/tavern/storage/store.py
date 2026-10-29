"""SQLite is the authority; all branch-changing writes use revision checks."""

import copy
import hashlib
import json
import secrets
import sqlite3
import time
import uuid
from contextlib import closing, contextmanager
from pathlib import Path

from tavern.character.cards import normalize, parse_card
from tavern.core.errors import NotFound
from tavern.core.history_refs import message_ref
from tavern.core.models import ContextRecipe, TurnRequest
from tavern.core.settings import SettingsDocument, from_runtime, migrate_settings
from tavern.core.usage import aggregate_cache_usage
from tavern.full_agent.recovery import load_progress, resume_progress
from tavern.memories.memory_records import new_memory
from tavern.storage.message_adapters import MessageAdapters, recipe_types
from tavern.storage.turn_retry import retry_head


def uid():
    return uuid.uuid4().hex


def dump(value):
    return json.dumps(value, ensure_ascii=False)


def new_snapshot(agent_mode: str) -> dict:
    snapshot = {"state": {}, "lore": {}, "scopes": {}, "content": None}
    if agent_mode == "full_agent":
        from tavern.full_agent.state import new_state

        snapshot["full_agent"] = new_state()
    else:
        snapshot["memory_document"] = new_memory()
    return snapshot


def settle_message_archive(db, sid, parent, snapshot):
    """Reduce a snapshot's archive delta to its own entries against ``parent``'s chain."""
    from tavern.memories.memory_archive import ARCHIVE, RETIRING, settle_archive

    remaining = {entry["id"] for key in (ARCHIVE, RETIRING) for entry in snapshot.get(key, [])}
    if not remaining:
        return settle_archive(snapshot, {})
    # Walk nearest ancestors first; the join alone does not guarantee row order,
    # so sort by depth. Only the candidates in this delta need comparison; stop
    # once their latest versions are known. Empty deltas (including Full Agent
    # turns) never read the chain.
    rows = db.execute(
        f"""WITH RECURSIVE chain(id, parent, depth) AS (
            SELECT id, parent, 0 FROM messages WHERE session=? AND id=?
            UNION ALL
            SELECT m.id, m.parent, chain.depth + 1 FROM messages m JOIN chain ON m.id = chain.parent
            WHERE m.session=?
        )
        SELECT json_extract(m.snapshot,'$.{ARCHIVE}') AS delta
        FROM chain JOIN messages m ON m.id = chain.id
        ORDER BY chain.depth""",
        (sid, parent, sid),
    )
    inherited = {}
    for row in rows:
        for entry in json.loads(row["delta"]) if row["delta"] else []:
            if entry["id"] in remaining:
                inherited[entry["id"]] = entry
                remaining.remove(entry["id"])
        if not remaining:
            break
    return settle_archive(snapshot, inherited)


def insert_row(db, table, row):
    """Insert a full row by column name; table and keys come from application code."""
    marks = ",".join("?" * len(row))
    db.execute(f"INSERT INTO {table}({','.join(row)}) VALUES ({marks})", tuple(row.values()))


def write_blob(path: Path, data: bytes):
    """Content-addressed files are trusted by name, so publish them only when complete."""
    temporary = path.with_name(f"{path.name}.{uid()}.tmp")
    try:
        temporary.write_bytes(data)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


class Conflict(ValueError):
    pass


# Persistent database format; bump together with docs/guides/maintenance.md.
SCHEMA_VERSION = 16
SETTINGS_VERSIONS = range(2, 15)
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
MESSAGE_COLUMNS = {"auxiliary": "TEXT NOT NULL DEFAULT '[]'"}


def add_columns(db, table, columns):
    present = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
    missing = [name for name in columns if name not in present]
    for name in missing:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {columns[name]}")
    return set(missing)


def inspect_existing(root, path):
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


class Store(MessageAdapters):
    def __init__(self, root: Path):
        self.root = root
        self.path = root / "tavern.sqlite3"
        # Validate the file before creating directories, tables or journal files.
        migrated = inspect_existing(root, self.path) if self.path.exists() else None
        (root / "blobs").mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
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
            previous_schema = db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            if previous_schema is not None and int(previous_schema[0]) < 16:
                # Schema 15 samples calibrated local estimates. Their opaque
                # identities cannot be backfilled into provider observations.
                # inspect_existing has already made the pre-upgrade backup.
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
            if "title" in added:
                db.execute("""UPDATE sessions SET title=COALESCE(
                    (SELECT name FROM cards WHERE cards.id=sessions.card),
                    json_extract(card_data,'$.name'), '对话')""")
            if "cache_breakpoint_follow" in added:
                for session_row in db.execute("SELECT id,head FROM sessions").fetchall():
                    self._update_cache_follow(db, session_row["id"], session_row["head"], reset=True)
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
        with self.connection() as db:
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
        from tavern.character.card_author_store import initialize_editor_tables

        initialize_editor_tables(self)
        from tavern.advisor.storage import initialize as initialize_advisor

        initialize_advisor(self)
        from tavern.storage.worldbooks import initialize as initialize_worldbooks

        initialize_worldbooks(self)
        self.initialize_message_adapters()
        # Every step above is idempotent, so an interrupted upgrade simply reruns.
        with self.connection() as db:
            if previous_schema is not None and int(previous_schema[0]) < 16:
                from tavern.core.context_usage import retire_estimated_pressure

                retire_estimated_pressure(db)
            db.execute("INSERT OR REPLACE INTO meta VALUES ('schema_version',?)", (str(SCHEMA_VERSION),))

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA secure_delete=ON")
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def device_id(self):
        """An application-local opaque identity, never a hardware/user identifier."""
        with self.connection() as db:
            db.execute(
                "INSERT OR IGNORE INTO meta(key,value) VALUES ('provider_device_id',?)",
                (secrets.token_hex(32),),
            )
            return db.execute("SELECT value FROM meta WHERE key='provider_device_id'").fetchone()[0]

    def cache_breakpoint_history(self, owner, scope="narrative"):
        """Durable send facts, independent of story commits and log retention."""
        from tavern.scripting.sent_breakpoints import SentBreakpoints

        def load(identity):
            with self.connection() as db:
                return {
                    row["prefix"]: json.loads(row["value"])
                    for row in db.execute(
                        "SELECT prefix,value FROM sent_cache_breakpoints WHERE owner=? AND scope=? AND identity=?",
                        (owner, scope, identity),
                    )
                }

        def save(identity, entries):
            with self.connection() as db:
                db.executemany(
                    "INSERT OR REPLACE INTO sent_cache_breakpoints VALUES (?,?,?,?,?)",
                    [(owner, scope, identity, prefix, dump(value)) for prefix, value in entries.items()],
                )

        return SentBreakpoints(load, save)

    def recover(self):
        from tavern.core.context_usage import recover_samples

        with self.connection() as db:
            recover_samples(db)
            db.execute(
                "UPDATE advisor_runs SET status='error',error='应用已重启，可继续或丢弃咨询' WHERE status IN ('queued','running')"
            )
        from tavern.character.card_author_store import recover_editor_runs

        recover_editor_runs(self)
        with self.connection() as db:
            rows = db.execute("SELECT id FROM turns WHERE status IN ('queued','running')").fetchall()
            db.execute(
                "UPDATE turns SET status='interrupted',error='服务重启，未提交的回合已中断' WHERE status IN ('queued','running')"
            )
        for row in rows:
            self.event(row[0], "interrupted", {"message": "服务重启；草稿保留，未重放脚本"})

    def blob(self, data, *, db=None):
        if db is None:
            # Import rollback can remove newly published blobs. Standalone
            # resource writers must wait for that transaction before reusing one.
            with self.connection() as db:
                db.execute("BEGIN IMMEDIATE")
                return self.blob(data, db=db)
        digest = hashlib.sha256(data).hexdigest()
        path = self.root / "blobs" / digest
        if not path.exists():
            write_blob(path, data)
        return digest

    def import_card(self, data, filename):
        document, container, resources, report = parse_card(data, filename)
        from tavern.character.card_workflow import validate_document

        validate_document(document)
        card = uid()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            original = self.blob(data, db=db)
            mapped = {k: self.blob(v, db=db) for k, v in resources.items()}
            db.execute(
                "INSERT INTO cards(id,name,original,container,resources,permission,document,report) VALUES (?,?,?,?,?,0,?,?)",
                (
                    card,
                    normalize(document)["name"],
                    original,
                    container,
                    dump(mapped),
                    dump(document),
                    dump(report),
                ),
            )
        return self.card(card)

    def create_card(self):
        """Blank V2 card stored like an imported JSON file, so export and editing need no special case."""
        from tavern.character.editor_config import blank_card

        with self.connection() as db:
            names = {row[0] for row in db.execute("SELECT name FROM cards")}
        name, index = "新角色", 2
        while name in names:
            name, index = f"新角色 {index}", index + 1
        return self.import_card(dump(blank_card(name)).encode(), "card.json")

    @staticmethod
    def _card_row(row):
        return {
            **dict(row),
            "document": json.loads(row["document"]),
            "report": json.loads(row["report"]),
            "resources": json.loads(row["resources"]),
        }

    def card(self, card):
        with self.connection() as db:
            row = db.execute("SELECT * FROM cards WHERE id=?", (card,)).fetchone()
        if row is None:
            raise NotFound("角色不存在")
        return self._card_row(row)

    def cards(self):
        with self.connection() as db:
            rows = db.execute("SELECT * FROM cards ORDER BY rowid DESC").fetchall()
        return [self._card_row(row) for row in rows]

    def copy_card(self, cid, document=None, source=None):
        from tavern.character.card_workflow import validate_document
        from tavern.character.cards import diagnose

        source = source or self.card(cid)
        document = copy.deepcopy(document if document is not None else source["document"])
        validate_document(document)
        data = document.get("data", document) if document.get("spec") else document
        base, new_id = data["name"] + "（副本）", uid()
        manifest = self.card_manifest(source)
        # Materialize the independent manifest before the DB row becomes visible.
        path = self.card_manifest_path({"id": new_id})
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(dump(manifest), encoding="utf-8")
        try:
            with self.connection() as db:
                db.execute("BEGIN IMMEDIATE")
                names = {r[0] for r in db.execute("SELECT name FROM cards")}
                name, index = base, 2
                while name in names:
                    name, index = f"{base} {index}", index + 1
                data["name"] = name
                db.execute(
                    "INSERT INTO cards(id,name,original,container,resources,permission,document,report) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (
                        new_id,
                        name,
                        source["original"],
                        source["container"],
                        dump(source["resources"]),
                        source["permission"],
                        dump(document),
                        dump(diagnose(document)),
                    ),
                )
                from tavern.storage.worldbooks import copy_defaults

                copy_defaults(db, source["id"], new_id)
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return self.card(new_id)

    def delete_card(self, cid):
        """Physically delete a card; existing stories own their required data."""
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM cards WHERE id=?", (cid,)).fetchone()
            if row is None:
                raise NotFound("角色卡不存在或已删除")
            if db.execute(
                "SELECT 1 FROM turns t JOIN sessions s ON t.session=s.id WHERE s.card=? "
                "AND t.status IN ('queued','running','awaiting_review')",
                (cid,),
            ).fetchone():
                raise Conflict("请先完成或取消使用此角色卡的当前回合")
            card = dict(row)
            for key in ("document", "report", "resources"):
                card[key] = json.loads(card[key])
            manifest = self.card_manifest(card)
            db.execute("DELETE FROM cards WHERE id=?", (cid,))
        self.prune_card_assets(card, manifest)

    def session_card(self, sid):
        with self.connection() as db:
            row = db.execute("SELECT card,card_data FROM sessions WHERE id=?", (sid,)).fetchone()
            if row is None:
                raise NotFound("会话不存在")
        return json.loads(row["card_data"])

    def branch_message_revision(self, sid, mid):
        """One message's card revision for image bridges, without loading every snapshot.

        Returns (on_current_branch, revised_document_or_None).
        """
        with self.connection() as db:
            session = db.execute("SELECT head FROM sessions WHERE id=?", (sid,)).fetchone()
            if session is None:
                raise NotFound("会话不存在")
            parents = dict(db.execute("SELECT id,parent FROM messages WHERE session=?", (sid,)).fetchall())
            cursor, seen = session["head"], set()
            while cursor and cursor != mid:
                if cursor not in parents or cursor in seen:
                    raise ValueError("剧情分支无效")
                seen.add(cursor)
                cursor = parents[cursor]
            if not cursor:
                return False, None
            revised = db.execute(
                "SELECT json_extract(snapshot,'$.content.document') FROM messages WHERE id=?", (mid,)
            ).fetchone()[0]
        return True, json.loads(revised) if revised is not None else None

    def card_manifest_path(self, card):
        filename = f"session-{card['session_id']}" if card.get("session_id") else card["id"]
        directory = (self.root / "dependencies").resolve()
        path = directory / f"{filename}.json"
        if path.resolve().parent != directory:
            raise ValueError("角色依赖缓存路径无效")
        return path

    def card_manifest(self, card):
        path = self.card_manifest_path(card)
        manifest = (
            json.loads(path.read_text(encoding="utf-8"))
            if path.exists()
            else copy.deepcopy(card.get("dependencies", {"resources": {}, "scripts": {}}))
        )
        if card.get("session_id"):
            return manifest
        manifest["resources"] = {
            url: resource
            for url, resource in manifest["resources"].items()
            if (self.root / "blobs" / resource["hash"]).is_file()
        }
        manifest["scripts"] = {
            source: blob
            for source, blob in manifest["scripts"].items()
            if (self.root / "blobs" / blob).is_file()
        }
        # A session may have resolved resources after the library card was imported.
        # Reuse its pinned blobs when creating another conversation from that card.
        with self.connection() as db:
            siblings = [
                row[0]
                for row in db.execute(
                    "SELECT id FROM sessions WHERE card=? ORDER BY created DESC", (card["id"],)
                )
            ]
        for sid in siblings:
            sibling_path = self.card_manifest_path({"session_id": sid})
            if not sibling_path.exists():
                continue
            try:
                sibling = json.loads(sibling_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                # Sibling manifests are only a reuse hint: a file being replaced by another
                # conversation or left damaged must not block this card's own manifest.
                continue
            for url, resource in sibling.get("resources", {}).items():
                if (self.root / "blobs" / resource.get("hash", "")).is_file():
                    manifest["resources"].setdefault(url, resource)
            for source_hash, blob_hash in sibling.get("scripts", {}).items():
                if (self.root / "blobs" / blob_hash).is_file():
                    manifest["scripts"].setdefault(source_hash, blob_hash)
        return manifest

    def cache_card_dependencies(self, card_id, manifest):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute("SELECT 1 FROM cards WHERE id=?", (card_id,)).fetchone():
                return
            path = self.card_manifest_path({"id": card_id})
            current = (
                json.loads(path.read_text(encoding="utf-8"))
                if path.exists()
                else {"resources": {}, "scripts": {}}
            )
            for url, resource in manifest["resources"].items():
                if (self.root / "blobs" / resource["hash"]).is_file():
                    pinned = current["resources"].get(url)
                    if not pinned or not (self.root / "blobs" / pinned["hash"]).is_file():
                        current["resources"][url] = resource
            for source, blob in manifest["scripts"].items():
                if (self.root / "blobs" / blob).is_file():
                    pinned = current["scripts"].get(source)
                    if not pinned or not (self.root / "blobs" / pinned).is_file():
                        current["scripts"][source] = blob
            temporary = path.with_name(path.name + "." + uid() + ".tmp")
            temporary.write_text(dump(current), encoding="utf-8")
            temporary.replace(path)

    def prune_card_assets(self, card, manifest):
        from tavern.character.resources import prune_card_assets

        prune_card_assets(self, card, manifest)

    def edit_card(self, card, document, expected_revision=None):
        from tavern.character.card_workflow import validate_document
        from tavern.character.cards import diagnose

        validate_document(document)
        if not normalize(document).get("name"):
            raise ValueError("角色必须有名称")
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT revision FROM cards WHERE id=?", (card,)).fetchone()
            if current is None:
                raise NotFound("角色卡不存在或已删除")
            if expected_revision is not None and current[0] != expected_revision:
                raise Conflict("角色卡已改变；草稿已保留，请重新载入或另存副本")
            result = db.execute(
                "UPDATE cards SET name=?,document=?,report=?,revision=revision+1 WHERE id=?",
                (normalize(document)["name"], dump(document), dump(diagnose(document)), card),
            )
            if result.rowcount != 1:
                raise NotFound("角色卡不存在或已删除")
        return self.card(card)

    def settings_document(self):
        with self.connection() as db:
            return SettingsDocument.model_validate_json(
                db.execute("SELECT value FROM meta WHERE key='settings'").fetchone()[0]
            )

    def settings(self, mode="half_agent", channel=None):
        settings = self.settings_document().runtime(mode, channel)
        generations = {
            provider.chatgpt_account_id: self.chatgpt.generation(provider.chatgpt_account_id)
            for provider in settings.providers
            if provider.auth_mode == "chatgpt_subscription" and provider.chatgpt_account_id
        }
        for config in (settings, settings._full_runtime):
            if config is not None:
                config._data_root = self.root
                config._chatgpt = self.chatgpt
                config._auth_generations = generations.copy()
                if config.auth_mode == "chatgpt_subscription":
                    config._auth_generation = generations.get(config.chatgpt_account_id, "")
        return settings

    def session_settings(self, session, *, require_origin=True):
        if isinstance(session, str):
            with self.connection() as db:
                row = db.execute(
                    "SELECT agent_mode,channel_origin,context_strategy,compact_auto,model_selection,kemini "
                    "FROM sessions WHERE id=?",
                    (session,),
                ).fetchone()
            if row is None:
                raise NotFound("对话不存在")
            session = dict(row)
        channel = session.get("channel_origin")
        if not channel and require_origin:
            raise ValueError("请先确认此旧对话的渠道归属")
        settings = self.settings(session["agent_mode"], channel or "standard")
        settings.context_strategy = session.get("context_strategy", "new_context")
        settings.compact_auto = bool(session.get("compact_auto", True))
        if settings.context_strategy == "openai_compact" and channel != "chatgpt":
            raise ValueError("只有 ChatGPT 官方渠道的对话可以使用云端 compact")
        selection = session.get("model_selection") or {}
        if isinstance(selection, str):
            selection = json.loads(selection)
        if selection:
            from tavern.core.model_selection import select_runtime

            settings = select_runtime(
                settings,
                "full_agent" if session["agent_mode"] == "full_agent" else "foreground",
                selection,
                channel,
            )
        if session["kemini"]:
            # A Kemini conversation's identity is fixed at creation, like its channel.
            for config in (settings, settings._full_runtime):
                if config is not None:
                    config.kemini.enabled = True
        return settings

    @property
    def chatgpt(self):
        if not hasattr(self, "_chatgpt_accounts"):
            from tavern.chatgpt.service import ChatGPTAccounts

            self._chatgpt_accounts = ChatGPTAccounts(
                self.root, lambda: self.settings_document().shared.network
            )
        return self._chatgpt_accounts

    def save_settings(self, settings):
        document = settings if isinstance(settings, SettingsDocument) else from_runtime(settings)
        document = SettingsDocument.model_validate(document.model_dump())
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            current = SettingsDocument.model_validate_json(
                db.execute("SELECT value FROM meta WHERE key='settings'").fetchone()[0]
            )
            for scope in document.revisions:
                if getattr(current, scope) != getattr(document, scope):
                    document.revisions[scope] = current.revisions[scope] + 1
            db.execute("UPDATE meta SET value=? WHERE key='settings'", (document.model_dump_json(),))

    def new_session(self, options):
        document = self.settings_document()
        channel = options.channel_origin or getattr(document, options.agent_mode).default_channel
        if options.kemini and not document.shared.kemini.enabled:
            raise ValueError("请先在全局设置 → 杂项启用 Kemini 式实验功能")
        if options.kemini and channel != "standard":
            raise ValueError("ChatGPT 官方渠道不能创建 Kemini 实验对话")
        card = self.card(options.card_id)
        data = normalize(card["document"])
        greetings = [data.get("first_mes", ""), *data.get("alternate_greetings", [])]
        if not 0 <= options.greeting < len(greetings):
            raise ValueError("开场白不存在")
        sid, mid = uid(), uid()
        snapshot = new_snapshot(options.agent_mode)
        from tavern.character.card_workflow import validate_document

        validate_document(card["document"])
        raw = greetings[options.greeting]
        persona_data = self.persona(options.persona_id) if options.persona_id else {}
        settings = self.settings()
        dependencies = self.card_manifest(card)
        now = time.time()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute("SELECT 1 FROM cards WHERE id=?", (card["id"],)).fetchone():
                raise NotFound("角色卡不存在或已删除")
            if options.worldbook_ids:
                from tavern.character.worldbooks import REGISTRY, apply_mounts
                from tavern.storage.worldbooks import read as read_worldbook

                rows = {wid: read_worldbook(db, wid) for wid in options.worldbook_ids}
                # card_data stays the plain shelf copy: it is the shelf-sync baseline.
                document, snapshot[REGISTRY] = apply_mounts(
                    card["document"], [], options.worldbook_ids, [], rows
                )
                validate_document(document)
                snapshot["content"] = {"document": document, "reason": "新建对话时挂载世界书"}
            insert_row(
                db,
                "sessions",
                {
                    "id": sid,
                    "card": card["id"],
                    "head": mid,
                    "revision": 0,
                    "persona": persona_data.get("name", options.persona),
                    "persona_data": dump(persona_data),
                    "policy": options.policy,
                    "recipe": settings.context_recipe.model_dump_json(),
                    "created": now,
                    "title": card["name"],
                    "agent_mode": options.agent_mode,
                    # Full Agent keeps worldbook entries inside the conversation it manages.
                    "lore_injection_mode": (
                        "conversation" if options.agent_mode == "full_agent" else settings.lore_injection_mode
                    ),
                    "card_data": dump({**card, "session_id": sid, "dependencies": dependencies}),
                    "channel_origin": channel,
                    "kemini": int(options.kemini),
                    "opening_locked": 0,
                },
            )
            insert_row(
                db,
                "messages",
                {
                    "id": mid,
                    "session": sid,
                    "parent": None,
                    "role": "assistant",
                    "raw": raw,
                    "model": raw,
                    "display": raw,
                    "snapshot": dump(snapshot),
                    "created": now,
                },
            )
            self.create_message_adapter(db, "story", sid, options.agent_mode)
        return self.session(sid)

    def fork_session(self, sid, head, expected_revision):
        """Copy a committed assistant prefix into a session with independent message IDs."""
        from tavern.advisor.storage import fork as fork_advisor
        from tavern.conversation.cache_breakpoint import reset_follow
        from tavern.storage.message_copy import copy_traces, remap_snapshot

        new_sid = uid()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute(
                "SELECT 1 FROM context_operations WHERE session=? AND status IN ('queued','running')", (sid,)
            ).fetchone():
                raise Conflict("请先完成或取消上下文整理")
            source = db.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
            if source is None:
                raise NotFound("会话不存在")
            if source["revision"] != expected_revision:
                raise Conflict("会话已改变，请刷新后重试")
            if db.execute(
                "SELECT 1 FROM turns WHERE session=? AND status IN ('queued','running','awaiting_review')",
                (sid,),
            ).fetchone():
                raise Conflict("请先完成或取消当前回合")
            rows = {r["id"]: dict(r) for r in db.execute("SELECT * FROM messages WHERE session=?", (sid,))}
            if head not in rows or rows[head]["role"] != "assistant":
                raise ValueError("请选择当前会话中的 AI 回复")
            prefix, seen, cursor = [], set(), head
            while cursor:
                if cursor not in rows or cursor in seen:
                    raise ValueError("剧情分支无效")
                message = rows[cursor]
                message["snapshot"] = json.loads(message["snapshot"])
                message["auxiliary"] = json.loads(message["auxiliary"])
                prefix.append(message)
                seen.add(cursor)
                cursor = message["parent"]
            prefix.reverse()
            card_data = json.loads(source["card_data"]) if source["card_data"] else None
            if card_data:
                card_data["dependencies"] = self.card_manifest(card_data)
                card_data["session_id"] = new_sid
            base_title = (source["title"] or (card_data or {}).get("name") or "对话")[:90]
            title, suffix = f"{base_title} · 副本", 2
            while db.execute("SELECT 1 FROM sessions WHERE title=?", (title,)).fetchone():
                title, suffix = f"{base_title} · 副本 {suffix}", suffix + 1
            ids = {m["id"]: uid() for m in prefix}
            copies = []
            for position, message in enumerate(prefix):
                cloned = copy.deepcopy(message)
                cloned.update(id=ids[message["id"]], session=new_sid, parent=ids.get(message["parent"]))
                remap_snapshot(
                    cloned["snapshot"], ids, prefix[: position + 1], copies + [cloned], new_story=True
                )
                copies.append(cloned)
            # Every story setting carries over; only identity, progress and references are rewritten.
            insert_row(
                db,
                "sessions",
                {
                    **dict(source),
                    "id": new_sid,
                    "head": ids[head],
                    "revision": 0,
                    "created": time.time(),
                    "title": title,
                    "card_data": dump(card_data) if card_data else None,
                    # A breakpoint outside the copied prefix has no counterpart in the fork.
                    "cache_breakpoint_message": ids.get(source["cache_breakpoint_message"]),
                    "cache_breakpoint_follow": dump(
                        reset_follow(copies, json.loads(source["cache_breakpoint_follow"]))
                    ),
                },
            )
            for message in copies:
                insert_row(
                    db,
                    "messages",
                    {
                        **message,
                        "snapshot": dump(message["snapshot"]),
                        "auxiliary": dump(message["auxiliary"]),
                    },
                )
            self.copy_message_adapter(db, "story", sid, new_sid)
            fork_advisor(db, sid, new_sid, ids)
            # Prefix hashes need no message-ID remapping. Only matching content
            # boundaries will be replayed in the fork's actual request.
            db.execute(
                "INSERT INTO sent_cache_breakpoints SELECT ?,scope,identity,prefix,value "
                "FROM sent_cache_breakpoints WHERE owner=?",
                (new_sid, sid),
            )
            copy_traces(db, new_sid, ids, new_story=True)
        return self.session(new_sid)

    def session(self, sid):
        with self.connection() as db:
            row = db.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
            if row is None:
                raise NotFound("会话不存在")
            turns = [
                dict(r)
                for r in db.execute(
                    "SELECT id,status,draft,error,created FROM turns WHERE session=? ORDER BY created DESC LIMIT 10",
                    (sid,),
                )
            ]
            result = dict(row)
            result["recipe"] = recipe_types(ContextRecipe.model_validate_json(result["recipe"]).model_dump())
            result["model_selection"] = json.loads(result["model_selection"])
            result["persona_data"] = json.loads(result["persona_data"])
            result["disabled_tools"] = json.loads(result["disabled_tools"])
            result["tool_result_options"] = json.loads(result["tool_result_options"])
            result["card_data"] = json.loads(result["card_data"]) if result["card_data"] else None
            result["turns"] = turns
        result["messages"] = self.history(sid, result["head"])
        result["snapshot"] = (
            copy.deepcopy(result["messages"][-1]["snapshot"])
            if result["messages"]
            else new_snapshot(result["agent_mode"])
        )
        result["opening_editable"] = (
            not result["opening_locked"]
            and len(result["messages"]) == 1
            and result["messages"][0]["role"] == "assistant"
            and result["messages"][0]["parent"] is None
            and not any(t["status"] in ("queued", "running", "awaiting_review") for t in turns)
        )
        if result["agent_mode"] == "full_agent":
            from tavern.full_agent.state import read_full_state

            read_full_state(result["snapshot"])
        settings = self.session_settings(result, require_origin=False)
        result["compact_auto"] = bool(result["compact_auto"])
        result["plot_enabled"] = bool(result["plot_enabled"])
        result["kemini"] = bool(result["kemini"])
        result["update_variables"] = bool(result["update_variables"])
        from tavern.core.update_policy import normalize_policy

        result["policy"] = normalize_policy(result["policy"])
        result["policy_effective"] = result["policy"] or normalize_policy(settings.policy)
        result["cache_breakpoint_auto"] = bool(result["cache_breakpoint_auto"])
        from tavern.conversation.cache_breakpoint import (
            advance_follow,
            breakpoint_reason,
            effective_message_id,
        )

        result["cache_breakpoint_follow"] = advance_follow(
            result["messages"], json.loads(result["cache_breakpoint_follow"])
        )
        result["cache_breakpoint_effective_id"] = effective_message_id(
            result, settings.for_role("full_agent" if result["agent_mode"] == "full_agent" else "foreground")
        )
        result["cache_breakpoint_reason"] = breakpoint_reason(result, result["cache_breakpoint_effective_id"])
        from tavern.memories.memory_archive import read_archive, with_archive_status
        from tavern.memories.memory_records import read_memory, with_source_status

        result["memory_view"] = with_source_status(read_memory(result["snapshot"]), result["messages"])
        result["archived_memory"] = with_archive_status(read_archive(result["messages"]), result["messages"])
        return result

    def sessions(self):
        with self.connection() as db:
            return [
                dict(x)
                for x in db.execute(
                    "SELECT s.*,COALESCE(c.name,json_extract(s.card_data,'$.name')) AS name "
                    "FROM sessions s LEFT JOIN cards c ON c.id=s.card ORDER BY s.created DESC"
                )
            ]

    def session_turn_ids(self, sid):
        with self.connection() as db:
            if not db.execute("SELECT 1 FROM sessions WHERE id=?", (sid,)).fetchone():
                raise NotFound("会话不存在")
            return [row[0] for row in db.execute("SELECT id FROM turns WHERE session=?", (sid,))]

    def delete_session(self, sid):
        """Delete all database state owned by a session in one transaction."""
        with self.connection() as db:
            row = db.execute("SELECT card_data FROM sessions WHERE id=?", (sid,)).fetchone()
        card = json.loads(row["card_data"]) if row and row["card_data"] else None
        manifest = self.card_manifest(card) if card else None
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute("SELECT 1 FROM sessions WHERE id=?", (sid,)).fetchone():
                raise NotFound("会话不存在")
            db.execute("DELETE FROM changes WHERE turn IN (SELECT id FROM turns WHERE session=?)", (sid,))
            db.execute("DELETE FROM events WHERE turn IN (SELECT id FROM turns WHERE session=?)", (sid,))
            db.execute("DELETE FROM turns WHERE session=?", (sid,))
            db.execute("DELETE FROM full_agent_trace WHERE session=?", (sid,))
            db.execute("DELETE FROM context_operations WHERE session=?", (sid,))
            db.execute("DELETE FROM messages WHERE session=?", (sid,))
            db.execute("DELETE FROM sessions WHERE id=?", (sid,))
            db.execute("DELETE FROM sent_cache_breakpoints WHERE owner=?", (sid,))
            db.execute(
                "DELETE FROM context_token_samples WHERE owner IN (?,?)", ("story:" + sid, "advisor:" + sid)
            )
            from tavern.storage.retrieval_cleanup import prune_orphan_retrieval

            prune_orphan_retrieval(db)
        if card:
            self.prune_card_assets(card, manifest)

    def history(self, sid, head):
        with self.connection() as db:
            rows = {
                r["id"]: dict(r)
                for r in db.execute("SELECT * FROM messages WHERE session=? ORDER BY created,rowid", (sid,))
            }

        def version_group(message):
            parent = rows.get(message["parent"])
            if parent is not None and parent["role"] == "user":
                return "exchange", parent["parent"]
            return "opening", message["parent"]

        versions = {}
        for row in rows.values():
            if row["role"] == "assistant":
                versions.setdefault(version_group(row), []).append(row["id"])
        result, seen = [], set()
        while head:
            if head not in rows or head in seen:
                raise ValueError("剧情分支无效")
            row = rows[head]
            row["snapshot"] = json.loads(row["snapshot"])
            row["auxiliary"] = json.loads(row["auxiliary"])
            if row["role"] == "assistant":
                row["versions"] = versions[version_group(row)]
                row["version_index"] = row["versions"].index(row["id"])
            result.append(row)
            seen.add(head)
            head = row["parent"]
        result.reverse()
        # Derived like versions: the UI shows the same H the models cite.
        for position, row in enumerate(result, 1):
            row["ref"] = message_ref(position)
        return result

    def switch_message_version(self, sid, mid, target, expected_revision):
        session = self.session(sid)
        if session["revision"] != expected_revision:
            raise Conflict("会话已改变，请刷新后重试")
        latest = session["messages"][-1] if session["messages"] else None
        if not latest or latest["role"] != "assistant" or latest["id"] != mid:
            raise ValueError("只能切换最新一轮的版本")
        if target not in latest["versions"]:
            raise ValueError("目标版本不属于当前轮次")
        return self.update_session(sid, expected_revision, head=target)

    def branches(self, sid):
        with self.connection() as db:
            return [
                dict(r)
                for r in db.execute(
                    "SELECT id,parent,role,raw,created FROM messages WHERE session=? ORDER BY created", (sid,)
                )
            ]

    def remove_lore_injection(self, sid, anchor_id, entry_id, expected_revision):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute(
                "SELECT 1 FROM context_operations WHERE session=? AND status IN ('queued','running')", (sid,)
            ).fetchone():
                raise Conflict("请先完成或取消上下文整理")
            session = db.execute("SELECT head,revision FROM sessions WHERE id=?", (sid,)).fetchone()
            if session is None:
                raise NotFound("会话不存在")
            if session["revision"] != expected_revision:
                raise Conflict("会话已改变，请刷新后重试")
            if db.execute(
                "SELECT 1 FROM turns WHERE session=? AND status IN ('running','queued','awaiting_review')",
                (sid,),
            ).fetchone():
                raise Conflict("请先完成或取消当前回合")
            rows = {
                r["id"]: r
                for r in db.execute("SELECT id,parent,snapshot FROM messages WHERE session=?", (sid,))
            }
            branch, branch_ids, mid = [], set(), session["head"]
            while mid:
                if mid not in rows or mid in branch_ids:
                    raise ValueError("剧情分支无效")
                branch.append(rows[mid])
                branch_ids.add(mid)
                mid = rows[mid]["parent"]
            if anchor_id not in branch_ids:
                raise ValueError("只能移除当前分支中的世界书注入")
            found = False
            reached_anchor = seen_injection = False
            for row in reversed(branch):
                reached_anchor = reached_anchor or row["id"] == anchor_id
                snapshot = json.loads(row["snapshot"])
                changed = False
                for record in snapshot.get("lore_injections", []):
                    if record["id"] == entry_id and record["anchor_id"] == anchor_id:
                        # Keep its anchor and usage state until the original window exits.
                        record.clear()
                        record.update(id=entry_id, anchor_id=anchor_id, manually_removed=True)
                        changed = found = True
                        seen_injection = True
                # The anchor/user snapshot predates compilation of its first injection.
                # Record the removal there too, so regenerating that reply cannot revive it.
                if reached_anchor and not seen_injection:
                    snapshot.setdefault("lore_injections", []).append(
                        {"id": entry_id, "anchor_id": anchor_id, "manually_removed": True}
                    )
                    snapshot.setdefault("lore", {})[entry_id] = {"present": True}
                    changed = True
                if changed:
                    db.execute("UPDATE messages SET snapshot=? WHERE id=?", (dump(snapshot), row["id"]))
            if not found:
                raise ValueError("世界书注入不存在")
            db.execute("UPDATE sessions SET revision=revision+1 WHERE id=?", (sid,))
        return self.session(sid)

    def set_session_script_permission(self, sid, expected, enabled):
        """Change the story-owned permission without replacing its card or replaying initialization."""
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT revision,card_data FROM sessions WHERE id=?", (sid,)).fetchone()
            if row is None:
                raise NotFound("会话不存在")
            if row["revision"] != expected:
                raise Conflict("会话已改变，请刷新后重试")
            if db.execute(
                "SELECT 1 FROM turns WHERE session=? AND status IN ('running','queued','awaiting_review')",
                (sid,),
            ).fetchone():
                raise Conflict("请先完成或取消当前回合")
            if db.execute(
                "SELECT 1 FROM context_operations WHERE session=? AND status IN ('queued','running')", (sid,)
            ).fetchone():
                raise Conflict("请先完成或取消上下文整理")
            card = json.loads(row["card_data"])
            card["permission"] = int(enabled)
            db.execute("UPDATE sessions SET card_data=?,revision=revision+1 WHERE id=?", (dump(card), sid))
        return {"session": self.session(sid), "card": card}

    def update_session(self, sid, expected, **fields):
        if "policy" in fields:
            from pydantic import TypeAdapter

            from tavern.core.update_policy import Policy

            fields["policy"] = TypeAdapter(Policy | None).validate_python(fields["policy"])
        if not fields or not set(fields).issubset(
            {
                "head",
                "persona",
                "policy",
                "recipe",
                "persona_data",
                "turn_preference",
                "update_variables",
                "title",
                "cache_breakpoint_auto",
                "cache_breakpoint_message",
                "cache_breakpoint_follow",
                "compact_auto",
            }
        ):
            raise ValueError("无效会话字段")
        if "title" in fields:
            title = fields["title"].strip()
            if not title or len(title) > 100:
                raise ValueError("对话标题须为 1 到 100 个字符")
            fields["title"] = title
        if "channel_origin" in fields or "context_strategy" in fields:
            raise ValueError("渠道归属不可修改；上下文模式必须通过整理操作切换")
        if fields.get("head"):
            self.history(sid, fields["head"])
        with self.connection() as db:
            active = db.execute(
                "SELECT 1 FROM turns WHERE session=? AND status IN ('running','queued','awaiting_review')",
                (sid,),
            ).fetchone()
            if active:
                raise Conflict("请先完成或取消当前回合")
            if db.execute(
                "SELECT 1 FROM context_operations WHERE session=? AND status IN ('queued','running')", (sid,)
            ).fetchone():
                raise Conflict("请先完成或取消上下文整理")
            assignments = ",".join(f"{k}=?" for k in fields)
            r = db.execute(
                f"UPDATE sessions SET {assignments},revision=revision+1 WHERE id=? AND revision=?",
                (*fields.values(), sid, expected),
            )
            if r.rowcount != 1:
                raise Conflict("会话已改变，请刷新后重试")
            if fields.get("head"):
                self._update_cache_follow(db, sid, fields["head"])
                db.execute(
                    "DELETE FROM context_token_samples WHERE owner IN (?,?)",
                    ("story:" + sid, "advisor:" + sid),
                )
        return self.session(sid)

    def set_cache_breakpoint(
        self, sid, expected, automatic, message_id, follow_interval=None, reply_offset=None
    ):
        from tavern.conversation.cache_breakpoint import reply_ids, reset_follow

        session = self.session(sid)
        if not automatic and message_id is not None:
            if message_id not in reply_ids(session["messages"]):
                raise ValueError("请选择当前分支上已完成的 AI 正文回复")
        follow = dict(session["cache_breakpoint_follow"])
        for key, value in (("interval", follow_interval), ("offset", reply_offset)):
            if value is not None:
                if type(value) is not int or value < 1:
                    raise ValueError("跟进间隔和回复位置须为正整数")
                follow[key] = value
        if automatic:
            follow = reset_follow(session["messages"], follow)
        return self.update_session(
            sid,
            expected,
            cache_breakpoint_auto=int(automatic),
            cache_breakpoint_message=None if automatic else message_id,
            cache_breakpoint_follow=dump(follow),
        )

    def _update_cache_follow(self, db, sid, head, *, advance=False, reset=False):
        from tavern.conversation.cache_breakpoint import advance_follow, reply_ids, reset_follow

        row = db.execute(
            "SELECT cache_breakpoint_follow,cache_breakpoint_auto FROM sessions WHERE id=?", (sid,)
        ).fetchone()
        follow = json.loads(row["cache_breakpoint_follow"])
        messages = {
            message["id"]: dict(message)
            for message in db.execute("SELECT id,parent,role FROM messages WHERE session=?", (sid,))
        }
        history, seen = [], set()
        while head and head in messages and head not in seen:
            seen.add(head)
            history.append(messages[head])
            head = messages[head]["parent"]
        history.reverse()
        follow = (
            reset_follow(history, follow)
            if reset
            else advance_follow(
                history,
                follow,
                advance
                and bool(row["cache_breakpoint_auto"])
                and bool(history)
                and history[-1]["id"] in reply_ids(history),
            )
        )
        db.execute("UPDATE sessions SET cache_breakpoint_follow=? WHERE id=?", (dump(follow), sid))

    def edit_memory_record(self, sid, edit):
        from tavern.memories.memory_records import install_memory, manual_edit, read_memory

        session = self.session(sid)
        if not session["head"]:
            raise ValueError("当前分支没有可保存档案的消息")
        snapshot = copy.deepcopy(session["snapshot"])
        memory = read_memory(snapshot)
        if edit.action != "add" and not any(r["id"] == edit.id for r in memory["records"]):
            from tavern.memories.memory_archive import edit_archive

            edit_archive(snapshot, session["messages"], edit)
            return self._save_memory_snapshot(sid, session, snapshot, edit.expected_revision)
        memory = manual_edit(memory, edit)
        if edit.action != "delete":
            from tavern.memories.memory_retention import retain_memory

            memory = retain_memory(memory, session["messages"], self.settings().for_role("background"))
        install_memory(snapshot, memory)
        return self._save_memory_snapshot(sid, session, snapshot, edit.expected_revision)

    def _save_memory_snapshot(self, sid, session, snapshot, expected_revision):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute(
                "SELECT 1 FROM context_operations WHERE session=? AND status IN ('queued','running')", (sid,)
            ).fetchone():
                raise Conflict("请先完成或取消上下文整理")
            if db.execute(
                "SELECT 1 FROM turns WHERE session=? AND status IN ('queued','running','awaiting_review')",
                (sid,),
            ).fetchone():
                raise Conflict("请先完成或取消当前回合")
            result = db.execute(
                "UPDATE sessions SET revision=revision+1 WHERE id=? AND revision=? AND head=?",
                (sid, expected_revision, session["head"]),
            )
            if result.rowcount != 1:
                raise Conflict("会话已改变，请刷新后重试")
            head = db.execute("SELECT parent FROM messages WHERE id=?", (session["head"],)).fetchone()
            settle_message_archive(db, sid, head["parent"], snapshot)
            db.execute(
                "UPDATE messages SET snapshot=? WHERE id=? AND session=?",
                (dump(snapshot), session["head"], sid),
            )
        return self.session(sid)

    def save_opening(self, session, greeting):
        """Publish text and its initialized state together, after the async sandbox finishes."""
        sid = session["id"]
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute(
                "SELECT revision,head,opening_locked FROM sessions WHERE id=?", (sid,)
            ).fetchone()
            if current is None:
                raise NotFound("会话不存在")
            if current["opening_locked"]:
                raise Conflict("已启动模型任务，不能再修改开场白")
            if current["revision"] != session["revision"] or current["head"] != greeting["id"]:
                raise Conflict("会话已改变，请刷新后重试")
            if db.execute(
                "SELECT 1 FROM turns WHERE session=? AND status IN ('queued','running','awaiting_review')",
                (sid,),
            ).fetchone():
                raise Conflict("请先完成或取消当前回合")
            db.execute(
                "UPDATE messages SET raw=?,model=?,display=?,snapshot=? WHERE id=? AND session=?",
                (
                    greeting["raw"],
                    greeting["model"],
                    greeting["display"],
                    dump(greeting["snapshot"]),
                    greeting["id"],
                    sid,
                ),
            )
            db.execute("UPDATE sessions SET revision=revision+1 WHERE id=?", (sid,))
        return self.session(sid)

    def create_turn(self, sid, request, *, model_task=True):
        experiment_allowed = self.settings_document().shared.kemini.enabled
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute(
                "SELECT 1 FROM context_operations WHERE session=? AND status IN ('queued','running')", (sid,)
            ).fetchone():
                raise Conflict("上下文正在整理，请先完成或取消整理")
            existing = db.execute(
                "SELECT id,request FROM turns WHERE session=? AND operation=?", (sid, request.operation_id)
            ).fetchone()
            if existing:
                if TurnRequest.model_validate_json(existing["request"]).model_dump() != request.model_dump():
                    raise Conflict("相同 operation_id 不能用于不同请求")
                return existing["id"], False
            session = db.execute(
                "SELECT revision,agent_mode,kemini,head FROM sessions WHERE id=?", (sid,)
            ).fetchone()
            if not session:
                raise NotFound("会话不存在")
            # Kemini conversations never fall back to ordinary requests. Scene
            # refresh runs only Tracker, which the experiment never touches.
            if session["kemini"] and not experiment_allowed and not request.scene_refresh:
                raise ValueError("Kemini 式实验功能已在全局设置 → 杂项关闭；重新启用后才能继续此对话")
            if session[0] != request.expected_revision:
                raise Conflict("会话已改变，请刷新后重试")
            if db.execute(
                "SELECT 1 FROM turns WHERE session=? AND status IN ('queued','running','awaiting_review')",
                (sid,),
            ).fetchone():
                raise Conflict("当前会话已有运行中的回合")
            if request.replacement_text is not None:
                target = request.message_id or session["head"]
                user = db.execute(
                    "SELECT u.id FROM messages a JOIN messages u ON u.id=a.parent "
                    "WHERE a.session=? AND a.id=? AND a.role='assistant' AND u.role='user'",
                    (sid, target),
                ).fetchone()
                if target != session["head"] or user is None:
                    raise ValueError("只能修改当前分支最新一轮的用户输入并重试")
            if request.resume_turn_id:
                if session["agent_mode"] != "full_agent":
                    raise Conflict("只有 Full Agent 回合可续接工具调用")
                source = db.execute(
                    "SELECT session,status,request FROM turns WHERE id=?", (request.resume_turn_id,)
                ).fetchone()
                latest = db.execute(
                    "SELECT id FROM turns WHERE session=? ORDER BY created DESC,rowid DESC LIMIT 1", (sid,)
                ).fetchone()
                if (
                    source is None
                    or source["session"] != sid
                    or source["status"] not in ("error", "cancelled", "interrupted")
                    or latest["id"] != request.resume_turn_id
                ):
                    raise Conflict("只能续接当前会话最新的失败或中断回合")
                if not db.execute(
                    "SELECT 1 FROM full_agent_progress WHERE turn=?", (request.resume_turn_id,)
                ).fetchone():
                    raise Conflict("此回合没有可续接进度；已拒绝的审核草稿不能恢复，请重新开始")
                previous = TurnRequest.model_validate_json(source["request"])
                matching = (
                    "text",
                    "expected_revision",
                    "mode",
                    "message_id",
                    "allow_content_edit",
                    "update_variables",
                    "generate",
                    "scene_refresh",
                    "regenerate_include_reply",
                    "replacement_text",
                )
                if any(getattr(previous, field) != getattr(request, field) for field in matching):
                    raise Conflict("续接请求的输入或授权已改变，请重新开始本轮")
            tid = uid()
            db.execute(
                "INSERT INTO turns(id,session,operation,request,status,created) VALUES (?,?,?,?,?,?)",
                (tid, sid, request.operation_id, request.model_dump_json(), "queued", time.time()),
            )
            if model_task:
                # The task owns the opening before any async preparation, even if it later fails.
                db.execute("UPDATE sessions SET opening_locked=1 WHERE id=?", (sid,))
            if request.resume_turn_id:
                row = db.execute(
                    "SELECT payload FROM full_agent_progress WHERE turn=?", (request.resume_turn_id,)
                ).fetchone()
                if row:
                    progress = resume_progress(json.loads(row["payload"]))
                    db.execute(
                        "INSERT INTO full_agent_progress(turn,payload) VALUES (?,?)", (tid, dump(progress))
                    )
                    trace = progress["new_trace"]
                    for index, item in enumerate(trace):
                        message = item["message"]
                        if (
                            message["role"] != "assistant"
                            or message.get("_full_agent_final")
                            or message.get("_full_agent_rejected")
                        ):
                            continue
                        assistant_seq = index + 1
                        if message.get("content") and not message.get("_story_call_id"):
                            db.execute(
                                "INSERT INTO events(turn,kind,payload,created) VALUES (?,?,?,?)",
                                (
                                    tid,
                                    "full_agent_assistant",
                                    dump(
                                        {
                                            "activity_id": f"text-{assistant_seq}",
                                            "order": assistant_seq * 10,
                                            "content": message["content"],
                                            "is_story": bool(message.get("_story_received")),
                                        }
                                    ),
                                    time.time(),
                                ),
                            )
                        results = {}
                        for following in trace[index + 1 :]:
                            part = following["message"]
                            if part["role"] == "assistant":
                                break
                            if part["role"] == "tool":
                                results[part["tool_call_id"]] = part["content"]
                        for call_index, call in enumerate(message.get("tool_calls", [])):
                            activity = {
                                "activity_id": f"call-{assistant_seq}-{call_index}",
                                "order": assistant_seq * 10 + call_index + 1,
                                "name": call["function"]["name"],
                                "arguments": call["function"]["arguments"],
                            }
                            result = results.get(call["id"])
                            if result is None:
                                continue
                            db.execute(
                                "INSERT INTO events(turn,kind,payload,created) VALUES (?,?,?,?)",
                                (tid, "full_agent_tool_started", dump(activity), time.time()),
                            )
                            db.execute(
                                "INSERT INTO events(turn,kind,payload,created) VALUES (?,?,?,?)",
                                (
                                    tid,
                                    "full_agent_tool",
                                    dump({**activity, "result": json.loads(result)}),
                                    time.time(),
                                ),
                            )
        return tid, True

    def full_agent_progress(self, tid):
        with self.connection() as db:
            row = db.execute("SELECT payload FROM full_agent_progress WHERE turn=?", (tid,)).fetchone()
        return load_progress(json.loads(row["payload"])) if row else None

    def reject_review(self, tid):
        """Explicit rejection retires executable work but preserves its audit trail."""
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT s.agent_mode FROM turns t JOIN sessions s ON s.id=t.session WHERE t.id=?", (tid,)
            ).fetchone()
            clear_draft = ",draft='',preview=NULL" if row and row["agent_mode"] == "full_agent" else ""
            result = db.execute(
                f"UPDATE turns SET status='cancelled',pending=NULL{clear_draft} "
                "WHERE id=? AND status='awaiting_review'",
                (tid,),
            )
            if result.rowcount != 1:
                raise Conflict("此回合没有待审核变更")
            db.execute("DELETE FROM full_agent_progress WHERE turn=?", (tid,))
            if clear_draft:
                db.execute(
                    "INSERT INTO events(turn,kind,payload,created) VALUES (?,?,?,?)",
                    (tid, "text_reset", dump({"text": ""}), time.time()),
                )
            db.execute(
                "INSERT INTO events(turn,kind,payload,created) VALUES (?,?,?,?)",
                (tid, "cancelled", dump({"reason": "review_rejected", "resumable": False}), time.time()),
            )
        return self.turn(tid)

    def save_full_agent_progress(self, tid, progress):
        with self.connection() as db:
            db.execute(
                "INSERT OR REPLACE INTO full_agent_progress(turn,payload) VALUES (?,?)", (tid, dump(progress))
            )

    def turn(self, tid):
        with self.connection() as db:
            r = db.execute("SELECT * FROM turns WHERE id=?", (tid,)).fetchone()
            if r is None:
                raise NotFound("回合不存在")
            resumable = r["status"] in ("error", "cancelled", "interrupted") and bool(
                db.execute("SELECT 1 FROM full_agent_progress WHERE turn=?", (tid,)).fetchone()
            )
            # Consecutive failed continuations replay the same saved context, so
            # the page can suggest a fresh start. Only the latest failed turn can
            # be resumed, hence every link of the chain is itself a failure.
            resume_failures = 0
            source = r
            while resumable and (previous := json.loads(source["request"]).get("resume_turn_id")):
                resume_failures += 1
                source = db.execute("SELECT request FROM turns WHERE id=?", (previous,)).fetchone()
            anchor = retry_head(db, r)
        result = dict(r)
        result["resumable"] = resumable
        result["resume_failures"] = resume_failures
        result["retry_head"] = anchor
        for key in ("request", "preview", "pending"):
            result[key] = json.loads(result[key]) if result[key] else None
        if result["pending"] and "review_changes" not in result["pending"]:
            from tavern.conversation.update_review import review_changes

            pending = result["pending"]
            after = (pending.get("scene") or (pending.get("messages") or [{}])[-1]).get("snapshot", {})
            pending["review_changes"] = review_changes(
                pending.get("before", {}), after, self.session_card(result["session"])["document"]
            )
        return result

    def set_turn(self, tid, **fields):
        if not set(fields).issubset({"status", "draft", "error", "preview", "pending"}):
            raise ValueError("无效回合字段")
        with self.connection() as db:
            db.execute(
                f"UPDATE turns SET {','.join(f'{k}=?' for k in fields)} WHERE id=?", (*fields.values(), tid)
            )

    def event(self, tid, kind, payload):
        with self.connection() as db:
            cur = db.execute(
                "INSERT INTO events(turn,kind,payload,created) VALUES (?,?,?,?)",
                (tid, kind, dump(payload), time.time()),
            )
            return cur.lastrowid

    def write_turn_draft(self, tid: str, text: str, *, replace: bool = False) -> None:
        """Persist a provisional preview and its replay event in one transaction.

        Draft writes cannot change committed history or resurrect a stopped turn.
        Replacement is required when a tool preamble/rejected reply is discarded.
        """
        with self.connection() as db:
            value = "?" if replace else "draft || ?"
            changed = db.execute(
                f"UPDATE turns SET draft={value} WHERE id=? AND status='running'", (text, tid)
            )
            if changed.rowcount != 1:
                raise Conflict("回合已结束，不能继续更新正文预览")
            db.execute(
                "INSERT INTO events(turn,kind,payload,created) VALUES (?,?,?,?)",
                (tid, "text_reset" if replace else "text", dump({"text": text}), time.time()),
            )

    def events(self, tid, after=0):
        with self.connection() as db:
            return [
                {**dict(r), "payload": json.loads(r["payload"])}
                for r in db.execute("SELECT * FROM events WHERE turn=? AND seq>? ORDER BY seq", (tid, after))
            ]

    def work_turns(self, sid, before=None, limit=20):
        with self.connection() as db:
            if not db.execute("SELECT 1 FROM sessions WHERE id=?", (sid,)).fetchone():
                raise NotFound("会话不存在")
            params = [sid]
            condition = "session=?"
            if before:
                cursor = db.execute(
                    "SELECT created,id FROM turns WHERE id=? AND session=?", (before, sid)
                ).fetchone()
                if cursor is None:
                    raise ValueError("工作记录分页位置无效")
                condition += " AND (created<? OR (created=? AND id<?))"
                params.extend([cursor["created"], cursor["created"], cursor["id"]])
            rows = db.execute(
                f"SELECT id,status,created,request,error FROM turns WHERE {condition} "
                "ORDER BY created DESC,id DESC LIMIT ?",
                (*params, limit + 1),
            ).fetchall()
            total = db.execute("SELECT count(*) FROM turns WHERE session=?", (sid,)).fetchone()[0]
        items = []
        for row in rows[:limit]:
            req = json.loads(row["request"])
            kind = req.get("scene_refresh") or ("maintenance" if req.get("allow_content_edit") else "send")
            items.append(
                {
                    "id": row["id"],
                    "status": row["status"],
                    "created": row["created"],
                    "kind": kind,
                    "mode": req.get("mode", "send"),
                    "text": req.get("text", "")[:120],
                    "error": row["error"],
                }
            )
        return {"items": items, "total": total, "next": items[-1]["id"] if len(rows) > limit else None}

    def work_events(self, sid, tid, after=0, limit=50):
        from tavern.conversation.work_records import background_records, compact_record

        turn = self.turn(tid)
        if turn["session"] != sid:
            raise NotFound("回合不属于当前会话")
        records = [event for event in background_records(self.events(tid)) if event["seq"] > after]
        items = [compact_record(event) for event in records[:limit]]
        return {"items": items, "next": items[-1]["seq"] if len(records) > limit else None}

    def work_event_payload(self, sid, tid, seq):
        turn = self.turn(tid)
        if turn["session"] != sid:
            raise NotFound("回合不属于当前会话")
        with self.connection() as db:
            row = db.execute("SELECT payload FROM events WHERE turn=? AND seq=?", (tid, seq)).fetchone()
        if row is None:
            raise NotFound("工作记录不存在")
        return json.loads(row["payload"])

    def latest_usage(self, sid):
        """Return the newest narrative provider usage for a session, if reported."""
        with self.connection() as db:
            rows = db.execute(
                """SELECT e.payload FROM events e
                   JOIN turns t ON t.id=e.turn
                   WHERE t.session=? AND e.kind='usage'
                   ORDER BY e.seq DESC""",
                (sid,),
            ).fetchall()
        fallback = None
        for row in rows:
            payload = json.loads(row["payload"])
            fallback = fallback or payload.get("usage")
            if payload.get("purpose") == "narrative":
                return payload.get("usage") or {}
        return fallback

    def session_cache_usage(self, sid):
        """All requests owned by this session, including tools and maintenance."""
        with self.connection() as db:
            usages = [
                json.loads(row[0]).get("usage") if row[0] is not None else None
                for row in db.execute(
                    """SELECT e.payload FROM turns t
                          LEFT JOIN events e ON e.turn=t.id AND e.kind='usage'
                          WHERE t.session=? ORDER BY e.seq""",
                    (sid,),
                )
            ]
            for row in db.execute("SELECT payload FROM context_operations WHERE session=?", (sid,)):
                usages.extend(json.loads(row[0]).get("usage", []))
        return aggregate_cache_usage(usages)

    def commit_turn(self, tid, pending):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            turn = db.execute("SELECT * FROM turns WHERE id=?", (tid,)).fetchone()
            if turn["status"] == "committed":
                return
            if turn["status"] not in ("running", "awaiting_review"):
                raise Conflict("回合已取消或结束")
            req = json.loads(turn["request"])
            current = db.execute(
                "SELECT head,cache_breakpoint_follow FROM sessions WHERE id=?", (turn["session"],)
            ).fetchone()
            if "anchor" not in json.loads(current["cache_breakpoint_follow"]):
                self._update_cache_follow(db, turn["session"], current["head"])
            scene = pending.get("scene")
            if scene and not req.get("scene_refresh"):
                raise ValueError("当前回合不允许更新场景档案")
            last = scene["message_id"] if scene else pending["messages"][-1]["id"]
            # Every persisted snapshot keeps only its own archive delta; drafts
            # carry copies of their parent's delta (see memory_archive).
            if scene:
                target = db.execute("SELECT parent FROM messages WHERE id=?", (last,)).fetchone()
                scene = {
                    **scene,
                    "snapshot": settle_message_archive(
                        db,
                        turn["session"],
                        target["parent"] if target else None,
                        copy.deepcopy(scene["snapshot"]),
                    ),
                }
            after = scene["snapshot"] if scene else pending["messages"][-1]["snapshot"]
            r = db.execute(
                "UPDATE sessions SET head=?,revision=revision+1 WHERE id=? AND revision=?",
                (last, turn["session"], req["expected_revision"]),
            )
            if r.rowcount != 1:
                raise Conflict("会话版本冲突；草稿未提交")
            if scene:
                r = db.execute(
                    "UPDATE messages SET snapshot=? WHERE id=? AND session=?",
                    (dump(after), last, turn["session"]),
                )
                if r.rowcount != 1:
                    raise Conflict("场景档案的目标消息不存在")
            for m in pending.get("messages", []):
                settle_message_archive(db, turn["session"], m["parent"], m["snapshot"])
                db.execute(
                    "INSERT INTO messages(id,session,parent,role,raw,model,display,snapshot,created) VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        m["id"],
                        turn["session"],
                        m["parent"],
                        m["role"],
                        m["raw"],
                        m["model"],
                        m["display"],
                        dump(m["snapshot"]),
                        time.time(),
                    ),
                )
                db.execute(
                    "UPDATE messages SET retrieval_query=? WHERE id=?",
                    (m.get("retrieval_query", ""), m["id"]),
                )
                db.execute(
                    "UPDATE messages SET auxiliary=? WHERE id=?", (dump(m.get("auxiliary", [])), m["id"])
                )
            self._update_cache_follow(
                db, turn["session"], last, advance=not scene and req.get("mode", "send") != "regenerate"
            )
            for seq, item in enumerate(pending.get("full_agent_trace", []), 1):
                db.execute(
                    "INSERT INTO full_agent_trace VALUES (?,?,?,?,?)",
                    (turn["session"], last, seq, item["window_id"], dump(item["message"])),
                )
            if record := pending.get("half_agent_record"):
                db.execute(
                    "INSERT INTO half_agent_trace VALUES (?,?,?,?,?)",
                    (turn["session"], last, 1, record["window"], dump(record)),
                )
            if original := pending.get("version_of"):
                from tavern.storage.message_copy import copy_traces

                # A content-only version replays the request that produced its original.
                copy_traces(db, turn["session"], {original: last}, new_story=False)
            db.execute(
                "INSERT INTO changes VALUES (?,?,?,?,?,?)",
                (
                    uid(),
                    tid,
                    last,
                    dump(pending["before"]),
                    dump(after),
                    pending.get("reason", "回合提交"),
                ),
            )
            db.execute("UPDATE turns SET status='committed',pending=NULL WHERE id=?", (tid,))
            db.execute("DELETE FROM full_agent_progress WHERE turn=?", (tid,))
            # The terminal event and authoritative state share one transaction.
            # An observer must never see committed state without its final event.
            db.execute(
                "INSERT INTO events(turn,kind,payload,created) VALUES (?,?,?,?)",
                (tid, "committed", dump({"message_id": last}), time.time()),
            )

    def backup(self, target):
        with self.connection() as source, closing(sqlite3.connect(target)) as dest:
            source.backup(dest)

    def personas(self):
        with self.connection() as db:
            return [dict(r) for r in db.execute("SELECT * FROM personas ORDER BY name")]

    def persona(self, pid):
        with self.connection() as db:
            row = db.execute("SELECT * FROM personas WHERE id=?", (pid,)).fetchone()
        if not row:
            raise NotFound("用户角色不存在")
        return dict(row)

    def delete_persona(self, pid):
        with self.connection() as db:
            if db.execute("DELETE FROM personas WHERE id=?", (pid,)).rowcount != 1:
                raise NotFound("用户角色不存在")

    def save_persona(self, pid, edit, *, db=None):
        if db is None:
            with self.connection() as connection:
                return self.save_persona(pid, edit, db=connection)
        if pid is None:
            pid = uid()
            db.execute("INSERT INTO personas VALUES (?,1,?,?)", (pid, edit.name, edit.description))
        else:
            result = db.execute(
                "UPDATE personas SET name=?,description=?,version=version+1 WHERE id=? AND version=?",
                (edit.name, edit.description, pid, edit.expected_version),
            )
            if result.rowcount != 1:
                raise Conflict("用户角色已被修改，请重新载入")
        return dict(db.execute("SELECT * FROM personas WHERE id=?", (pid,)).fetchone())
