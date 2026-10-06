"""Adapter preferences have their own revision, independent of in-flight story commits."""
import json

from tavern.core.message_adapter import MessageAdapterConfig, catalog, infer_type


def recipe_types(recipe):
    for block in recipe.get("blocks", []):
        if block.get("source") != "history" and not block.get("message_type"):
            block["message_type"] = infer_type({"context_source": block["source"], "role": block.get("role", "system")})
    return recipe


class MessageAdapters:
    def initialize_message_adapters(self):
        with self.connection() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS message_adapter_configs(
                owner_kind TEXT NOT NULL, owner_id TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0,
                config TEXT NOT NULL, PRIMARY KEY(owner_kind,owner_id))""")
            db.executescript("""CREATE TRIGGER IF NOT EXISTS adapter_story_delete AFTER DELETE ON sessions BEGIN
                DELETE FROM message_adapter_configs WHERE owner_kind='story' AND owner_id=OLD.id; END;
                CREATE TRIGGER IF NOT EXISTS adapter_editor_delete AFTER DELETE ON card_editor_conversations BEGIN
                DELETE FROM message_adapter_configs WHERE owner_kind='editor' AND owner_id=OLD.id; END;""")
            owners = [("story", r["id"], r["agent_mode"]) for r in db.execute("SELECT id,agent_mode FROM sessions")]
            owners += [("editor", r["id"], "card_editor") for r in db.execute("SELECT id FROM card_editor_conversations")]
            for kind, owner, mode in owners:
                self.create_message_adapter(db, kind, owner, mode)
            for row in db.execute("SELECT owner_kind,owner_id,config FROM message_adapter_configs").fetchall():
                config = json.loads(row["config"])
                cleaned = {lane: MessageAdapterConfig.model_validate(value).model_dump()
                           for lane, value in config.items()}
                if cleaned != config:
                    db.execute("""UPDATE message_adapter_configs SET config=?,revision=revision+1
                        WHERE owner_kind=? AND owner_id=?""",
                        (json.dumps(cleaned, ensure_ascii=False), row["owner_kind"], row["owner_id"]))

    def create_message_adapter(self, db, kind, owner, mode):
        document = self.settings_document()
        profiles = ({"card_editor": document.shared.card_editor} if kind == "editor" else
                    {"full_agent": document.full_agent.model} if mode == "full_agent" else
                    {"foreground": document.half_agent.foreground, "background": document.half_agent.background})
        config = {lane: profile.message_adapter.model_dump() for lane, profile in profiles.items()}
        db.execute("INSERT OR IGNORE INTO message_adapter_configs(owner_kind,owner_id,config) VALUES (?,?,?)",
                   (kind, owner, json.dumps(config, ensure_ascii=False)))

    def message_adapter_settings(self, kind, owner):
        if kind not in ("story", "editor"):
            raise ValueError("未知消息适配设置范围")
        with self.connection() as db:
            table = "sessions" if kind == "story" else "card_editor_conversations"
            row = db.execute(f"SELECT * FROM {table} WHERE id=?", (owner,)).fetchone()
            if row is None:
                raise KeyError("对话不存在")
            saved = db.execute("SELECT * FROM message_adapter_configs WHERE owner_kind=? AND owner_id=?",
                               (kind, owner)).fetchone()
            if saved is None:
                raise ValueError("对话消息适配设置尚未初始化")
            extra = []
            if kind == "story":
                recipe = recipe_types(json.loads(row["recipe"]))
                extra = [b["message_type"] for b in recipe.get("blocks", []) if b.get("message_type")]
            config = json.loads(saved["config"])
        document = self.settings_document()
        profiles = {"foreground": document.half_agent.foreground, "background": document.half_agent.background,
                    "full_agent": document.full_agent.model, "card_editor": document.shared.card_editor}
        labels = {"foreground": "正文生成", "background": "Tracker", "full_agent": "Full Agent", "card_editor": "角色卡编辑"}
        return {"config": config, "revision": saved["revision"], "lanes": [
            {"id": lane, "label": labels[lane], "api_mode": profiles[lane].api_mode,
             "types": catalog(lane, list(dict.fromkeys((extra if lane == "foreground" else []) +
                 [name for rules in config[lane].get("mappings", {}).values() for name in rules])))} for lane in config]}

    def message_adapter(self, kind, owner, lane):
        with self.connection() as db:
            row = db.execute("SELECT config,revision FROM message_adapter_configs WHERE owner_kind=? AND owner_id=?",
                             (kind, owner)).fetchone()
        if row is None:
            raise KeyError("对话消息适配设置不存在")
        config = json.loads(row["config"])
        if lane in ("foreground", "background") and "full_agent" in config:
            lane = "full_agent"
        if lane not in config:
            raise ValueError("对话不包含此模型调用链")
        return {"config": config[lane], "revision": row["revision"]}

    def save_message_adapter(self, kind, owner, expected, config):
        from tavern.storage.store import Conflict
        current = self.message_adapter_settings(kind, owner)
        if set(config) != set(current["config"]):
            raise ValueError("消息适配调用链不匹配")
        validated = {lane: MessageAdapterConfig.model_validate(value).model_dump() for lane, value in config.items()}
        with self.connection() as db:
            result = db.execute("""UPDATE message_adapter_configs SET config=?,revision=revision+1
                WHERE owner_kind=? AND owner_id=? AND revision=?""",
                (json.dumps(validated, ensure_ascii=False), kind, owner, expected))
            if result.rowcount != 1:
                raise Conflict("消息适配设置已改变，请重新载入")
        return self.message_adapter_settings(kind, owner)

    def copy_message_adapter(self, db, kind, previous, owner):
        db.execute("""INSERT INTO message_adapter_configs(owner_kind,owner_id,config)
            SELECT owner_kind,?,config FROM message_adapter_configs WHERE owner_kind=? AND owner_id=?
            ON CONFLICT(owner_kind,owner_id) DO UPDATE SET config=excluded.config,revision=0""",
            (owner, kind, previous))
