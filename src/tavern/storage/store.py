"""SQLite is the authority; all branch-changing writes use revision checks."""

import copy
import hashlib
import json
import sqlite3
import time
import uuid
from contextlib import closing, contextmanager
from pathlib import Path

from tavern.character.cards import normalize, parse_card
from tavern.core.models import ContextRecipe, TurnRequest
from tavern.core.settings import SettingsDocument, from_runtime, migrate_settings


def uid():
    return uuid.uuid4().hex


def dump(value):
    return json.dumps(value, ensure_ascii=False)


class Conflict(ValueError):
    pass


class Store:
    def __init__(self, root: Path):
        self.root = root
        self.path = root / "tavern.sqlite3"
        migrated = None
        # Validate the file before creating directories, tables or journal files.
        if self.path.exists():
            with closing(sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
                version = (db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
                           if db.execute("SELECT 1 FROM sqlite_master WHERE name='meta'").fetchone() else None)
                if version is None or version[0] not in {"1", "2"}:
                    raise ValueError("数据库版本不受支持；需要 schema 1 或 2")
                raw = json.loads(db.execute("SELECT value FROM meta WHERE key='settings'").fetchone()[0])
                validated_settings = migrate_settings(raw)
                if version[0] == "1":
                    migrated = validated_settings
                    backup_path = root / f"settings-before-v2-{uuid.uuid4().hex[:12]}.sqlite3"
                    with closing(sqlite3.connect(backup_path)) as backup_db:
                        db.backup(backup_db)
                elif raw.get("version") != 2:
                    raise ValueError("设置版本与数据库版本不一致")
                elif any("cache" not in raw.get(scope, {}).get(key, {}) for scope, key in
                         (("full_agent", "model"), ("half_agent", "foreground"),
                          ("half_agent", "background"), ("shared", "card_editor"))):
                    backup_path = root / f"settings-before-cache-{uuid.uuid4().hex[:12]}.sqlite3"
                    with closing(sqlite3.connect(backup_path)) as backup_db:
                        db.backup(backup_db)
                    migrated = validated_settings
        (root / "blobs").mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
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
                    card_data TEXT NOT NULL, lore_injection_mode TEXT NOT NULL, greeting_id TEXT);
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
            db.execute("INSERT OR IGNORE INTO meta VALUES ('settings',?)", (SettingsDocument().model_dump_json(),))
            if migrated is not None:
                db.execute("UPDATE meta SET value=? WHERE key='settings'", (migrated.model_dump_json(),))
            db.execute("INSERT OR REPLACE INTO meta VALUES ('schema_version','2')")
            if "title" not in {row[1] for row in db.execute("PRAGMA table_info(sessions)")}:
                db.execute("ALTER TABLE sessions ADD COLUMN title TEXT NOT NULL DEFAULT ''")
                db.execute("""UPDATE sessions SET title=COALESCE(
                    (SELECT name FROM cards WHERE cards.id=sessions.card),
                    json_extract(card_data,'$.name'), '对话')""")
            session_columns = {row[1] for row in db.execute("PRAGMA table_info(sessions)")}
            if "disabled_tools" not in session_columns:
                db.execute("ALTER TABLE sessions ADD COLUMN disabled_tools TEXT NOT NULL DEFAULT '[]'")
            if "tool_result_options" not in session_columns:
                db.execute("ALTER TABLE sessions ADD COLUMN tool_result_options TEXT NOT NULL DEFAULT '{}'")
            if "cache_breakpoint_auto" not in session_columns:
                db.execute("ALTER TABLE sessions ADD COLUMN cache_breakpoint_auto INTEGER NOT NULL DEFAULT 1")
            if "cache_breakpoint_message" not in session_columns:
                db.execute("ALTER TABLE sessions ADD COLUMN cache_breakpoint_message TEXT")
            if "cache_breakpoint_follow" not in session_columns:
                db.execute("ALTER TABLE sessions ADD COLUMN cache_breakpoint_follow TEXT NOT NULL DEFAULT '{}'")
                for session_row in db.execute("SELECT id,head FROM sessions").fetchall():
                    self._update_cache_follow(db, session_row["id"], session_row["head"], reset=True)
            # Additive schema-1 extension: existing stories remain Half Agent.
            if "agent_mode" not in {r["name"] for r in db.execute("PRAGMA table_info(sessions)")}:
                db.execute("ALTER TABLE sessions ADD COLUMN agent_mode TEXT NOT NULL DEFAULT 'half_agent'")
            db.execute("""CREATE TABLE IF NOT EXISTS full_agent_trace (
                session TEXT NOT NULL, message_id TEXT NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
                seq INTEGER NOT NULL, window_id TEXT NOT NULL, payload TEXT NOT NULL,
                PRIMARY KEY(message_id, seq))""")
            db.execute("CREATE INDEX IF NOT EXISTS full_trace_session ON full_agent_trace(session)")
            db.execute("""CREATE TABLE IF NOT EXISTS full_agent_progress (
                turn TEXT PRIMARY KEY REFERENCES turns(id) ON DELETE CASCADE,
                payload TEXT NOT NULL)""")
        from tavern.character.card_editor_store import initialize_editor_tables
        initialize_editor_tables(self)

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

    def recover(self):
        from tavern.character.card_editor_store import recover_editor_runs
        recover_editor_runs(self)
        with self.connection() as db:
            rows = db.execute("SELECT id FROM turns WHERE status IN ('queued','running')").fetchall()
            db.execute(
                "UPDATE turns SET status='interrupted',error='服务重启，未提交的回合已中断' WHERE status IN ('queued','running')"
            )
        for row in rows:
            self.event(row[0], "interrupted", {"message": "服务重启；草稿保留，未重放脚本"})

    def blob(self, data):
        digest = hashlib.sha256(data).hexdigest()
        path = self.root / "blobs" / digest
        if not path.exists():
            path.write_bytes(data)
        return digest

    def import_card(self, data, filename):
        document, container, resources, report = parse_card(data, filename)
        from tavern.character.card_workflow import validate_document
        validate_document(document)
        card = uid()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            original = self.blob(data)
            mapped = {k: self.blob(v) for k, v in resources.items()}
            db.execute(
                "INSERT INTO cards(id,name,original,container,resources,permission,document,report) VALUES (?,?,?,?,?,0,?,?)",
                (card, normalize(document)["name"], original, container, dump(mapped), dump(document), dump(report)),
            )
        return self.card(card)

    def card(self, card):
        with self.connection() as db:
            c = db.execute("SELECT * FROM cards WHERE id=?", (card,)).fetchone()
            if c is None:
                raise KeyError("角色不存在")
            return {
                **dict(c),
                "document": json.loads(c["document"]),
                "report": json.loads(c["report"]),
                "resources": json.loads(c["resources"]),
            }

    def cards(self):
        with self.connection() as db:
            ids = [x[0] for x in db.execute("SELECT id FROM cards ORDER BY rowid DESC")]
        return [self.card(x) for x in ids]

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
                db.execute("INSERT INTO cards(id,name,original,container,resources,permission,document,report) "
                           "VALUES (?,?,?,?,?,?,?,?)", (new_id, name, source["original"], source["container"],
                           dump(source["resources"]), source["permission"], dump(document), dump(diagnose(document))))
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
                raise KeyError("角色卡不存在或已删除")
            if db.execute("SELECT 1 FROM turns t JOIN sessions s ON t.session=s.id WHERE s.card=? "
                          "AND t.status IN ('queued','running','awaiting_review')", (cid,)).fetchone():
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
                raise KeyError("会话不存在")
        return json.loads(row["card_data"])

    def card_manifest_path(self, card):
        filename = f"session-{card['session_id']}" if card.get("session_id") else card["id"]
        directory = (self.root / "dependencies").resolve()
        path = directory / f"{filename}.json"
        if path.resolve().parent != directory:
            raise ValueError("角色依赖缓存路径无效")
        return path

    def card_manifest(self, card):
        path = self.card_manifest_path(card)
        manifest = (json.loads(path.read_text(encoding="utf-8")) if path.exists() else
                    copy.deepcopy(card.get("dependencies", {"resources": {}, "scripts": {}})))
        if card.get("session_id"):
            return manifest
        manifest["resources"] = {url: resource for url, resource in manifest["resources"].items()
                                 if (self.root / "blobs" / resource["hash"]).is_file()}
        manifest["scripts"] = {source: blob for source, blob in manifest["scripts"].items()
                               if (self.root / "blobs" / blob).is_file()}
        # A session may have resolved resources after the library card was imported.
        # Reuse its pinned blobs when creating another conversation from that card.
        with self.connection() as db:
            siblings = [row[0] for row in db.execute(
                "SELECT id FROM sessions WHERE card=? ORDER BY created DESC", (card["id"],))]
        for sid in siblings:
            sibling_path = self.card_manifest_path({"session_id": sid})
            if not sibling_path.exists():
                continue
            try:
                sibling = json.loads(sibling_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
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
            current = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"resources": {}, "scripts": {}}
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
                raise KeyError("角色卡不存在或已删除")
            if expected_revision is not None and current[0] != expected_revision:
                raise Conflict("角色卡已改变；草稿已保留，请重新载入或另存副本")
            result = db.execute(
                "UPDATE cards SET name=?,document=?,report=?,revision=revision+1 WHERE id=?",
                (normalize(document)["name"], dump(document), dump(diagnose(document)), card),
            )
            if result.rowcount != 1:
                raise KeyError("角色卡不存在或已删除")
        return self.card(card)

    def settings_document(self):
        with self.connection() as db:
            return SettingsDocument.model_validate_json(
                db.execute("SELECT value FROM meta WHERE key='settings'").fetchone()[0]
            )

    def settings(self, mode="half_agent"):
        return self.settings_document().runtime(mode)

    def save_settings(self, settings):
        document = settings if isinstance(settings, SettingsDocument) else from_runtime(settings)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            current = SettingsDocument.model_validate_json(db.execute("SELECT value FROM meta WHERE key='settings'").fetchone()[0])
            for scope in document.revisions:
                if getattr(current, scope) != getattr(document, scope):
                    document.revisions[scope] = current.revisions[scope] + 1
            db.execute("UPDATE meta SET value=? WHERE key='settings'", (document.model_dump_json(),))

    def new_session(self, options):
        card = self.card(options.card_id)
        data = normalize(card["document"])
        greetings = [data.get("first_mes", ""), *data.get("alternate_greetings", [])]
        if not 0 <= options.greeting < len(greetings):
            raise ValueError("开场白不存在")
        sid, mid = uid(), uid()
        snapshot = {"state": {}, "memory_document": {"version": 1, "records": []}, "lore": {}, "scopes": {}, "content": None}
        if options.agent_mode == "full_agent":
            from tavern.full_agent.state import new_state
            snapshot.pop("memory_document")
            snapshot["full_agent"] = new_state()
        from tavern.character.card_workflow import validate_document, workflow
        validate_document(card["document"])
        metadata = workflow(card["document"])
        greeting_id = metadata["greetings"][options.greeting]["id"] if metadata else None
        raw = greetings[options.greeting]
        persona_data = self.persona(options.persona_id) if options.persona_id else {}
        persona_name = persona_data.get("name", options.persona)
        recipe = self.settings().context_recipe.model_copy(deep=True)
        dependencies = self.card_manifest(card)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute("SELECT 1 FROM cards WHERE id=?", (card["id"],)).fetchone():
                raise KeyError("角色卡不存在或已删除")
            db.execute(
                "INSERT INTO sessions(id,card,head,revision,persona,policy,recipe,created,title,persona_data,lore_injection_mode,card_data,greeting_id) VALUES (?,?,?,0,?,?,?,?,?,?,?,?,?)",
                (
                    sid,
                    card["id"],
                    mid,
                    persona_name,
                    options.policy,
                    recipe.model_dump_json(),
                    time.time(),
                    card["name"],
                    dump(persona_data),
                    self.settings().lore_injection_mode,
                    dump({**card, "session_id": sid, "dependencies": dependencies}),
                    greeting_id,
                ),
            )
            db.execute(
                "INSERT INTO messages(id,session,parent,role,raw,model,display,snapshot,created) VALUES (?,?,?,?,?,?,?,?,?)",
                (mid, sid, None, "assistant", raw, raw, raw, dump(snapshot), time.time()),
            )
            db.execute("UPDATE sessions SET agent_mode=?,lore_injection_mode=? WHERE id=?",
                       (options.agent_mode, "conversation" if options.agent_mode == "full_agent"
                        else self.settings().lore_injection_mode, sid))
        return self.session(sid)

    def fork_session(self, sid, head, expected_revision):
        """Copy a committed assistant prefix into a session with independent message IDs."""
        from tavern.memories.memory import fingerprint, window_valid

        new_sid = uid()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            source = db.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
            if source is None:
                raise KeyError("会话不存在")
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
                prefix.append(message)
                seen.add(cursor)
                cursor = message["parent"]
            prefix.reverse()
            card_data = json.loads(source["card_data"]) if source["card_data"] else None
            if card_data:
                card_data["dependencies"] = self.card_manifest(card_data)
                card_data["session_id"] = new_sid
            base_title = (source["title"] or (card_data or {}).get("name") or "对话")[:90]
            title = f"{base_title} · 副本"
            index = 2
            while db.execute("SELECT 1 FROM sessions WHERE title=?", (title,)).fetchone():
                title = f"{base_title} · 副本 {index}"
                index += 1
            ids = {m["id"]: uid() for m in prefix}
            copies = []
            for index, message in enumerate(prefix):
                cloned = copy.deepcopy(message)
                cloned.update(id=ids[message["id"]], session=new_sid, parent=ids.get(message["parent"]))
                from tavern.full_agent.state import remap_sources
                remap_sources(cloned["snapshot"], ids)
                for memory in cloned["snapshot"].get("memory_document", {}).get("records", []):
                    for source_ref in memory.get("sources", []) + memory.get("deleted_sources", []):
                        source_ref["message_id"] = ids.get(source_ref["message_id"], source_ref["message_id"])
                    for setting_ref in memory.get("setting_sources", []):
                        setting_ref["anchor"] = ids.get(setting_ref.get("anchor"), setting_ref.get("anchor"))
                for record in cloned["snapshot"].get("lore_injections", []):
                    record["anchor_id"] = ids.get(record["anchor_id"], record["anchor_id"])
                    record["source_ids"] = [ids.get(mid, mid) for mid in record.get("source_ids", [])]
                for archive_key in ("context_window", "memory_archive"):
                    window = cloned["snapshot"].get(archive_key)
                    if not window:
                        continue
                    if window_valid(window, prefix[:index + 1]):
                        window["fingerprint"] = fingerprint((copies + [cloned])[:window["covered_count"]])
                    else:
                        cloned["snapshot"].pop(archive_key)
                copies.append(cloned)
            db.execute(
                "INSERT INTO sessions(id,card,head,revision,persona,policy,recipe,created,title,persona_data,turn_preference,card_data,lore_injection_mode,cache_breakpoint_auto,cache_breakpoint_message) "
                "VALUES (?,?,?,0,?,?,?,?,?,?,?,?,?,?,?)",
                (new_sid, source["card"], ids[head], source["persona"],
                 source["policy"], source["recipe"], time.time(), title, source["persona_data"], source["turn_preference"],
                 dump(card_data) if card_data else None, source["lore_injection_mode"],
                 source["cache_breakpoint_auto"], ids.get(source["cache_breakpoint_message"], source["cache_breakpoint_message"])),
            )
            db.executemany(
                "INSERT INTO messages(id,session,parent,role,raw,model,display,snapshot,created) VALUES (?,?,?,?,?,?,?,?,?)",
                [(m["id"], new_sid, m["parent"], m["role"], m["raw"], m["model"], m["display"],
                  dump(m["snapshot"]), m["created"]) for m in copies],
            )
            for m in copies:
                db.execute("UPDATE messages SET retrieval_query=? WHERE id=?", (m.get("retrieval_query", ""), m["id"]))
            db.execute("UPDATE sessions SET agent_mode=? WHERE id=?", (source["agent_mode"], new_sid))
            db.execute("UPDATE sessions SET disabled_tools=? WHERE id=?", (source["disabled_tools"], new_sid))
            db.execute("UPDATE sessions SET tool_result_options=? WHERE id=?",
                       (source["tool_result_options"], new_sid))
            from tavern.conversation.cache_breakpoint import reset_follow
            follow = reset_follow(copies, json.loads(source["cache_breakpoint_follow"]))
            db.execute("UPDATE sessions SET cache_breakpoint_follow=? WHERE id=?", (dump(follow), new_sid))
            for original, cloned_id in ids.items():
                db.execute("""INSERT INTO full_agent_trace(session,message_id,seq,window_id,payload)
                    SELECT ?,?,seq,window_id,payload FROM full_agent_trace WHERE message_id=?""",
                           (new_sid, cloned_id, original))
        return self.session(new_sid)

    def session(self, sid):
        with self.connection() as db:
            row = db.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
            if row is None:
                raise KeyError("会话不存在")
            turns = [
                dict(r)
                for r in db.execute(
                    "SELECT id,status,draft,error,created FROM turns WHERE session=? ORDER BY created DESC LIMIT 10",
                    (sid,),
                )
            ]
            result = dict(row)
            result["recipe"] = ContextRecipe.model_validate_json(result["recipe"]).model_dump()
            result["persona_data"] = json.loads(result["persona_data"])
            result["disabled_tools"] = json.loads(result["disabled_tools"])
            result["tool_result_options"] = json.loads(result["tool_result_options"])
            result["card_data"] = json.loads(result["card_data"]) if result["card_data"] else None
            result["turns"] = turns
        result["messages"] = self.history(sid, result["head"])
        result["snapshot"] = (
            copy.deepcopy(result["messages"][-1]["snapshot"])
            if result["messages"]
            else {"state": {}, "memory_document": {"version": 1, "records": []}, "lore": {}, "scopes": {}, "content": None}
        )
        settings = self.settings()
        settings = self.settings(result["agent_mode"])
        result["policy_effective"] = result["policy"] or settings.policy
        result["cache_breakpoint_auto"] = bool(result["cache_breakpoint_auto"])
        from tavern.conversation.cache_breakpoint import (
            advance_follow,
            breakpoint_reason,
            effective_message_id,
        )
        result["cache_breakpoint_follow"] = advance_follow(
            result["messages"], json.loads(result["cache_breakpoint_follow"]))
        result["cache_breakpoint_effective_id"] = effective_message_id(
            result, settings.for_role("full_agent" if result["agent_mode"] == "full_agent" else "foreground"))
        result["cache_breakpoint_reason"] = breakpoint_reason(result, result["cache_breakpoint_effective_id"])
        from tavern.memories.memory_records import read_memory, with_source_status
        result["memory_view"] = with_source_status(read_memory(result["snapshot"]), result["messages"])
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
                raise KeyError("会话不存在")
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
                raise KeyError("会话不存在")
            db.execute("DELETE FROM changes WHERE turn IN (SELECT id FROM turns WHERE session=?)", (sid,))
            db.execute("DELETE FROM events WHERE turn IN (SELECT id FROM turns WHERE session=?)", (sid,))
            db.execute("DELETE FROM turns WHERE session=?", (sid,))
            db.execute("DELETE FROM full_agent_trace WHERE session=?", (sid,))
            db.execute("DELETE FROM messages WHERE session=?", (sid,))
            db.execute("DELETE FROM sessions WHERE id=?", (sid,))
            from tavern.storage.retrieval_cleanup import prune_orphan_retrieval
            prune_orphan_retrieval(db)
        if card:
            self.prune_card_assets(card, manifest)

    def history(self, sid, head):
        with self.connection() as db:
            rows = {r["id"]: dict(r) for r in db.execute(
                "SELECT * FROM messages WHERE session=? ORDER BY created,rowid", (sid,))}
        versions = {}
        for row in rows.values():
            if row["role"] == "assistant":
                versions.setdefault(row["parent"], []).append(row["id"])
        result, seen = [], set()
        while head:
            if head not in rows or head in seen:
                raise ValueError("剧情分支无效")
            row = rows[head]
            row["snapshot"] = json.loads(row["snapshot"])
            if row["role"] == "assistant":
                row["versions"] = versions[row["parent"]]
                row["version_index"] = row["versions"].index(row["id"])
            result.append(row)
            seen.add(head)
            head = row["parent"]
        return list(reversed(result))

    def switch_message_version(self, sid, mid, target, expected_revision):
        session = self.session(sid)
        if session["revision"] != expected_revision:
            raise Conflict("会话已改变，请刷新后重试")
        latest = session["messages"][-1] if session["messages"] else None
        if not latest or latest["role"] != "assistant" or latest["id"] != mid:
            raise ValueError("只能切换最新 AI 回复的版本")
        if target not in latest["versions"]:
            raise ValueError("目标版本不属于这条 AI 回复")
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
            session = db.execute("SELECT head,revision FROM sessions WHERE id=?", (sid,)).fetchone()
            if session is None:
                raise KeyError("会话不存在")
            if session["revision"] != expected_revision:
                raise Conflict("会话已改变，请刷新后重试")
            if db.execute("SELECT 1 FROM turns WHERE session=? AND status IN ('running','queued','awaiting_review')",
                          (sid,)).fetchone():
                raise Conflict("请先完成或取消当前回合")
            rows = {r["id"]: r for r in db.execute("SELECT id,parent,snapshot FROM messages WHERE session=?", (sid,))}
            branch, mid = [], session["head"]
            while mid:
                branch.append(rows[mid])
                mid = rows[mid]["parent"]
            if anchor_id not in {r["id"] for r in branch}:
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

    def edit_message(self, sid, mid, text, expected_revision, *, model=None, display=None):
        if not text.strip():
            raise ValueError("消息不能为空")
        session = self.session(sid)
        branch = session["messages"]
        from tavern.memories.memory import fingerprint, window_valid
        archive = session["snapshot"].get("memory_archive")
        affected_memory = bool(window_valid(archive, branch) and any(
            message["id"] == mid for message in branch[:archive["covered_count"]]
        ))
        active_ids = {message["id"] for message in branch}
        if mid not in active_ids:
            raise ValueError("只能编辑当前会话分支中的消息")
        affected = {m["id"] for m in branch[next(i for i, m in enumerate(branch) if m["id"] == mid):]}
        with self.connection() as db:
            active = db.execute(
                "SELECT 1 FROM turns WHERE session=? AND status IN ('running','queued','awaiting_review')",
                (sid,),
            ).fetchone()
            if active:
                raise Conflict("请先完成或取消当前回合")
            revision = db.execute("SELECT revision FROM sessions WHERE id=?", (sid,)).fetchone()
            if revision is None:
                raise KeyError("会话不存在")
            if revision[0] != expected_revision:
                raise Conflict("会话已改变，请刷新后重试")
            rows = db.execute(
                "SELECT id,parent,role,raw,model,display,snapshot FROM messages WHERE session=?", (sid,)
            ).fetchall()
            messages = {row["id"]: dict(row) for row in rows}
            original_messages = copy.deepcopy(messages)
            result = db.execute(
                "UPDATE messages SET raw=?,model=?,display=? WHERE id=? AND session=?",
                (text, text if model is None else model, text if display is None else display, mid, sid),
            )
            if result.rowcount != 1:
                raise KeyError("消息不存在")
            messages[mid].update(
                raw=text,
                model=text if model is None else model,
                display=text if display is None else display,
            )
            # The visible-history cursor may describe the message before this edit.
            # Keep the user's current memory intact: an edit is not permission to roll it back
            # or silently regenerate it. Rebase verified memory coverage onto the edited source
            # so ordinary sends do not turn the notification into an automatic memory rewrite.
            def lineage(head, source):
                result, seen = [], set()
                while head:
                    if head not in source or head in seen:
                        raise ValueError("剧情分支无效")
                    message = source[head]
                    result.append(message)
                    seen.add(head)
                    head = message["parent"]
                return list(reversed(result))

            for row in rows:
                snapshot = json.loads(row["snapshot"])
                old_history = lineage(row["id"], original_messages)
                history = lineage(row["id"], messages)
                from tavern.full_agent.state import mark_edited
                mark_edited(snapshot, mid, history)
                if snapshot.get("memory_document"):
                    from tavern.memories.memory_records import with_source_status
                    # Preserve the user's archive; this is a warning, not an
                    # instruction to rerun Tracker on already covered text.
                    snapshot["memory_document"] = with_source_status(snapshot["memory_document"], history)
                memory_archive = snapshot.get("memory_archive")
                if memory_archive and window_valid(memory_archive, old_history):
                    count = memory_archive["covered_count"]
                    if any(message["id"] == mid for message in history[:count]):
                        memory_archive["fingerprint"] = fingerprint(history[:count])
                for key in ("context_window",):
                    snapshot.pop(key, None)
                if "lore_injections" in snapshot:
                    snapshot["lore_injections"] = [
                        record for record in snapshot["lore_injections"]
                        if record["anchor_id"] not in affected
                        and mid not in record.get("source_ids", [])
                    ]
                db.execute("UPDATE messages SET snapshot=? WHERE id=?", (dump(snapshot), row["id"]))
            db.execute("UPDATE messages SET retrieval_query='' WHERE session=?", (sid,))
            db.execute("UPDATE sessions SET revision=revision+1 WHERE id=?", (sid,))
        result = self.session(sid)
        result["edit_affected_memory"] = affected_memory
        return result

    def update_session(self, sid, expected, **fields):
        if not fields or not set(fields).issubset({"head", "persona", "policy", "recipe", "persona_data", "turn_preference", "title",
                                                  "cache_breakpoint_auto", "cache_breakpoint_message", "cache_breakpoint_follow"}):
            raise ValueError("无效会话字段")
        if "title" in fields:
            title = fields["title"].strip()
            if not title or len(title) > 100:
                raise ValueError("对话标题须为 1 到 100 个字符")
            fields["title"] = title
        if fields.get("head"):
            self.history(sid, fields["head"])
        with self.connection() as db:
            active = db.execute(
                "SELECT 1 FROM turns WHERE session=? AND status IN ('running','queued','awaiting_review')",
                (sid,),
            ).fetchone()
            if active:
                raise Conflict("请先完成或取消当前回合")
            assignments = ",".join(f"{k}=?" for k in fields)
            r = db.execute(
                f"UPDATE sessions SET {assignments},revision=revision+1 WHERE id=? AND revision=?",
                (*fields.values(), sid, expected),
            )
            if r.rowcount != 1:
                raise Conflict("会话已改变，请刷新后重试")
            if fields.get("head"):
                self._update_cache_follow(db, sid, fields["head"])
        return self.session(sid)

    def set_cache_breakpoint(self, sid, expected, automatic, message_id, follow_interval=None, reply_offset=None):
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
        return self.update_session(sid, expected, cache_breakpoint_auto=int(automatic),
                                   cache_breakpoint_message=None if automatic else message_id,
                                   cache_breakpoint_follow=dump(follow))

    def _update_cache_follow(self, db, sid, head, *, advance=False, reset=False):
        from tavern.conversation.cache_breakpoint import advance_follow, reply_ids, reset_follow
        row = db.execute("SELECT cache_breakpoint_follow,cache_breakpoint_auto FROM sessions WHERE id=?", (sid,)).fetchone()
        follow = json.loads(row["cache_breakpoint_follow"])
        messages = {message["id"]: dict(message) for message in db.execute(
            "SELECT id,parent,role FROM messages WHERE session=?", (sid,))}
        history, seen = [], set()
        while head and head in messages and head not in seen:
            seen.add(head)
            history.append(messages[head])
            head = messages[head]["parent"]
        history.reverse()
        follow = (reset_follow(history, follow) if reset else
                  advance_follow(history, follow, advance and bool(row["cache_breakpoint_auto"])
                                 and bool(history) and history[-1]["id"] in reply_ids(history)))
        db.execute("UPDATE sessions SET cache_breakpoint_follow=? WHERE id=?", (dump(follow), sid))


    def edit_memory_record(self, sid, edit):
        from tavern.memories.memory_records import install_memory, manual_edit, read_memory
        session = self.session(sid)
        if not session["head"]:
            raise ValueError("当前分支没有可保存档案的消息")
        snapshot = copy.deepcopy(session["snapshot"])
        memory = manual_edit(read_memory(snapshot), edit)
        if edit.action != "delete":
            from tavern.memories.memory_retention import retain_memory
            memory = retain_memory(memory, session["messages"], self.settings().for_role("background"))
        install_memory(snapshot, memory)
        return self._save_memory_snapshot(sid, session, snapshot, edit.expected_revision)

    def _save_memory_snapshot(self, sid, session, snapshot, expected_revision):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM turns WHERE session=? AND status IN "
                          "('queued','running','awaiting_review')", (sid,)).fetchone():
                raise Conflict("请先完成或取消当前回合")
            result = db.execute("UPDATE sessions SET revision=revision+1 WHERE id=? AND revision=? AND head=?",
                                (sid, expected_revision, session["head"]))
            if result.rowcount != 1:
                raise Conflict("会话已改变，请刷新后重试")
            db.execute("UPDATE messages SET snapshot=? WHERE id=? AND session=?",
                       (dump(snapshot), session["head"], sid))
        return self.session(sid)

    def create_turn(self, sid, request):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT id,request FROM turns WHERE session=? AND operation=?", (sid, request.operation_id)
            ).fetchone()
            if existing:
                if TurnRequest.model_validate_json(existing["request"]).model_dump() != request.model_dump():
                    raise Conflict("相同 operation_id 不能用于不同请求")
                return existing["id"], False
            session = db.execute("SELECT revision,agent_mode FROM sessions WHERE id=?", (sid,)).fetchone()
            if not session:
                raise KeyError("会话不存在")
            if session[0] != request.expected_revision:
                raise Conflict("会话已改变，请刷新后重试")
            if db.execute(
                "SELECT 1 FROM turns WHERE session=? AND status IN ('queued','running','awaiting_review')",
                (sid,),
            ).fetchone():
                raise Conflict("当前会话已有运行中的回合")
            if request.resume_turn_id:
                if session["agent_mode"] != "full_agent":
                    raise Conflict("只有 Full Agent 回合可续接工具调用")
                source = db.execute("SELECT session,status,request FROM turns WHERE id=?",
                                    (request.resume_turn_id,)).fetchone()
                latest = db.execute("SELECT id FROM turns WHERE session=? ORDER BY created DESC,rowid DESC LIMIT 1",
                                    (sid,)).fetchone()
                if (source is None or source["session"] != sid or source["status"] not in
                        ("error", "cancelled", "interrupted") or latest["id"] != request.resume_turn_id):
                    raise Conflict("只能续接当前会话最新的失败或中断回合")
                previous = TurnRequest.model_validate_json(source["request"])
                matching = ("text", "expected_revision", "mode", "message_id",
                            "allow_content_edit", "update_variables", "generate", "scene_refresh")
                if any(getattr(previous, field) != getattr(request, field) for field in matching):
                    raise Conflict("续接请求的输入或授权已改变，请重新开始本轮")
            tid = uid()
            db.execute(
                "INSERT INTO turns(id,session,operation,request,status,created) VALUES (?,?,?,?,?,?)",
                (tid, sid, request.operation_id, request.model_dump_json(), "queued", time.time()),
            )
            if request.resume_turn_id:
                row = db.execute("SELECT payload FROM full_agent_progress WHERE turn=?",
                                 (request.resume_turn_id,)).fetchone()
                if row:
                    db.execute("INSERT INTO full_agent_progress(turn,payload) VALUES (?,?)", (tid, row["payload"]))
                    trace = json.loads(row["payload"])["new_trace"]
                    for index, item in enumerate(trace):
                        message = item["message"]
                        if message["role"] != "assistant":
                            continue
                        assistant_seq = index + 1
                        if message.get("content"):
                            db.execute("INSERT INTO events(turn,kind,payload,created) VALUES (?,?,?,?)",
                                (tid, "full_agent_assistant", dump({"activity_id": f"text-{assistant_seq}",
                                 "order": assistant_seq * 10, "content": message["content"]}), time.time()))
                        results = {}
                        for following in trace[index + 1:]:
                            part = following["message"]
                            if part["role"] == "assistant":
                                break
                            if part["role"] == "tool":
                                results[part["tool_call_id"]] = part["content"]
                        for call_index, call in enumerate(message.get("tool_calls", [])):
                            activity = {"activity_id": f"call-{assistant_seq}-{call_index}",
                                "order": assistant_seq * 10 + call_index + 1,
                                "name": call["function"]["name"],
                                "arguments": call["function"]["arguments"]}
                            result = results.get(call["id"])
                            if result is None:
                                continue
                            db.execute("INSERT INTO events(turn,kind,payload,created) VALUES (?,?,?,?)",
                                (tid, "full_agent_tool_started", dump(activity), time.time()))
                            db.execute("INSERT INTO events(turn,kind,payload,created) VALUES (?,?,?,?)",
                                (tid, "full_agent_tool", dump({**activity, "result": json.loads(result)}), time.time()))
        return tid, True

    def full_agent_progress(self, tid):
        with self.connection() as db:
            row = db.execute("SELECT payload FROM full_agent_progress WHERE turn=?", (tid,)).fetchone()
        return json.loads(row["payload"]) if row else None

    def save_full_agent_progress(self, tid, progress):
        with self.connection() as db:
            db.execute("INSERT OR REPLACE INTO full_agent_progress(turn,payload) VALUES (?,?)",
                       (tid, dump(progress)))

    def turn(self, tid):
        with self.connection() as db:
            r = db.execute("SELECT * FROM turns WHERE id=?", (tid,)).fetchone()
            if r is None:
                raise KeyError("回合不存在")
        result = dict(r)
        for key in ("request", "preview", "pending"):
            result[key] = json.loads(result[key]) if result[key] else None
        return result

    def edit_pending_input(self, tid, text, expected_revision):
        if not text.strip():
            raise ValueError("消息不能为空")
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM turns WHERE id=?", (tid,)).fetchone()
            if row is None:
                raise KeyError("回合不存在")
            request = json.loads(row["request"])
            session = db.execute("SELECT revision FROM sessions WHERE id=?", (row["session"],)).fetchone()
            latest = db.execute("SELECT id FROM turns WHERE session=? ORDER BY created DESC, rowid DESC LIMIT 1", (row["session"],)).fetchone()
            if (row["status"] not in ("error", "cancelled", "interrupted")
                    or request.get("mode", "send") != "send" or request.get("scene_refresh")
                    or latest["id"] != tid or session["revision"] != expected_revision
                    or request["expected_revision"] != expected_revision):
                raise Conflict("只能编辑当前会话最新的失败或取消发言")
            request["text"] = text
            db.execute("UPDATE turns SET request=?,draft='',pending=NULL WHERE id=?", (dump(request), tid))
            db.execute("DELETE FROM full_agent_progress WHERE turn=?", (tid,))
        return self.turn(tid)

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

    def events(self, tid, after=0):
        with self.connection() as db:
            return [
                {**dict(r), "payload": json.loads(r["payload"])}
                for r in db.execute("SELECT * FROM events WHERE turn=? AND seq>? ORDER BY seq", (tid, after))
            ]

    def work_turns(self, sid, before=None, limit=20):
        with self.connection() as db:
            if not db.execute("SELECT 1 FROM sessions WHERE id=?", (sid,)).fetchone():
                raise KeyError("会话不存在")
            params = [sid]
            condition = "session=?"
            if before:
                cursor = db.execute("SELECT created,id FROM turns WHERE id=? AND session=?", (before, sid)).fetchone()
                if cursor is None:
                    raise ValueError("工作记录分页位置无效")
                condition += " AND (created<? OR (created=? AND id<?))"
                params.extend([cursor["created"], cursor["created"], cursor["id"]])
            rows = db.execute(f"SELECT id,status,created,request,error FROM turns WHERE {condition} "
                              "ORDER BY created DESC,id DESC LIMIT ?", (*params, limit + 1)).fetchall()
            total = db.execute("SELECT count(*) FROM turns WHERE session=?", (sid,)).fetchone()[0]
        items = []
        for row in rows[:limit]:
            req = json.loads(row["request"])
            kind = req.get("scene_refresh") or ("maintenance" if req.get("allow_content_edit") else "send")
            items.append({"id": row["id"], "status": row["status"], "created": row["created"], "kind": kind,
                          "mode": req.get("mode", "send"), "text": req.get("text", "")[:120], "error": row["error"]})
        return {"items": items, "total": total, "next": items[-1]["id"] if len(rows) > limit else None}

    def work_events(self, sid, tid, after=0, limit=50):
        from tavern.conversation.work_records import background_records, compact_record

        turn = self.turn(tid)
        if turn["session"] != sid:
            raise KeyError("回合不属于当前会话")
        records = [event for event in background_records(self.events(tid)) if event["seq"] > after]
        items = [compact_record(event) for event in records[:limit]]
        return {"items": items, "next": items[-1]["seq"] if len(records) > limit else None}

    def work_event_payload(self, sid, tid, seq):
        turn = self.turn(tid)
        if turn["session"] != sid:
            raise KeyError("回合不属于当前会话")
        with self.connection() as db:
            row = db.execute("SELECT payload FROM events WHERE turn=? AND seq=?", (tid, seq)).fetchone()
        if row is None:
            raise KeyError("工作记录不存在")
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

    def commit_turn(self, tid, pending):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            turn = db.execute("SELECT * FROM turns WHERE id=?", (tid,)).fetchone()
            if turn["status"] == "committed":
                return
            if turn["status"] not in ("running", "awaiting_review"):
                raise Conflict("回合已取消或结束")
            req = json.loads(turn["request"])
            current = db.execute("SELECT head,cache_breakpoint_follow FROM sessions WHERE id=?", (turn["session"],)).fetchone()
            if "anchor" not in json.loads(current["cache_breakpoint_follow"]):
                self._update_cache_follow(db, turn["session"], current["head"])
            scene = pending.get("scene")
            if scene and not req.get("scene_refresh"):
                raise ValueError("当前回合不允许更新场景档案")
            last = scene["message_id"] if scene else pending["messages"][-1]["id"]
            after = scene["snapshot"] if scene else pending["messages"][-1]["snapshot"]
            r = db.execute(
                "UPDATE sessions SET head=?,revision=revision+1 WHERE id=? AND revision=?",
                (last, turn["session"], req["expected_revision"]),
            )
            if r.rowcount != 1:
                raise Conflict("会话版本冲突；草稿未提交")
            if scene:
                r = db.execute("UPDATE messages SET snapshot=? WHERE id=? AND session=?",
                               (dump(after), last, turn["session"]))
                if r.rowcount != 1:
                    raise Conflict("场景档案的目标消息不存在")
            for m in pending.get("messages", []):
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
                db.execute("UPDATE messages SET retrieval_query=? WHERE id=?",
                           (m.get("retrieval_query", ""), m["id"]))
            self._update_cache_follow(db, turn["session"], last,
                                      advance=not scene and req.get("mode", "send") != "regenerate")
            for seq, item in enumerate(pending.get("full_agent_trace", []), 1):
                db.execute("INSERT INTO full_agent_trace VALUES (?,?,?,?,?)",
                           (turn["session"], last, seq, item["window_id"], dump(item["message"])))
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
            db.execute("INSERT INTO events(turn,kind,payload,created) VALUES (?,?,?,?)",
                       (tid, "committed", dump({"message_id": last}), time.time()))

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
            raise KeyError("玩家角色不存在")
        return dict(row)

    def delete_persona(self, pid):
        with self.connection() as db:
            if db.execute("DELETE FROM personas WHERE id=?", (pid,)).rowcount != 1:
                raise KeyError("玩家角色不存在")

    def save_persona(self, pid, edit):
        with self.connection() as db:
            if pid is None:
                pid = uid()
                db.execute("INSERT INTO personas VALUES (?,1,?,?)", (pid, edit.name, edit.description))
            else:
                result = db.execute(
                    "UPDATE personas SET name=?,description=?,version=version+1 WHERE id=? AND version=?",
                    (edit.name, edit.description, pid, edit.expected_version),
                )
                if result.rowcount != 1:
                    raise Conflict("玩家角色已被修改，请重新载入")
        return self.persona(pid)
