"""Card Author conversation storage; pending progress never replaces committed cards."""
import copy
import json
import time

from tavern.character.card_workflow import validate_document, validation_report
from tavern.character.cards import diagnose, normalize
from tavern.character.editor_environment import capture_environment
from tavern.core.model_view import ModelView
from tavern.storage.store import Conflict, dump, uid


def initialize(store):
    with store.connection() as db:
        db.executescript("""BEGIN IMMEDIATE;
            CREATE TABLE IF NOT EXISTS card_author_conversations(
                id TEXT PRIMARY KEY, revision INTEGER NOT NULL DEFAULT 0, title TEXT NOT NULL DEFAULT '',
                payload TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL, next_ref INTEGER NOT NULL DEFAULT 1);
            CREATE TABLE IF NOT EXISTS card_author_progress(workspace TEXT PRIMARY KEY, run TEXT NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS card_author_messages(workspace TEXT NOT NULL, seq INTEGER NOT NULL,
                role TEXT NOT NULL, content TEXT NOT NULL, run TEXT NOT NULL, PRIMARY KEY(workspace,seq));
            CREATE TABLE IF NOT EXISTS card_author_traces(run TEXT PRIMARY KEY, payload TEXT NOT NULL);
        """)
        columns = {r[1] for r in db.execute("PRAGMA table_info(card_author_conversations)")}
        if "channel_origin" not in columns:
            db.execute("ALTER TABLE card_author_conversations ADD COLUMN channel_origin TEXT")
            db.execute("ALTER TABLE card_author_conversations ADD COLUMN model_selection TEXT NOT NULL DEFAULT '{}'")
        if "ref" not in {r[1] for r in db.execute("PRAGMA table_info(card_edit_versions)")}:
            db.execute("ALTER TABLE card_edit_versions ADD COLUMN ref TEXT NOT NULL DEFAULT 'C1'")
        if "snapshot" not in {r[1] for r in db.execute("PRAGMA table_info(card_edit_versions)")}:
            db.execute("ALTER TABLE card_edit_versions ADD COLUMN snapshot TEXT")
        for row in db.execute("SELECT * FROM card_edits").fetchall():
            item = {key: json.loads(row[key]) for key in ("source", "base_document", "document")}
            item.update(ref="C1", card=row["card"], base_revision=row["base_revision"], kind="draft", target_mode=None, brief="")
            db.execute("INSERT OR IGNORE INTO card_author_conversations(id,revision,title,payload,created,updated,next_ref) VALUES (?,?, '', ?, ?, ?, 2)",
                       (row["id"], row["revision"], dump({"cards": [item], "state": {}}), row["created"], row["created"]))
            seq = 0
            for run in db.execute("SELECT * FROM card_edit_runs WHERE workspace=? ORDER BY created,rowid", (row["id"],)):
                for role, text in (("user", json.loads(run["request"]).get("text", "")), ("assistant", run["response"])):
                    if text:
                        seq += 1
                        db.execute("INSERT OR IGNORE INTO card_author_messages VALUES (?,?,?,?,?)", (row["id"], seq, role, text, run["id"]))
            context = db.execute("SELECT payload FROM card_edit_contexts WHERE workspace=?", (row["id"],)).fetchone()
            latest = db.execute("SELECT id FROM card_edit_runs WHERE workspace=? AND status='completed' ORDER BY created DESC LIMIT 1", (row["id"],)).fetchone()
            if context and latest:
                record = json.loads(context[0])
                view = ModelView()
                view.reverse = record.get("view", {}).get("reverse", {})
                view.reserved = set(record.get("view", {}).get("reserved", []))
                messages = copy.deepcopy(record.get("messages", []))
                for message in messages:
                    if view.reverse:
                        if isinstance(message.get("content"), str):
                            message["content"] = view.restore(message["content"])
                        for call in message.get("tool_calls", []):
                            call["function"]["arguments"] = view.restore(call["function"]["arguments"])
                db.execute("INSERT OR IGNORE INTO card_author_traces VALUES (?,?)", (latest[0], dump(messages)))
        db.execute("DELETE FROM card_edits")
        db.execute("UPDATE meta SET value='3' WHERE key='schema_version' AND value IN ('1','2')")


