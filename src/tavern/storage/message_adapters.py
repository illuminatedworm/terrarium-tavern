"""Adapter preferences have their own revision, independent of in-flight story commits."""

import json

from tavern.core.errors import NotFound
from tavern.core.message_adapter import MessageAdapterConfig, catalog


class MessageAdapters:
    def initialize_message_adapters(self):
        with self.connection() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS message_adapter_configs(
                owner_kind TEXT NOT NULL, owner_id TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0,
                config TEXT NOT NULL, PRIMARY KEY(owner_kind,owner_id))""")
            db.executescript("""CREATE TRIGGER IF NOT EXISTS adapter_story_delete AFTER DELETE ON sessions BEGIN
                DELETE FROM message_adapter_configs WHERE owner_kind='story' AND owner_id=OLD.id; END;
                CREATE TRIGGER IF NOT EXISTS adapter_editor_delete AFTER DELETE ON card_author_conversations BEGIN
                DELETE FROM message_adapter_configs WHERE owner_kind='editor' AND owner_id=OLD.id; END;""")
            for kind, table in (("story", "sessions"), ("editor", "card_author_conversations")):
                for row in db.execute(
                    f"""
                    SELECT id FROM {table} WHERE NOT EXISTS (
                        SELECT 1 FROM message_adapter_configs WHERE owner_kind=? AND owner_id={table}.id
                    )
                """,
                    (kind,),
                ).fetchall():
                    self.create_message_adapter(db, kind, row["id"])
            for row in db.execute(
                "SELECT owner_kind,owner_id,config FROM message_adapter_configs"
            ).fetchall():
                config = json.loads(row["config"])
                cleaned = {
                    lane: MessageAdapterConfig.model_validate(value).model_dump()
                    for lane, value in config.items()
                }
                if cleaned != config:
                    db.execute(
                        """UPDATE message_adapter_configs SET config=?,revision=revision+1
                        WHERE owner_kind=? AND owner_id=?""",
                        (json.dumps(cleaned, ensure_ascii=False), row["owner_kind"], row["owner_id"]),
                    )

    def create_message_adapter(self, db, kind, owner):
        from tavern.core.settings import SettingsDocument

        document = SettingsDocument.model_validate_json(
            db.execute("SELECT value FROM meta WHERE key='settings'").fetchone()[0]
        )
        lane, profile = (
            ("card_author", document.shared.card_author)
            if kind == "editor"
            else ("story", document.story.model)
        )
        config = {lane: profile.message_adapter.model_dump()}
        db.execute(
            "INSERT OR IGNORE INTO message_adapter_configs(owner_kind,owner_id,config) VALUES (?,?,?)",
            (kind, owner, json.dumps(config, ensure_ascii=False)),
        )

    def message_adapter_settings(self, kind, owner):
        if kind not in ("story", "editor"):
            raise ValueError("未知消息适配设置范围")
        with self.connection() as db:
            table = "sessions" if kind == "story" else "card_author_conversations"
            row = db.execute(f"SELECT * FROM {table} WHERE id=?", (owner,)).fetchone()
            if row is None:
                raise NotFound("对话不存在")
            saved = db.execute(
                "SELECT * FROM message_adapter_configs WHERE owner_kind=? AND owner_id=?", (kind, owner)
            ).fetchone()
            if saved is None:
                raise ValueError("对话消息适配设置尚未初始化")
            config = json.loads(saved["config"])
        document = self.settings_document()
        profiles = {
            "story": document.story.model,
            "card_author": document.shared.card_author,
        }
        labels = {
            "story": "游玩",
            "card_author": "制卡 agent",
        }
        return {
            "config": config,
            "revision": saved["revision"],
            "lanes": [
                {
                    "id": lane,
                    "label": labels[lane],
                    "api_mode": profiles[lane].api_mode,
                    "types": catalog(
                        lane,
                        list(
                            dict.fromkeys(
                                [
                                    name
                                    for rules in config[lane].get("mappings", {}).values()
                                    for name in rules
                                ]
                            )
                        ),
                    ),
                }
                for lane in config
            ],
        }

    def message_adapter(self, kind, owner, lane):
        with self.connection() as db:
            row = db.execute(
                "SELECT config,revision FROM message_adapter_configs WHERE owner_kind=? AND owner_id=?",
                (kind, owner),
            ).fetchone()
        if row is None:
            raise NotFound("对话消息适配设置不存在")
        config = json.loads(row["config"])
        if lane not in config:
            raise ValueError("对话不包含此模型调用链")
        return {"config": config[lane], "revision": row["revision"]}

    def save_message_adapter(self, kind, owner, expected, config):
        from tavern.storage.store import Conflict

        current = self.message_adapter_settings(kind, owner)
        if set(config) != set(current["config"]):
            raise ValueError("消息适配调用链不匹配")
        validated = {
            lane: MessageAdapterConfig.model_validate(value).model_dump() for lane, value in config.items()
        }
        with self.connection() as db:
            result = db.execute(
                """UPDATE message_adapter_configs SET config=?,revision=revision+1
                WHERE owner_kind=? AND owner_id=? AND revision=?""",
                (json.dumps(validated, ensure_ascii=False), kind, owner, expected),
            )
            if result.rowcount != 1:
                raise Conflict("消息适配设置已改变，请重新载入")
        return self.message_adapter_settings(kind, owner)

    def copy_message_adapter(self, db, kind, previous, owner):
        db.execute(
            """INSERT INTO message_adapter_configs(owner_kind,owner_id,config)
            SELECT owner_kind,?,config FROM message_adapter_configs WHERE owner_kind=? AND owner_id=?
            ON CONFLICT(owner_kind,owner_id) DO UPDATE SET config=excluded.config,revision=0""",
            (owner, kind, previous),
        )
