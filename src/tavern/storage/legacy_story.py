"""Read the story keys used by older settings, databases and personal data packages."""

import re
import sqlite3
from pathlib import Path

from tavern.storage.migrate_json import rewrite_json_records
from tavern.storage.session_deletion import delete_session_rows

OLD_STORY_KEY = "full_agent"
RETIRED_STORY_KEY = "half_agent"
OLD_SESSION_FIELDS = ("agent_mode", "recipe", "lore_injection_mode")
_OLD_GUIDE_REFERENCE = re.compile(r"(?<![\w./\\:-])(?:full-agent|half-agent)\.md(?![\w/\\-]|\.\w)", re.ASCII)


def upgrade_card_author_guide_references(text: str) -> str:
    # Only standalone built-in resource names. The author may cite a path or URL.
    return _OLD_GUIDE_REFERENCE.sub("story.md", text)


def _rename_key(record: dict, old: str, new: str) -> bool:
    if old not in record:
        return False
    if new in record:
        raise ValueError(f"旧记录中的 {new} 与新版字段重复")
    record[new] = record.pop(old)
    return True


def upgrade_settings_keys(raw: dict) -> None:
    _rename_key(raw, OLD_STORY_KEY, "story")
    if "version" in raw:
        _rename_key(raw.get("revisions", {}), OLD_STORY_KEY, "story")
        shared = raw.get("shared", {})
    else:
        for suffix in ("config", "system_prompt"):
            _rename_key(raw, f"{OLD_STORY_KEY}_{suffix}", f"story_{suffix}")
        shared = raw
    _rename_key(shared, f"debug_{OLD_STORY_KEY}_prefix", "debug_story_prefix")


def discard_retired_settings(raw: dict) -> None:
    raw.pop(RETIRED_STORY_KEY, None)
    raw.get("revisions", {}).pop(RETIRED_STORY_KEY, None)


def supported_package_story(session: dict, version: int) -> bool:
    if version >= 14:
        return True
    marker = session.get("agent_mode")
    if marker == RETIRED_STORY_KEY or (version < 3 and marker is None):
        return False
    if marker != OLD_STORY_KEY:
        raise ValueError("数据包包含无法识别的旧故事记录")
    if version < 3:
        raise ValueError("旧故事早于整轮重试格式，请选择只包含资料的数据包")
    return True


def _snapshot_fields(record: dict, *fields: str) -> bool:
    changed = False
    for field in fields:
        if field in record:
            changed |= _rename_key(record[field], OLD_STORY_KEY, "story")
    return changed


def _trace_message(message: dict) -> bool:
    changed = False
    for suffix in ("final", "rejected"):
        changed |= _rename_key(message, f"_{OLD_STORY_KEY}_{suffix}", f"_story_{suffix}")
    return changed


def upgrade_package_story(bundle: dict) -> None:
    _rename_key(bundle, f"{OLD_STORY_KEY}_trace", "story_trace")
    for message in bundle["messages"]:
        _snapshot_fields(message, "snapshot")
    for trace in bundle.get("story_trace", []):
        _trace_message(trace["payload"])
    if bundle.get("adapter"):
        _rename_key(bundle["adapter"]["config"], OLD_STORY_KEY, "story")


def inspect_sessions(db: sqlite3.Connection, version: int, columns: set[str], root: Path) -> None:
    if "agent_mode" not in columns:
        if version >= 9 and db.execute("SELECT 1 FROM sessions LIMIT 1").fetchone():
            raise ValueError("旧数据库缺少识别故事格式的字段")
        return
    if db.execute(
        "SELECT 1 FROM sessions WHERE agent_mode NOT IN (?,?) OR agent_mode IS NULL LIMIT 1",
        (RETIRED_STORY_KEY, OLD_STORY_KEY),
    ).fetchone():
        raise ValueError("旧数据库包含无法识别的故事记录")
    if (
        version < 9
        and db.execute("SELECT 1 FROM sessions WHERE agent_mode=? LIMIT 1", (OLD_STORY_KEY,)).fetchone()
    ):
        raise ValueError(
            "旧对话不支持整轮重试格式；数据库未修改。请先停止酒馆服务，"
            f'在项目根目录运行 .\\clear-conversations.cmd --data "{root.resolve()}"'
            "（Linux 使用 .venv/bin/python clear-conversations.py，并传入相同的 --data 参数）。"
            "脚本会完整备份后删除全部剧情对话，保留角色卡、用户角色、设置和制卡资料；完成后重新启动。"
            "如需继续旧剧情，请使用兼容旧版打开原数据。"
        )