class Conversations:
    def load(self, db, wid):
        row = db.execute("SELECT * FROM card_author_conversations WHERE id=?", (wid,)).fetchone()
        if not row:
            raise KeyError("制卡对话不存在")
        return {**dict(row), **json.loads(row["payload"]), "model_selection": json.loads(row["model_selection"])}

    def write(self, db, workspace, *, increment=True):
        payload = {"cards": workspace["cards"], "state": workspace["state"]}
        if "files" in workspace:
            payload["files"] = workspace["files"]
        if workspace.get("environment"):
            payload["environment"] = workspace["environment"]
        db.execute("UPDATE card_author_conversations SET payload=?,revision=revision+?,updated=? WHERE id=?",
                   (dump(payload), int(increment), time.time(), workspace["id"]))

    def environment_snapshot(self, db, workspace, timestamp=None):
        if workspace.get("environment"):
            return copy.deepcopy(workspace["environment"])
        # Old conversations can recover their first request's time, but cannot
        # reconstruct the OS used then. Never silently present today's OS as it.
        first = db.execute("""SELECT r.created FROM card_author_messages m
            LEFT JOIN card_edit_runs r ON r.id=m.run
            WHERE m.workspace=? AND m.role='user' ORDER BY m.seq LIMIT 1""", (workspace["id"],)).fetchone()
        snapshot = capture_environment(first[0] if first and first[0] is not None else timestamp)
        if first:
            snapshot["legacy"] = True
            if first[0] is None:
                snapshot["local_time"] = "未记录（首次请求日志已清理）"
                snapshot["utc_offset"] = "未记录"
        return snapshot

    def check(self, db, wid, revision, idle=True):
        workspace = self.load(db, wid)
        if workspace["revision"] != revision:
            raise Conflict("制卡对话已改变，请重新载入")
        if idle and db.execute("SELECT 1 FROM card_edit_runs WHERE workspace=? AND status IN ('queued','running')", (wid,)).fetchone():
            raise Conflict("请先等待或停止当前编辑任务")
        if idle and db.execute("SELECT 1 FROM card_author_progress WHERE workspace=?", (wid,)).fetchone():
            raise Conflict("请先继续或丢弃本轮未完成进度")
        return workspace

    def initialize_channel(self, db, wid):
        from tavern.core.channels import provider_channel
        profile = self.store.settings_document().shared.card_author
        cfg = self.store.settings().for_role("card_author")
        selection = {key: getattr(profile, key) for key in ("provider_id", "model")}
        db.execute("UPDATE card_author_conversations SET channel_origin=?,model_selection=? WHERE id=?",
                   (provider_channel(cfg), dump(selection) if profile.model else "{}", wid))

    def create_conversation(self, title=""):
        wid, now = uid(), time.time()
        with self.store.connection() as db:
            db.execute("INSERT INTO card_author_conversations(id,revision,title,payload,created,updated,next_ref) VALUES (?,0,?,?,?,?,1)",
                        (wid, title, dump({"cards": [], "state": {}}), now, now))
            self.initialize_channel(db, wid)
            self.store.create_message_adapter(db, "editor", wid, "card_author")
        return self.conversation(wid)

    def conversations(self, cid=None):
        with self.store.connection() as db:
            rows = db.execute("""SELECT c.*,
                (SELECT substr(content,1,64) FROM card_author_messages WHERE workspace=c.id AND role='user' ORDER BY seq LIMIT 1) AS first_input,
                coalesce((SELECT status FROM card_edit_runs WHERE workspace=c.id ORDER BY created DESC,rowid DESC LIMIT 1),'empty') AS status,
                (SELECT count(*) FROM card_edit_runs WHERE workspace=c.id) AS turn_count,
                EXISTS(SELECT 1 FROM card_author_progress WHERE workspace=c.id) AS resumable
                FROM card_author_conversations c ORDER BY updated DESC,rowid DESC""").fetchall()
        result = []
        for row in rows:
            payload = json.loads(row["payload"])
            items = payload["cards"]
            if cid is None or any(c["card"] == cid for c in items):
                result.append({key: row[key] for key in ("id", "revision", "created", "updated", "status", "turn_count", "resumable", "channel_origin")} | {
                    "title": row["title"] or row["first_input"] or "新制卡对话", "card_count": len(items),
                    "reference_count": len(items) + len(payload["state"].get("persona_attachments", {})) + len(payload.get("files", [])), "card": cid})
        return result

    def messages(self, wid, db=None):
        if db is None:
            with self.store.connection() as connection:
                return self.messages(wid, connection)
        files = self.load(db, wid).get("files", [])
        return [{**dict(r), "ref": f"H{r['seq']}",
                 "attachments": [f for f in files if f.get("message_ref") == f"H{r['seq']}"]}
                for r in db.execute("SELECT * FROM card_author_messages WHERE workspace=? ORDER BY seq", (wid,))]

    def message(self, db, wid, role, text, rid):
        seq = db.execute("SELECT coalesce(max(seq),0)+1 FROM card_author_messages WHERE workspace=?", (wid,)).fetchone()[0]
        db.execute("INSERT INTO card_author_messages VALUES (?,?,?,?,?)", (wid, seq, role, text, rid))
        return f"H{seq}"

    def conversation(self, wid):
        from tavern.character.editor_images import portrait_digest
        with self.store.connection() as db:
            result = self.load(db, wid)
            result.pop("payload", None)
            result["messages"] = self.messages(wid, db)
            result["runs"] = [{**dict(r), "request": json.loads(r["request"])} for r in db.execute("SELECT * FROM card_edit_runs WHERE workspace=? ORDER BY created,rowid", (wid,))]
            pending = db.execute("SELECT run,payload FROM card_author_progress WHERE workspace=?", (wid,)).fetchone()
            value = json.loads(pending[1]) if pending else None
            result["pending"] = {"run": pending[0], "cards": value["working"]["cards"],
                                 "state": value["working"]["state"], "calls": value.get("calls", 0)} if pending else None
            # Compare against the library on read, including pending drafts. This
            # UI metadata never becomes a second persisted portrait source.
            for item in result["cards"] + (result["pending"]["cards"] if pending else []):
                source = db.execute("SELECT revision,container,original FROM cards WHERE id=?", (item["card"],)).fetchone()
                item.update(source_exists=bool(source), source_changed=bool(source and source["revision"] != item["base_revision"]),
                            portrait_changed=portrait_digest(item["source"]) != portrait_digest(dict(source) if source else {}))
            for item in result["cards"]:
                item.update(validation=validation_report(item["document"]),
                            can_undo=bool(db.execute("SELECT 1 FROM card_edit_versions WHERE workspace=? AND ref=?", (wid, item["ref"])).fetchone()))
        return result

    def title(self, wid, title, revision):
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            workspace = self.check(db, wid, revision)
            db.execute("UPDATE card_author_conversations SET title=? WHERE id=?", (title, wid))
            self.write(db, workspace)
        return self.conversation(wid)

    @staticmethod
    def item(workspace, ref):
        item = next((c for c in workspace["cards"] if c["ref"] == ref), None)
        if item is None:
            raise KeyError("卡片引用不存在")
        return item

    def version(self, db, wid, item, reason):
        db.execute("INSERT INTO card_edit_versions(workspace,document,reason,ref,snapshot) VALUES (?,?,?,?,?)",
                   (wid, dump(item["document"]), reason, item["ref"], dump(item)))

    def attach(self, wid, cid, revision, document=None, expected_card_revision=None, *, editable=False):
        source = self.store.card(cid)
        source["dependencies"] = self.store.card_manifest(source)
        if expected_card_revision is not None and expected_card_revision != source["revision"]:
            raise Conflict("角色卡已改变，请刷新后引用")
        document = copy.deepcopy(source["document"] if document is None else document)
        validate_document(document)
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            workspace = self.check(db, wid, revision)
            if not any(c["card"] == cid for c in workspace["cards"]):
                current = db.execute("SELECT revision FROM cards WHERE id=?", (cid,)).fetchone()
                if not current or current[0] != source["revision"]:
                    raise Conflict("角色卡已改变或删除，请重新引用")
                workspace["cards"].append({"ref": f"C{workspace['next_ref']}", "card": cid, "source": source,
                    "kind": "draft" if editable or document != source["document"] else "reference", "target_mode": None,
                    "base_revision": source["revision"], "base_document": source["document"], "document": document, "brief": ""})
                db.execute("UPDATE card_author_conversations SET next_ref=next_ref+1 WHERE id=?", (wid,))
                self.write(db, workspace)
        return self.conversation(wid)

    def attach_persona(self, wid, pid, revision, expected_version):
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            workspace = self.check(db, wid, revision)
            attachments = workspace["state"].setdefault("persona_attachments", {})
            if not any(item["id"] == pid for item in attachments.values()):
                row = db.execute("SELECT * FROM personas WHERE id=?", (pid,)).fetchone()
                if row is None:
                    raise KeyError("用户角色不存在")
                if row["version"] != expected_version:
                    raise Conflict("用户角色已改变，请刷新后引用")
                refs = workspace["state"].setdefault("persona_refs", {})
                ref = next((ref for ref, value in refs.items() if value == pid), None)
                if ref is None:
                    ref = f"P{workspace['next_ref']}"
                    db.execute("UPDATE card_author_conversations SET next_ref=next_ref+1 WHERE id=?", (wid,))
                    refs[ref] = pid
                attachments[ref] = {key: row[key] for key in ("id", "name", "description", "version")}
                self.write(db, workspace)
        return self.conversation(wid)

    def detach_persona(self, wid, ref, revision):
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            workspace = self.check(db, wid, revision)
            attachments = workspace["state"].get("persona_attachments", {})
            if ref not in attachments:
                raise KeyError("用户角色引用不存在")
            del attachments[ref]
            self.write(db, workspace)
        return self.conversation(wid)

    def update_card(self, wid, ref, document, revision, reason="手工修改", target_mode=...):
        validate_document(document)
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            workspace = self.check(db, wid, revision)
            item = self.item(workspace, ref)
            target_changed = target_mode is not ... and target_mode != item.get("target_mode")
            if document != item["document"] or target_changed:
                self.version(db, wid, item, reason)
                item.update(document=document, kind="draft")
                if target_changed:
                    item["target_mode"] = target_mode
                self.write(db, workspace)
        return self.conversation(wid)

    def undo_card(self, wid, ref, revision):
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            workspace = self.check(db, wid, revision)
            item = self.item(workspace, ref)
            old = db.execute("SELECT * FROM card_edit_versions WHERE workspace=? AND ref=? ORDER BY id DESC LIMIT 1", (wid, ref)).fetchone()
            if old:
                if old["snapshot"]:
                    workspace["cards"][workspace["cards"].index(item)] = json.loads(old["snapshot"])
                else:
                    item["document"] = json.loads(old["document"])
                db.execute("DELETE FROM card_edit_versions WHERE id=?", (old["id"],))
                self.write(db, workspace)
        return self.conversation(wid)

    def reload_card(self, wid, ref, revision):
        item = self.item(self.conversation(wid), ref)
        source = self.store.card(item["card"])
        source["dependencies"] = self.store.card_manifest(source)
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            workspace = self.check(db, wid, revision)
            item = self.item(workspace, ref)
            current = db.execute("SELECT revision FROM cards WHERE id=?", (item["card"],)).fetchone()
            if not current or current[0] != source["revision"]:
                raise Conflict("原卡再次改变，请重试")
            self.version(db, wid, item, "重新载入前的草稿")
            item.update(source=source, base_revision=source["revision"], base_document=source["document"], document=source["document"])
            self.write(db, workspace)
        return self.conversation(wid)

    def set_portrait(self, wid, ref, file_ref, revision):
        from tavern.character.editor_images import apply_portrait, portrait_png
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            workspace = self.check(db, wid, revision)
            item = self.item(workspace, ref)
            file = next((f for f in workspace.get("files", []) if f["ref"] == file_ref), None) if file_ref else None
            if file_ref and file is None:
                raise KeyError("文件引用不存在")
            digest = portrait_png(self.store, wid, file) if file else None
            if digest != item.get("source", {}).get("original") or not digest and item.get("source", {}).get("container") == "png":
                self.version(db, wid, item, "设置头像" if digest else "移除头像")
                apply_portrait(item, digest)
                self.write(db, workspace)
        return self.conversation(wid)

    def portrait(self, wid, digest):
        """Serve only images that a current, pending or undoable draft actually uses."""
        with self.store.connection() as db:
            workspace = self.load(db, wid)
            sources = [c.get("source", {}) for c in workspace["cards"]]
            pending = db.execute("SELECT payload FROM card_author_progress WHERE workspace=?", (wid,)).fetchone()
            if pending:
                sources.extend(c.get("source", {}) for c in json.loads(pending[0])["working"]["cards"])
        if not any(s.get("container") == "png" and s.get("original") == digest for s in sources):
            raise KeyError("头像不存在")
        return self.store.root / "blobs" / digest

    def remove_card(self, wid, ref, revision):
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            workspace = self.check(db, wid, revision)
            item = self.item(workspace, ref)
            sources = [item["source"], *[json.loads(r[0]).get("source", {}) for r in db.execute(
                "SELECT snapshot FROM card_edit_versions WHERE workspace=? AND ref=? AND snapshot IS NOT NULL", (wid, ref))]]
            workspace["cards"].remove(item)
            db.execute("DELETE FROM card_edit_versions WHERE workspace=? AND ref=?", (wid, ref))
            self.write(db, workspace)
        for source in sources:
            self.store.prune_card_assets({**source, "id": wid}, source.get("dependencies", {}))
        return self.conversation(wid)

    def save_card(self, wid, ref, revision, mode, operation):
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute("SELECT result FROM card_edit_saves WHERE workspace=? AND operation=?", (wid, operation)).fetchone()
            if prior:
                result = json.loads(prior[0])
                if result.pop("_ref", "C1") != ref:
                    raise Conflict("保存操作编号已用于另一张卡")
                return result
            workspace = self.check(db, wid, revision)
            item = self.item(workspace, ref)
            document, source = copy.deepcopy(item["document"]), item["source"]
            dependencies = {"resources": {}, "scripts": {}, **source.get("dependencies", {})}
            validate_document(document)
            # A portrait set in the draft replaces the PNG container image; removing it
            # leaves a JSON card whose original is the document itself.
            original = source.get("original") or self.store.blob(dump(document).encode())
            if mode == "overwrite":
                current = db.execute("SELECT revision FROM cards WHERE id=?", (item["card"],)).fetchone()
                if not current or current[0] != item["base_revision"]:
                    raise Conflict("原卡已改变或删除；草稿已保留，请另存或重新载入")
                cid = item["card"]
                db.execute("UPDATE cards SET document=?,name=?,report=?,original=?,container=?,revision=revision+1 WHERE id=?",
                           (dump(document), normalize(document)["name"], dump(diagnose(document)), original, source["container"], cid))
            elif mode in {"copy", "create"}:
                if mode == "create" and item["card"]:
                    raise ValueError("草稿已有保存目标，请使用覆盖或另存")
                cid = uid()
                if mode == "copy":
                    data = document.get("data", document) if document.get("spec") else document
                    names = {r[0] for r in db.execute("SELECT name FROM cards")}
                    base, index = data["name"] + "（副本）", 2
                    name = base
                    while name in names:
                        name, index = f"{base} {index}", index + 1
                    data["name"] = name
                # Permission is a current user setting, not an editable draft field.
                # A long-lived editor source may predate either approval or revocation.
                live_source = db.execute("SELECT permission FROM cards WHERE id=?", (source.get("id"),)).fetchone()
                permission = live_source["permission"] if live_source else source.get("permission", 0)
                db.execute("INSERT INTO cards(id,name,original,container,resources,permission,document,report) VALUES (?,?,?,?,?,?,?,?)",
                           (cid, normalize(document)["name"], original, source.get("container", "json"), dump(source.get("resources", {})), permission, dump(document), dump(diagnose(document))))
                path = self.store.card_manifest_path({"id": cid})
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(dump(dependencies), encoding="utf-8")
            else:
                raise ValueError("保存方式无效")
            result = dict(db.execute("SELECT * FROM cards WHERE id=?", (cid,)).fetchone())
            for key in ("document", "resources", "report"):
                result[key] = json.loads(result[key])
            item.update(card=cid, source={**result, "dependencies": dependencies}, base_revision=result["revision"], base_document=document, document=document, kind="draft")
            db.execute("DELETE FROM card_edit_versions WHERE workspace=? AND ref=?", (wid, ref))
            db.execute("INSERT INTO card_edit_saves VALUES (?,?,?)", (wid, operation, dump({**result, "_ref": ref})))
            self.write(db, workspace)
        return result

    def delete(self, wid, revision):
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            workspace = self.check(db, wid, revision)
            sources = [c["source"] for c in workspace["cards"]]
            sources.extend(json.loads(r[0]).get("source", {}) for r in db.execute(
                "SELECT snapshot FROM card_edit_versions WHERE workspace=? AND snapshot IS NOT NULL", (wid,)))
            db.execute("DELETE FROM card_edit_events WHERE run IN (SELECT id FROM card_edit_runs WHERE workspace=?)", (wid,))
            db.execute("DELETE FROM card_author_traces WHERE run IN (SELECT id FROM card_edit_runs WHERE workspace=?)", (wid,))
            for table in ("card_edit_runs", "card_edit_versions", "card_edit_saves", "card_edit_contexts", "card_author_messages", "card_author_progress"):
                db.execute(f"DELETE FROM {table} WHERE workspace=?", (wid,))
            db.execute("DELETE FROM card_author_conversations WHERE id=?", (wid,))
            db.execute("DELETE FROM sent_cache_breakpoints WHERE owner=?", (wid,))
        for source in sources:
            self.store.prune_card_assets({**source, "id": wid}, source.get("dependencies", {}))
        from tavern.character.editor_attachments import remove_files
        remove_files(self.store, wid, workspace.get("files", []), conversation_deleted=True)

    def discard(self, wid, revision):
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            workspace = self.check(db, wid, revision, idle=False)
            if db.execute("SELECT 1 FROM card_edit_runs WHERE workspace=? AND status IN ('queued','running')", (wid,)).fetchone():
                raise Conflict("请先停止任务")
            db.execute("DELETE FROM card_author_progress WHERE workspace=?", (wid,))
            self.write(db, workspace)
        return self.conversation(wid)

    def reserve_ref(self, wid, prefix="C"):
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            number = db.execute("SELECT next_ref FROM card_author_conversations WHERE id=?", (wid,)).fetchone()[0]
            db.execute("UPDATE card_author_conversations SET next_ref=next_ref+1 WHERE id=?", (wid,))
        return f"{prefix}{number}"

    def progress(self, wid):
        with self.store.connection() as db:
            row = db.execute("SELECT payload FROM card_author_progress WHERE workspace=?", (wid,)).fetchone()
        return json.loads(row[0]) if row else None

    def save_progress(self, wid, rid, payload):
        with self.store.connection() as db:
            db.execute("INSERT OR REPLACE INTO card_author_progress VALUES (?,?,?)", (wid, rid, dump(payload)))

    def get(self, wid):
        value = self.conversation(wid)
        if len(value["cards"]) > 1:
            raise Conflict("多卡对话请明确指定卡片")
        return {**value, **value["cards"][0], "id": wid} if value["cards"] else value

    def create(self, cid, document=None, expected_revision=None):
        # Validate first, so a stale legacy request does not create an empty chat.
        source = self.store.card(cid)
        if expected_revision is not None and expected_revision != source["revision"]:
            raise Conflict("角色卡已改变，请刷新后创建编辑草稿")
        document = copy.deepcopy(document if document is not None else source["document"])
        validate_document(document)
        source["dependencies"] = self.store.card_manifest(source)
        wid, now = uid(), time.time()
        item = {"ref": "C1", "card": cid, "source": source, "kind": "draft", "target_mode": None,
                "base_revision": source["revision"], "base_document": source["document"], "document": document, "brief": ""}
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT revision FROM cards WHERE id=?", (cid,)).fetchone()
            if not current or current[0] != source["revision"]:
                raise Conflict("角色卡已改变或删除，请刷新后创建编辑草稿")
            db.execute("INSERT INTO card_author_conversations(id,revision,title,payload,created,updated,next_ref) VALUES (?,0,'',?,?,?,2)",
                       (wid, dump({"cards": [item], "state": {}}), now, now))
            self.initialize_channel(db, wid)
            self.store.create_message_adapter(db, "editor", wid, "card_author")
        return self.get(wid)

    def list(self, cid):
        return self.conversations(cid)

    def update(self, wid, document, revision, reason="手工修改"):
        self.update_card(wid, self.get(wid)["ref"], document, revision, reason)
        return self.get(wid)

    def undo(self, wid, revision):
        self.undo_card(wid, self.get(wid)["ref"], revision)
        return self.get(wid)

    def reload(self, wid, revision):
        self.reload_card(wid, self.get(wid)["ref"], revision)
        return self.get(wid)

    def save(self, wid, revision, mode, operation):
        return self.save_card(wid, self.get(wid)["ref"], revision, mode, operation)

    def new_context(self, wid, revision):
        new_id, now = uid(), time.time()
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            workspace = self.check(db, wid, revision, idle=False)
            if len(workspace["cards"]) != 1:
                raise Conflict("旧接口只支持单卡草稿；请从制卡入口创建对话并明确引用")
            db.execute("INSERT INTO card_author_conversations(id,revision,title,payload,created,updated,next_ref) VALUES (?,0,'',?,?,?,?)", (new_id,
                dump({"cards": workspace["cards"], "state": {}}), now, now, workspace["next_ref"]))
            db.execute("UPDATE card_author_conversations SET channel_origin=?,model_selection=? WHERE id=?",
                       (workspace["channel_origin"], dump(workspace["model_selection"]), new_id))
            self.store.copy_message_adapter(db, "editor", wid, new_id)
        return self.get(new_id)
