"""Card editing workspaces are independent of story sessions and turn snapshots."""

import copy
import json
import time

from tavern.character.card_workflow import validate_document, validation_report
from tavern.character.cards import diagnose, normalize
from tavern.storage.store import Conflict, dump, uid


def initialize_editor_tables(store):
    with store.connection() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS card_edits(
                id TEXT PRIMARY KEY, card TEXT NOT NULL, source TEXT NOT NULL,
                base_revision INTEGER NOT NULL, base_document TEXT NOT NULL,
                document TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS card_edit_versions(
                id INTEGER PRIMARY KEY AUTOINCREMENT, workspace TEXT NOT NULL,
                document TEXT NOT NULL, reason TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS card_edit_runs(
                id TEXT PRIMARY KEY, workspace TEXT NOT NULL, operation TEXT NOT NULL,
                request TEXT NOT NULL, status TEXT NOT NULL, response TEXT NOT NULL DEFAULT '',
                error TEXT, created REAL NOT NULL, UNIQUE(workspace,operation));
            CREATE TABLE IF NOT EXISTS card_edit_events(
                seq INTEGER PRIMARY KEY AUTOINCREMENT, run TEXT NOT NULL,
                kind TEXT NOT NULL, payload TEXT NOT NULL, created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS card_edit_saves(
                workspace TEXT NOT NULL, operation TEXT NOT NULL, result TEXT NOT NULL,
                PRIMARY KEY(workspace,operation));
            CREATE INDEX IF NOT EXISTS card_edit_events_run ON card_edit_events(run,seq);
            CREATE INDEX IF NOT EXISTS card_edit_versions_workspace ON card_edit_versions(workspace,id);
            CREATE INDEX IF NOT EXISTS card_edits_card ON card_edits(card);
        """)


def recover_editor_runs(store):
    with store.connection() as db:
        db.execute("UPDATE card_edit_runs SET status='interrupted',error='服务重启，本轮修改未应用到草稿' "
                   "WHERE status IN ('queued','running')")


class EditorStore:
    def __init__(self, store):
        self.store = store

    def create(self, cid, document=None, expected_revision=None):
        card = self.store.card(cid)
        if expected_revision is not None and card["revision"] != expected_revision:
            raise Conflict("角色卡已改变，请刷新后创建编辑草稿")
        document = copy.deepcopy(card["document"] if document is None else document)
        validate_document(document)
        card["dependencies"] = self.store.card_manifest(card)
        wid = uid()
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT revision FROM cards WHERE id=?", (cid,)).fetchone()
            if current is None or current[0] != card["revision"]:
                raise Conflict("角色卡已改变或删除，请刷新后创建编辑草稿")
            db.execute("INSERT INTO card_edits VALUES (?,?,?,?,?,?,0,?)",
                       (wid, cid, dump(card), card["revision"], dump(card["document"]), dump(document), time.time()))
        return self.get(wid)

    def get(self, wid):
        with self.store.connection() as db:
            row = db.execute("SELECT * FROM card_edits WHERE id=?", (wid,)).fetchone()
            if row is None:
                raise KeyError("编辑草稿不存在")
            runs = db.execute("SELECT * FROM card_edit_runs WHERE workspace=? ORDER BY created", (wid,)).fetchall()
            undo = db.execute("SELECT count(*) FROM card_edit_versions WHERE workspace=?", (wid,)).fetchone()[0]
            source_exists = db.execute("SELECT 1 FROM cards WHERE id=?", (row["card"],)).fetchone() is not None
        result = dict(row)
        for key in ("source", "document", "base_document"):
            result[key] = json.loads(result[key])
        result["runs"] = [{**dict(r), "request": json.loads(r["request"])} for r in runs]
        result["can_undo"] = bool(undo)
        result["source_exists"] = source_exists
        result["validation"] = validation_report(result["document"])
        return result

    def list(self, cid):
        with self.store.connection() as db:
            return [dict(r) for r in db.execute(
                """SELECT w.id,w.card,w.revision,w.created,
                coalesce((SELECT substr(json_extract(r.request,'$.text'),1,64) FROM card_edit_runs r
                    WHERE r.workspace=w.id ORDER BY r.created,r.rowid LIMIT 1),'新对话') AS title,
                coalesce((SELECT r.status FROM card_edit_runs r WHERE r.workspace=w.id
                    ORDER BY r.created DESC,r.rowid DESC LIMIT 1),'empty') AS status,
                (SELECT count(*) FROM card_edit_runs r WHERE r.workspace=w.id) AS turn_count
                FROM card_edits w WHERE w.card=? ORDER BY w.created DESC,w.rowid DESC""", (cid,))]

    def new_context(self, wid, revision):
        """Carry only the last complete draft, never chat, tool history or undo versions.

        Preserve the original base revision: carrying an old draft must not silently
        authorize overwriting a newer library card. Running source turns are unaffected.
        """
        new_id = uid()
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self.check(db, wid, revision, idle=False)
            db.execute("INSERT INTO card_edits VALUES (?,?,?,?,?,?,0,?)",
                       (new_id, row["card"], row["source"], row["base_revision"],
                        row["base_document"], row["document"], time.time()))
        return self.get(new_id)

    def check(self, db, wid, revision, idle=True):
        row = db.execute("SELECT * FROM card_edits WHERE id=?", (wid,)).fetchone()
        if row is None:
            raise KeyError("编辑草稿不存在")
        if row["revision"] != revision:
            raise Conflict("编辑草稿已改变，请重新载入")
        if idle and db.execute("SELECT 1 FROM card_edit_runs WHERE workspace=? AND status IN ('queued','running')",
                               (wid,)).fetchone():
            raise Conflict("请先等待或停止当前编辑任务")
        return row

    def update(self, wid, document, revision, reason="手工修改"):
        validate_document(document)
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self.check(db, wid, revision)
            if dump(document) != row["document"]:
                db.execute("INSERT INTO card_edit_versions(workspace,document,reason) VALUES (?,?,?)",
                           (wid, row["document"], reason))
                db.execute("UPDATE card_edits SET document=?,revision=revision+1 WHERE id=?", (dump(document), wid))
        return self.get(wid)

    def undo(self, wid, revision):
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self.check(db, wid, revision)
            version = db.execute("SELECT * FROM card_edit_versions WHERE workspace=? ORDER BY id DESC LIMIT 1", (wid,)).fetchone()
            if version:
                db.execute("UPDATE card_edits SET document=?,revision=revision+1 WHERE id=?", (version["document"], wid))
                db.execute("DELETE FROM card_edit_versions WHERE id=?", (version["id"],))
        return self.get(wid)

    def reload(self, wid, revision):
        workspace = self.get(wid)
        source = self.store.card(workspace["card"])
        source["dependencies"] = self.store.card_manifest(source)
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self.check(db, wid, revision)
            db.execute("INSERT INTO card_edit_versions(workspace,document,reason) VALUES (?,?,?)",
                       (wid, row["document"], "重新载入原卡前的草稿"))
            db.execute("UPDATE card_edits SET source=?,base_revision=?,base_document=?,document=?,revision=revision+1 WHERE id=?",
                       (dump(source), source["revision"], dump(source["document"]), dump(source["document"]), wid))
        return self.get(wid)

    def delete(self, wid, revision):
        source = self.get(wid)["source"]
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self.check(db, wid, revision)
            db.execute("DELETE FROM card_edit_events WHERE run IN (SELECT id FROM card_edit_runs WHERE workspace=?)", (wid,))
            for table in ("card_edit_runs", "card_edit_versions", "card_edit_saves"):
                db.execute(f"DELETE FROM {table} WHERE workspace=?", (wid,))
            db.execute("DELETE FROM card_edits WHERE id=?", (wid,))
        # Use an editor-owned manifest identity so the source card's cache is never removed.
        self.store.prune_card_assets({**source, "id": wid}, source.get("dependencies", {}))

    def run(self, rid):
        with self.store.connection() as db:
            row = db.execute("SELECT * FROM card_edit_runs WHERE id=?", (rid,)).fetchone()
        if row is None:
            raise KeyError("编辑任务不存在")
        return {**dict(row), "request": json.loads(row["request"])}

    def event(self, rid, kind, payload):
        with self.store.connection() as db:
            db.execute("INSERT INTO card_edit_events(run,kind,payload,created) VALUES (?,?,?,?)",
                       (rid, kind, dump(payload), time.time()))

    def events(self, rid, after=0):
        with self.store.connection() as db:
            return [{**dict(r), "payload": json.loads(r["payload"])} for r in db.execute(
                "SELECT * FROM card_edit_events WHERE run=? AND seq>? ORDER BY seq LIMIT 200", (rid, after))]

    def activity(self, rid, after=0):
        with self.store.connection() as db:
            rows = db.execute("""SELECT * FROM card_edit_events WHERE run=? AND seq>?
                AND kind IN ('started','model_call','tool_started','tool_finished','completed','error','cancelled')
                ORDER BY seq LIMIT 201""", (rid, after)).fetchall()
        events = [{**dict(row), "payload": json.loads(row["payload"])} for row in rows[:200]]
        return {"events": events, "has_more": len(rows) > 200,
                "next_cursor": events[-1]["seq"] if events else after}

    def save(self, wid, revision, mode, operation):

        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute("SELECT result FROM card_edit_saves WHERE workspace=? AND operation=?", (wid, operation)).fetchone()
            if prior:
                return json.loads(prior[0])
            row = self.check(db, wid, revision)
            document, source = json.loads(row["document"]), json.loads(row["source"])
            validate_document(document)
            if mode == "overwrite":
                current = db.execute("SELECT revision FROM cards WHERE id=?", (row["card"],)).fetchone()
                if current is None or current[0] != row["base_revision"]:
                    raise Conflict("原卡已改变或删除；草稿已保留，请另存副本或重新载入原卡")
                cid = row["card"]
                db.execute("UPDATE cards SET document=?,name=?,report=?,revision=revision+1 WHERE id=?",
                           (dump(document), normalize(document)["name"], dump(diagnose(document)), cid))
            elif mode == "copy":
                cid = uid()
                data = document.get("data", document) if document.get("spec") else document
                names = {r[0] for r in db.execute("SELECT name FROM cards")}
                base, index = data["name"] + "（副本）", 2
                name = base
                while name in names:
                    name, index = f"{base} {index}", index + 1
                data["name"] = name
                path = self.store.card_manifest_path({"id": cid})
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(dump(source.get("dependencies", {})), encoding="utf-8")
                db.execute("INSERT INTO cards(id,name,original,container,resources,permission,document,report) "
                           "VALUES (?,?,?,?,?,?,?,?)", (cid, name, source["original"], source["container"],
                           dump(source["resources"]), source["permission"], dump(document), dump(diagnose(document))))
            else:
                raise ValueError("保存方式无效")
            result = dict(db.execute("SELECT * FROM cards WHERE id=?", (cid,)).fetchone())
            for key in ("document", "resources", "report"):
                result[key] = json.loads(result[key])
            source = {**result, "dependencies": source.get("dependencies", {})}
            db.execute("UPDATE card_edits SET card=?,source=?,base_revision=?,base_document=?,document=?,revision=revision+1 WHERE id=?",
                       (cid, dump(source), result["revision"], dump(document), dump(document), wid))
            db.execute("DELETE FROM card_edit_versions WHERE workspace=?", (wid,))
            db.execute("INSERT INTO card_edit_saves VALUES (?,?,?)", (wid, operation, dump(result)))
        return result