def discard_unsupported_stories(db: sqlite3.Connection, version: int) -> None:
    columns = {row["name"] for row in db.execute("PRAGMA table_info(sessions)")}
    if "agent_mode" in columns:
        delete_session_rows(db, "agent_mode=?", (RETIRED_STORY_KEY,))
    elif version < 9:
        delete_session_rows(db, "1")
    db.execute(f"DROP TABLE IF EXISTS {RETIRED_STORY_KEY}_trace")
    for column in OLD_SESSION_FIELDS:
        if column in columns:
            db.execute(f"ALTER TABLE sessions DROP COLUMN {column}")

    # Older drafts saved this field in the active version and its undo/resume copies.
    db.execute(
        "UPDATE card_edit_versions SET snapshot=json_remove(snapshot,'$.target_mode') "
        "WHERE json_type(snapshot,'$.target_mode') IS NOT NULL"
    )
    for table, paths in (
        ("card_author_conversations", ("$.cards",)),
        ("card_author_progress", ("$.working.cards", "$.base.cards")),
    ):
        for path in paths:
            db.execute(
                f"""
                UPDATE {table}
                SET payload=json_set(payload,?,(
                    SELECT json_group_array(json_remove(entry.value,'$.target_mode'))
                    FROM json_each(payload,?) AS entry
                ))
                WHERE EXISTS (SELECT 1 FROM json_each(payload,?) AS entry
                    WHERE json_type(entry.value,'$.target_mode') IS NOT NULL)
                """,
                (path, path, path),
            )


def rename_story_tables(db: sqlite3.Connection) -> None:
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for suffix in ("trace", "progress"):
        old = f"{OLD_STORY_KEY}_{suffix}"
        if old in tables:
            db.execute(f"ALTER TABLE {old} RENAME TO story_{suffix}")
    db.execute("DROP INDEX IF EXISTS full_trace_session")


def _saved_pending(pending: dict) -> bool:
    changed = _snapshot_fields(pending, "before")
    changed |= _rename_key(pending, f"{OLD_STORY_KEY}_trace", "story_trace")
    for message in pending.get("messages", []):
        changed |= _snapshot_fields(message, "snapshot")
    for trace in pending.get("story_trace", []):
        changed |= _trace_message(trace["message"])
    return changed


def _saved_progress(progress: dict) -> bool:
    changed = _snapshot_fields(progress, "snapshot")
    changed |= _snapshot_fields(progress.get("user", {}), "snapshot")
    for message in progress.get("continuation", []):
        changed |= _trace_message(message)
    for trace in progress.get("new_trace", []):
        changed |= _trace_message(trace["message"])
    return changed


def _frozen_story(progress: dict) -> bool:
    story = progress["story"]
    changed = _snapshot_fields(story, "snapshot")
    for message in story["messages"]:
        changed |= _snapshot_fields(message, "snapshot")
    return changed


def _turn_event(event: dict) -> bool:
    changed = _snapshot_fields(event, "before", "after")
    return _snapshot_fields(event.get("message", {}), "snapshot") or changed


def upgrade_story_records(db: sqlite3.Connection) -> None:
    # Rewrite only format-owned fields. User text, variables, tool bodies and
    # native model attachments can contain the same strings as ordinary data.
    def snapshot(record):
        return _rename_key(record, OLD_STORY_KEY, "story")

    rewrites = [
        ("messages", "snapshot", snapshot, ""),
        ("changes", "before_state", snapshot, ""),
        ("changes", "after_state", snapshot, ""),
        ("story_trace", "payload", _trace_message, ""),
        ("story_progress", "payload", _saved_progress, ""),
        ("turns", "pending", _saved_pending, ""),
        ("advisor_runs", "progress", _frozen_story, ""),
        ("events", "payload", _turn_event, "AND kind IN ('input_ready','awaiting_review')"),
    ]
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='message_adapter_configs'").fetchone():
        rewrites.append(("message_adapter_configs", "config", snapshot, "AND owner_kind='story'"))
    rewrite_json_records(db, rewrites, OLD_STORY_KEY)
    db.execute(
        "UPDATE events SET kind='story' || substr(kind,?) WHERE substr(kind,1,?)=?",
        (len(OLD_STORY_KEY) + 1, len(OLD_STORY_KEY) + 1, OLD_STORY_KEY + "_"),
    )
    db.execute(
        "UPDATE events SET payload=json_set(payload,'$.purpose','story') "
        "WHERE instr(payload,?)>0 AND json_extract(payload,'$.purpose')=?",
        (OLD_STORY_KEY, OLD_STORY_KEY),
    )
    db.execute(
        "UPDATE context_token_samples SET lane='story' || substr(lane,?) WHERE lane=? OR substr(lane,1,?)=?",
        (len(OLD_STORY_KEY) + 1, OLD_STORY_KEY, len(OLD_STORY_KEY) + 1, OLD_STORY_KEY + ":"),
    )
    # Saved requests used to include unused, null fields. Strip those before
    # the current request schema reads them for idempotency or continuation.
    for path in ("$.generate", "$.scene_refresh"):
        db.execute(
            "UPDATE turns SET request=json_remove(request,?) WHERE json_type(request,?)='null'", (path, path)
        )
