"""Selective, versioned personal data packages; imported stories own new identities."""

import copy
import hashlib
import io
import json
import re
import sqlite3
import time
import zipfile
import zlib
from contextlib import contextmanager
from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from tavern.character.card_workflow import validate_document
from tavern.character.cards import diagnose, normalize
from tavern.character.worldbooks import REGISTRY, validate_book
from tavern.core.kemini import AuxiliaryPart
from tavern.core.message_adapter import MessageAdapterConfig
from tavern.core.models import ContextRecipe, PersonaEdit, WorldbookEdit
from tavern.core.settings import SettingsDocument, migrate_settings
from tavern.memories.memory import fingerprint, window_valid
from tavern.search.retrieval_config import ModelProfile
from tavern.storage.store import Conflict, dump, uid, write_blob
from tavern.storage.worldbooks import read_defaults, write_defaults

MAX_UPLOAD = 256 * 1024 * 1024
MAX_UNPACKED = 512 * 1024 * 1024
FORMAT = "terrarium-tavern-personal-data"
JSON_FIELDS = {
    "worldbooks": {"book"},
    "cards": {"resources", "document", "report"},
    "sessions": {
        "model_selection",
        "recipe",
        "persona_data",
        "card_data",
        "disabled_tools",
        "tool_result_options",
        "cache_breakpoint_follow",
    },
    "messages": {"snapshot", "auxiliary"},
    "full_agent_trace": {"payload"},
    "half_agent_trace": {"payload"},
    "message_adapter_configs": {"config"},
}


class ExportSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    settings: bool = True
    include_api_keys: bool = False
    personas: list[str] = Field(default_factory=list)
    worldbooks: list[str] = Field(default_factory=list)
    cards: list[str] = Field(default_factory=list)
    sessions: list[str] = Field(default_factory=list)
    browser_preferences: dict = Field(default_factory=dict)


def browser_preferences(raw):
    """Only portable reading/layout preferences, never local navigation identities."""
    if not isinstance(raw, dict) or set(raw) - {"layout"}:
        raise ValueError("阅读偏好格式无效")
    layout = raw.get("layout", {})
    if not isinstance(layout, dict):
        raise ValueError("阅读偏好格式无效")
    limits = {
        "fontSize": (12, 40),
        "lineHeight": (1, 3),
        "readingWidth": (480, 2400),
        "rightWidth": (240, 800),
    }
    result = {}
    for key, value in layout.items():
        if key in limits:
            low, high = limits[key]
            if type(value) not in (int, float) or not low <= value <= high:
                raise ValueError("阅读偏好数值无效")
        elif key == "fontFamily":
            if value not in ("sans", "serif"):
                raise ValueError("阅读字体无效")
        elif key in ("leftCollapsed", "rightCollapsed", "glass"):
            if type(value) is not bool:
                raise ValueError("布局偏好格式无效")
        else:
            raise ValueError("未知阅读偏好")
        result[key] = value
    return {"layout": result} if result else {}


def decoded(table, row):
    result = dict(row)
    for field in JSON_FIELDS.get(table, ()):
        result[field] = json.loads(result[field])
    return result


def insert(db, table, row):
    # Tables and columns come from application code, never archive-supplied SQL.
    columns = {column["name"] for column in db.execute(f"PRAGMA table_info({table})")}
    if set(row) - columns:
        raise ValueError("数据包包含不支持的字段，请更新软件")
    keys = list(row)
    values = [dump(row[key]) if key in JSON_FIELDS.get(table, ()) else row[key] for key in keys]
    db.execute(f"INSERT INTO {table} ({','.join(keys)}) VALUES ({','.join('?' for _ in keys)})", values)


def remap(value, ids):
    if isinstance(value, dict):
        return {ids.get(key, key): remap(item, ids) for key, item in value.items()}
    if isinstance(value, list):
        return [remap(item, ids) for item in value]
    return ids.get(value, value) if isinstance(value, str) else value


def hashes(card):
    dependencies = card.get("dependencies", {})
    return {
        card["original"],
        *card["resources"].values(),
        *dependencies.get("scripts", {}).values(),
        *(resource["hash"] for resource in dependencies.get("resources", {}).values()),
    }


def export_package(store, selection):
    if not (
        selection.settings
        or selection.personas
        or selection.worldbooks
        or selection.cards
        or selection.sessions
    ):
        raise ValueError("请至少选择一种数据")
    package = {
        "format": FORMAT,
        "version": 5,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "includes_api_keys": selection.settings and selection.include_api_keys,
        "settings": None,
        "personas": [],
        "worldbooks": [],
        "cards": [],
        "sessions": [],
    }
    blobs = set()

    def pin(card):
        card = copy.deepcopy(card)
        card["dependencies"] = store.card_manifest(card)
        blobs.update(hashes(card))
        return card

    with store.connection() as db:
        db.execute("BEGIN")  # All selected database rows share one read snapshot.
        if selection.settings:
            document = SettingsDocument.model_validate_json(
                db.execute("SELECT value FROM meta WHERE key='settings'").fetchone()[0]
            )
            raw = document.model_dump()
            if not selection.include_api_keys:
                for provider in raw["shared"]["providers"]:
                    provider["api_key"] = ""
                for name in ("exa", "tavily"):
                    raw["shared"]["web"][name]["api_key"] = ""
            package["settings"] = {
                "document": raw,
                "browser": browser_preferences(selection.browser_preferences),
            }
        for table in ("personas", "worldbooks", "cards", "sessions"):
            for identity in dict.fromkeys(getattr(selection, table)):
                row = db.execute(f"SELECT * FROM {table} WHERE id=?", (identity,)).fetchone()
                if row is None:
                    raise ValueError("选中的数据已被删除，请刷新后重试")
                if table == "personas":
                    package[table].append(dict(row))
                elif table == "worldbooks":
                    package[table].append(decoded(table, row))
                elif table == "cards":
                    if db.execute(
                        "SELECT 1 FROM card_edit_runs r JOIN card_edits e ON r.workspace=e.id "
                        "WHERE e.card=? AND r.status IN ('queued','running')",
                        (identity,),
                    ).fetchone():
                        raise Conflict("请等待制卡 agent 任务完成后导出")
                    card = pin(decoded(table, row))
                    defaults = read_defaults(db, identity)
                    if defaults["version"]:
                        # Defaults travel only with the personas and books packed beside them.
                        persona = defaults["persona_id"]
                        card["defaults"] = {
                            "persona_id": persona if persona in selection.personas else None,
                            "worldbook_ids": [
                                wid for wid in defaults["worldbook_ids"] if wid in selection.worldbooks
                            ],
                        }
                    package[table].append(card)
                else:
                    if db.execute(
                        "SELECT 1 FROM turns WHERE session=? AND status IN ('queued','running','awaiting_review')",
                        (identity,),
                    ).fetchone():
                        raise Conflict("请先完成或取消所选对话的当前回合，再导出")
                    session = decoded(table, row)
                    session["card_data"] = pin(session["card_data"])
                    bundle = {"session": session}
                    from tavern.advisor.transfer import export_bundle as export_advisor

                    bundle["advisor"] = export_advisor(db, identity)
                    for related in ("messages", "full_agent_trace", "half_agent_trace"):
                        bundle[related] = [
                            decoded(related, item)
                            for item in db.execute(
                                f"SELECT * FROM {related} WHERE session=? ORDER BY rowid", (identity,)
                            )
                        ]
                    adapter = db.execute(
                        "SELECT * FROM message_adapter_configs WHERE owner_kind='story' AND owner_id=?",
                        (identity,),
                    ).fetchone()
                    bundle["adapter"] = decoded("message_adapter_configs", adapter) if adapter else None
                    package[table].append(bundle)
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("dataset.json", dump(package))
        for digest in sorted(blobs):
            if not re.fullmatch(r"[a-f0-9]{64}", digest):
                raise ValueError("角色资源标识无效")
            path = store.root / "blobs" / digest
            if not path.is_file():
                raise ValueError("角色资源缺失，无法创建完整数据包")
            archive.write(path, "blobs/" + digest)
    result = output.getvalue()
    if len(result) > MAX_UPLOAD:
        raise ValueError("数据包超过 256 MiB，请分批导出")
    # Keep export and import limits symmetric, including uncompressed size.
    read_package(result)
    return result


def validate_row(schema, table, row, required):
    if not isinstance(row, dict) or not required <= set(row):
        raise ValueError("数据包记录不完整")
    columns = schema[table]
    if set(row) - set(columns):
        raise ValueError("数据包包含不支持的字段，请更新软件")
    for key, value in row.items():
        if key in JSON_FIELDS.get(table, ()):
            if not isinstance(value, (dict, list)):
                raise ValueError("数据包状态格式无效")
        elif value is None:
            if columns[key]["notnull"]:
                raise ValueError("数据包必填字段不能为空")
        else:
            kind = columns[key]["type"]
            if (
                (kind == "TEXT" and not isinstance(value, str))
                or (kind == "INTEGER" and type(value) is not int)
                or (kind == "REAL" and (type(value) not in (int, float) or not 0 <= value < 1e12))
            ):
                raise ValueError("数据包字段类型无效")


def validate_full_sources(messages):
    """Check imported immutable citations against every branch in linear tree time."""
    children = {}
    for message in messages.values():
        children.setdefault(message["parent"], []).append(message["id"])
    entered, exited, depths = {}, {}, {}
    stack = [(mid, 1, False) for mid in children.get(None, [])]
    while stack:
        mid, depth, leaving = stack.pop()
        if leaving:
            exited[mid] = len(entered)
        else:
            entered[mid], depths[mid] = len(entered), depth
            stack.append((mid, depth, True))
            stack.extend((child, depth + 1, False) for child in children.get(mid, []))

    def position(source, owner):
        if source not in entered or not entered[source] <= entered[owner] < exited[source]:
            raise ValueError("记忆来源不在所属剧情分支")
        return depths[source]

    def points(items, owner, span=None):
        for point in items:
            for ref in point["refs"]:
                first, last = position(ref["start"], owner), position(ref["end"], owner)
                if first > last or (span and not span[0] <= first <= last <= span[1]):
                    raise ValueError("交接要点的引用范围无效")

    for mid, message in messages.items():
        state = message["snapshot"]["full_agent"]
        for note in state["notes"].values():
            for source in note["sources"]:
                position(source["message_id"], mid)
        through = 0
        for chapter in state["handoff"]["chapters"]:
            first, last = position(chapter["start"], mid), position(chapter["end"], mid)
            if first != through + 1 or last < first:
                raise ValueError("交接章节必须按当前分支连续覆盖")
            points(chapter["points"], mid, (first, last))
            through = last
        points(state["handoff"]["current"], mid)


def validate_package(store, package, blobs):
    if package.get("format") != FORMAT or package.get("version") not in (1, 2, 3, 4, 5):
        raise ValueError("不支持的数据包格式或版本，请选择本软件导出的个人数据 ZIP")
    if type(package.get("includes_api_keys")) is not bool:
        raise ValueError("数据包密钥选项无效")
    if package["version"] < 4:
        # Older packages predate the worldbook library. Validation runs again on
        # import, so filling the missing list must stay idempotent.
        package.setdefault("worldbooks", [])
    for key in ("personas", "worldbooks", "cards", "sessions"):
        if not isinstance(package.get(key), list):
            raise ValueError("数据包清单无效")
    if package["sessions"] and package["version"] < 3:
        raise ValueError("旧对话不支持整轮重试格式；请只导入角色卡、设置等非对话资料")
    identities = set()
    with store.connection() as db:
        schema = {
            table: {column["name"]: dict(column) for column in db.execute(f"PRAGMA table_info({table})")}
            for table in ("personas", *JSON_FIELDS)
        }

    def identity(value):
        if not isinstance(value, str) or not 1 <= len(value) <= 128 or value in identities:
            raise ValueError("数据包标识无效或重复")
        identities.add(value)

    def card(value, library=False):
        raw = {
            key: item for key, item in value.items() if key not in ("dependencies", "session_id", "defaults")
        }
        validate_row(
            schema,
            "cards",
            raw,
            {"id", "name", "original", "container", "resources", "document", "report", "permission"},
        )
        if library:
            identity(raw["id"])
        validate_document(raw["document"])
        if not normalize(raw["document"]).get("name") or raw["permission"] not in (0, 1):
            raise ValueError("角色卡名称或权限无效")
        dependencies = value.get("dependencies", {})
        if (
            not isinstance(raw["resources"], dict)
            or not isinstance(dependencies, dict)
            or not isinstance(dependencies.get("resources", {}), dict)
            or not isinstance(dependencies.get("scripts", {}), dict)
        ):
            raise ValueError("角色资源清单无效")
        try:
            required = hashes(value)
        except (KeyError, TypeError):
            raise ValueError("角色资源清单无效") from None
        if any(not isinstance(digest, str) or digest not in blobs for digest in required):
            raise ValueError("数据包缺少角色资源")
        if "defaults" in value:
            defaults = value["defaults"]
            books = defaults.get("worldbook_ids") if isinstance(defaults, dict) else None
            if (
                not library
                or set(defaults) != {"persona_id", "worldbook_ids"}
                or defaults["persona_id"] not in (None, *packed_personas)
                or not isinstance(books, list)
                or len(set(books)) != len(books)
                or not set(books) <= packed_worldbooks
            ):
                raise ValueError("角色卡默认项引用了数据包外的数据")

    packed_personas = {row.get("id") for row in package["personas"] if isinstance(row, dict)}
    packed_worldbooks = {row.get("id") for row in package["worldbooks"] if isinstance(row, dict)}

    try:
        preferences = package.get("settings")
        if preferences is not None:
            preferences["document"] = migrate_settings(preferences["document"]).model_dump()
            browser_preferences(preferences.get("browser", {}))
        for persona in package["personas"]:
            validate_row(schema, "personas", persona, {"id", "version", "name", "description"})
            PersonaEdit.model_validate({"name": persona["name"], "description": persona["description"]})
            identity(persona["id"])
        for book in package["worldbooks"]:
            validate_row(schema, "worldbooks", book, {"id", "name", "book", "revision", "created"})
            WorldbookEdit.model_validate({"name": book["name"], "book": book["book"]})
            validate_book(book["name"], book["book"])
            identity(book["id"])
        for value in package["cards"]:
            card(value, True)
        for bundle in package["sessions"]:
            session = bundle["session"]
            from tavern.advisor.transfer import validate_bundle as validate_advisor

            validate_advisor(bundle.get("advisor"), session.get("channel_origin"))
            validate_row(
                schema,
                "sessions",
                session,
                {
                    "id",
                    "card",
                    "head",
                    "persona",
                    "recipe",
                    "created",
                    "card_data",
                    "persona_data",
                    "lore_injection_mode",
                    "agent_mode",
                },
            )
            identity(session["id"])
            ContextRecipe.model_validate(session["recipe"])
            card(session["card_data"])
            if session["agent_mode"] not in ("half_agent", "full_agent") or session[
                "lore_injection_mode"
            ] not in ("conversation", "st_top"):
                raise ValueError("对话运行模式无效")
            planning = session.get("plot_enabled", 0)
            if (
                type(planning) not in (bool, int)
                or planning not in (0, 1)
                or planning
                and session["agent_mode"] != "full_agent"
            ):
                raise ValueError("剧情规划开关无效")
            if session.get("model_selection"):
                from tavern.core.model_selection import ModelSelection

                ModelSelection.model_validate(session["model_selection"]).profile(ModelProfile())
            if session.get("channel_origin") not in (None, "standard", "chatgpt"):
                raise ValueError("对话渠道无效")
            kemini = session.get("kemini", 0)
            if (
                type(kemini) not in (bool, int)
                or kemini not in (0, 1)
                or kemini
                and session.get("channel_origin") != "standard"
            ):
                raise ValueError("Kemini 实验对话标记无效")
            if session.get("context_strategy", "new_context") not in ("new_context", "openai_compact"):
                raise ValueError("上下文模式无效")
            if (
                session.get("context_strategy") == "openai_compact"
                and session.get("channel_origin") != "chatgpt"
            ):
                raise ValueError("此渠道不能导入云端 compact 会话")
            if session.get("policy") not in (None, "automatic", "review", "directed", "evolving", "preview"):
                raise ValueError("对话更新策略无效")
            if not isinstance(session["persona_data"], dict):
                raise ValueError("对话用户角色无效")
            messages = bundle["messages"]
            by_id = {}
            for message in messages:
                required = {
                    "id",
                    "session",
                    "parent",
                    "role",
                    "raw",
                    "model",
                    "display",
                    "snapshot",
                    "created",
                    "auxiliary",
                }
                validate_row(schema, "messages", message, required)
                if not isinstance(message["auxiliary"], list):
                    raise ValueError("实验辅助记录必须是列表")
                message["auxiliary"] = [
                    AuxiliaryPart.model_validate(part).model_dump() for part in message["auxiliary"]
                ]
                identity(message["id"])
                if (
                    message["session"] != session["id"]
                    or message["role"] not in ("assistant", "user")
                    or not isinstance(message["snapshot"], dict)
                ):
                    raise ValueError("对话消息无效")
                snapshot = message["snapshot"]
                if session["agent_mode"] == "full_agent":
                    from tavern.full_agent.state import read_full_state

                    if not snapshot.get("full_agent"):
                        raise ValueError("对话缺少 Full Agent 状态")
                    read_full_state(snapshot)
                else:
                    from tavern.memories.memory_records import read_memory

                    if not snapshot.get("memory_document"):
                        raise ValueError("对话缺少 Half Agent 记忆")
                    read_memory(snapshot)
                for key in ("state", "scopes"):
                    if not isinstance(snapshot.get(key, {}), dict):
                        raise ValueError("对话变量状态无效")
                for key in ("context_window", "memory_archive"):
                    window = snapshot.get(key)
                    if window and (
                        not isinstance(window, dict)
                        or type(window.get("covered_count")) is not int
                        or window["covered_count"] < 0
                        or not isinstance(window.get("fingerprint"), str)
                    ):
                        raise ValueError("对话历史窗口无效")
                if snapshot.get("content"):
                    validate_document(snapshot["content"]["document"])
                for item in snapshot.get(REGISTRY, []):
                    if (
                        set(item) != {"id", "name", "revision", "entries"}
                        or not isinstance(item["id"], str)
                        or not isinstance(item["name"], str)
                        or type(item["revision"]) is not int
                        or not isinstance(item["entries"], list)
                        or not all(isinstance(eid, str) for eid in item["entries"])
                    ):
                        raise ValueError("对话挂载世界书记录无效")
                by_id[message["id"]] = message
            if session["head"] is not None and session["head"] not in by_id:
                raise ValueError("对话当前进度缺失")
            # Validate every branch in linear time, including inactive alternatives.
            done = set()
            for mid in by_id:
                seen, cursor = set(), mid
                while cursor is not None and cursor not in done:
                    if cursor in seen or cursor not in by_id:
                        raise ValueError("对话分支无效")
                    seen.add(cursor)
                    cursor = by_id[cursor]["parent"]
                done.update(seen)
            if session["agent_mode"] == "full_agent":
                validate_full_sources(by_id)
            for table in ("full_agent_trace", "half_agent_trace"):
                trace_keys = set()
                for trace in bundle.get(table, []):
                    validate_row(
                        schema, table, trace, {"session", "message_id", "seq", "window_id", "payload"}
                    )
                    key = (trace["message_id"], trace["seq"] if table == "full_agent_trace" else 0)
                    if (
                        trace["session"] != session["id"]
                        or trace["message_id"] not in by_id
                        or key in trace_keys
                        or not isinstance(trace["payload"], dict)
                    ):
                        raise ValueError("对话上下文轨迹无效")
                    trace_keys.add(key)
            for key, expected_type in (
                ("disabled_tools", list),
                ("tool_result_options", dict),
                ("cache_breakpoint_follow", dict),
            ):
                if key in session and not isinstance(session[key], expected_type):
                    raise ValueError("对话选项格式无效")
            adapter = bundle.get("adapter")
            if adapter:
                validate_row(
                    schema,
                    "message_adapter_configs",
                    adapter,
                    {"owner_kind", "owner_id", "revision", "config"},
                )
                lanes = (
                    {"full_agent"} if session["agent_mode"] == "full_agent" else {"foreground", "background"}
                )
                if (
                    adapter["owner_kind"] != "story"
                    or adapter["owner_id"] != session["id"]
                    or set(adapter["config"]) != lanes
                ):
                    raise ValueError("对话消息适配配置无效")
                for config in adapter["config"].values():
                    MessageAdapterConfig.model_validate(config)
    except ValidationError:
        raise ValueError("数据包包含无效设置或记录") from None
    except (KeyError, TypeError, AttributeError):
        raise ValueError("数据包记录不完整或格式无效") from None


def read_package(content, store=None):
    if len(content) > MAX_UPLOAD:
        raise ValueError("数据包超过 256 MiB，请分批导入")
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            infos = archive.infolist()
            names = [item.filename for item in infos]
            if (
                len(infos) > 20000
                or sum(item.file_size for item in infos) > MAX_UNPACKED
                or len(set(names)) != len(names)
                or "dataset.json" not in names
            ):
                raise ValueError("数据包清单无效或解压大小超过 512 MiB")
            if any(
                name != "dataset.json" and not re.fullmatch(r"blobs/[a-f0-9]{64}", name) for name in names
            ):
                raise ValueError("数据包包含不支持的文件路径")
            if archive.getinfo("dataset.json").file_size > 64 * 1024 * 1024:
                raise ValueError("数据清单过大，请分批导入")
            package = json.loads(archive.read("dataset.json"))
            blobs = {name[6:]: archive.read(name) for name in names if name.startswith("blobs/")}
            if any(hashlib.sha256(data).hexdigest() != digest for digest, data in blobs.items()):
                raise ValueError("数据包资源校验失败，文件可能已损坏")
    except (
        zipfile.BadZipFile,
        UnicodeError,
        json.JSONDecodeError,
        RuntimeError,
        NotImplementedError,
        EOFError,
        zlib.error,
    ):
        raise ValueError("无法读取数据包，请选择完整的个人数据 ZIP") from None
    if not isinstance(package, dict):
        raise ValueError("数据包清单无效")
    if store:
        validate_package(store, package, blobs)
    return package, blobs


def preview(package):
    return {
        "created_at": package.get("created_at"),
        "includes_api_keys": bool(package.get("includes_api_keys")),
        "settings": package.get("settings") is not None,
        "personas": [{"id": row["id"], "name": row["name"]} for row in package["personas"]],
        "worldbooks": [{"id": row["id"], "name": row["name"]} for row in package["worldbooks"]],
        "cards": [{"id": row["id"], "name": row["name"]} for row in package["cards"]],
        "sessions": [
            {
                "id": item["session"]["id"],
                "name": item["session"].get("title") or "对话",
                "agent_mode": item["session"]["agent_mode"],
                "messages": len(item["messages"]),
            }
            for item in package["sessions"]
        ],
    }


@contextmanager
def import_transaction(store):
    with store.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        written = []
        try:
            yield db, written
            # Handle commit failures here too, before the connection releases
            # any write lock that SQLite still holds.
            db.commit()
        except BaseException:
            # SQLITE_FULL/IOERR can roll back and release the lock before raising.
            # Once unlocked, another writer may already own these blobs: retain
            # possible orphans rather than deleting a successful import's data.
            if db.in_transaction:
                for path in reversed(written):
                    path.unlink(missing_ok=True)
            raise


def import_package(store, package, blobs, restore_settings=False, expected_revisions=None):
    validate_package(store, package, blobs)
    ids = {row["id"]: uid() for key in ("personas", "worldbooks", "cards") for row in package[key]}
    for bundle in package["sessions"]:
        ids[bundle["session"]["id"]] = uid()
        ids.update({row["id"]: uid() for row in bundle["messages"]})
    result = {
        "personas": [],
        "worldbooks": [],
        "cards": [],
        "sessions": [],
        "settings_restored": False,
        "browser_preferences": {},
    }
    try:
        with import_transaction(store) as (db, written):
            if restore_settings:
                if not package.get("settings"):
                    raise ValueError("数据包未包含个人设置")
                if (
                    db.execute(
                        "SELECT 1 FROM turns WHERE status IN ('queued','running','awaiting_review')"
                    ).fetchone()
                    or db.execute(
                        "SELECT 1 FROM card_edit_runs WHERE status IN ('queued','running')"
                    ).fetchone()
                    or db.execute(
                        "SELECT 1 FROM advisor_runs WHERE status IN ('queued','running')"
                    ).fetchone()
                    or db.execute(
                        "SELECT 1 FROM context_operations WHERE status IN ('queued','running')"
                    ).fetchone()
                ):
                    raise Conflict("请先完成或取消运行中的任务，再恢复个人设置")
                current = SettingsDocument.model_validate_json(
                    db.execute("SELECT value FROM meta WHERE key='settings'").fetchone()[0]
                )
                if expected_revisions != current.revisions:
                    raise Conflict("个人设置已改变，请重新预览后导入")
                incoming = migrate_settings(package["settings"]["document"])
                if not package.get("includes_api_keys"):
                    # Preserve local secrets only for the exact same provider identity and endpoint.
                    keys = {(p.id, p.base_url): p.api_key for p in current.shared.providers}
                    for provider in incoming.shared.providers:
                        provider.api_key = keys.get((provider.id, provider.base_url), "")
                    for name in ("exa", "tavily"):
                        getattr(incoming.shared.web, name).api_key = getattr(current.shared.web, name).api_key
                incoming.revisions = {scope: revision + 1 for scope, revision in current.revisions.items()}
                for profile in (
                    incoming.full_agent.model,
                    incoming.half_agent.foreground,
                    incoming.half_agent.background,
                    incoming.shared.card_author,
                ):
                    profile.capability, profile.capability_key = "unknown", ""
                db.execute("UPDATE meta SET value=? WHERE key='settings'", (incoming.model_dump_json(),))
                result.update(
                    settings_restored=True,
                    browser_preferences=browser_preferences(package["settings"].get("browser", {})),
                )
            for digest, data in blobs.items():
                path = store.root / "blobs" / digest
                if path.exists():
                    if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                        raise ValueError("本地资源校验失败")
                else:
                    write_blob(path, data)
                    written.append(path)
            for persona in package["personas"]:
                row = copy.deepcopy(persona)
                row.update(id=ids[row["id"]], version=1)
                insert(db, "personas", row)
                result["personas"].append(row["id"])
            for book in package["worldbooks"]:
                row = copy.deepcopy(book)
                # Keep the packed revision: imported mount records compare against it.
                row.update(id=ids[row["id"]])
                insert(db, "worldbooks", row)
                result["worldbooks"].append(row["id"])
            for card in package["cards"]:
                row = copy.deepcopy(card)
                dependencies = row.pop("dependencies", {})
                defaults = row.pop("defaults", None)
                row.pop("session_id", None)
                row.update(id=ids[row["id"]], revision=0, report=diagnose(row["document"]))
                insert(db, "cards", row)
                if defaults:
                    write_defaults(
                        db,
                        row["id"],
                        ids.get(defaults["persona_id"]),
                        [ids[wid] for wid in defaults["worldbook_ids"]],
                        1,
                    )
                path = store.card_manifest_path(row)
                path.parent.mkdir(exist_ok=True)
                written.append(path)
                path.write_text(dump(dependencies), encoding="utf-8")
                result["cards"].append(row["id"])
            for bundle in package["sessions"]:
                source = bundle["session"]
                row = remap(source, ids)
                row["model_selection"] = copy.deepcopy(source.get("model_selection", {}))
                # A session owns its pinned card even when the library card was deleted.
                row["card"] = ids.get(source["card"], uid())
                row["card_data"]["id"] = row["card"]
                row["card_data"]["session_id"] = row["id"]
                row.update(revision=0, created=time.time())
                insert(db, "sessions", row)
                messages = {message["id"]: message for message in bundle["messages"]}
                window_fingerprints = {}
                for message in bundle["messages"]:
                    cloned = remap(message, ids)
                    from tavern.conversation.half_compaction import STATE, remap_state

                    if STATE in message["snapshot"]:
                        cloned["snapshot"][STATE] = copy.deepcopy(message["snapshot"][STATE])
                        remap_state(cloned["snapshot"], ids)
                    # User-authored strings are content, not identity references.
                    for key in ("raw", "model", "display", "retrieval_query", "auxiliary"):
                        if key in message:
                            cloned[key] = message[key]
                    for key in ("context_window", "memory_archive"):
                        window = message["snapshot"].get(key)
                        if window and isinstance(window, dict):
                            cache_key = (window.get("covered_count"), window.get("fingerprint"))
                            if cache_key in window_fingerprints:
                                if window_fingerprints[cache_key]:
                                    cloned["snapshot"][key]["fingerprint"] = window_fingerprints[cache_key]
                                continue
                            prefix, cursor = [], message["id"]
                            while cursor is not None:
                                prefix.append(messages[cursor])
                                cursor = messages[cursor]["parent"]
                            prefix.reverse()
                            if window_valid(window, prefix):
                                new_fingerprint = fingerprint(
                                    [{**m, "id": ids[m["id"]]} for m in prefix[: window["covered_count"]]]
                                )
                                cloned["snapshot"][key]["fingerprint"] = new_fingerprint
                                window_fingerprints[cache_key] = new_fingerprint
                            else:
                                window_fingerprints[cache_key] = None
                    insert(db, "messages", cloned)
                for table in ("full_agent_trace", "half_agent_trace"):
                    for trace in bundle.get(table, []):
                        cloned = remap(trace, ids)
                        if table == "half_agent_trace":
                            from tavern.conversation.half_context import remap_record

                            cloned["payload"] = remap_record(trace["payload"], ids)
                            cloned["window_id"] = cloned["payload"]["window"]
                        insert(db, table, cloned)
                adapter = bundle.get("adapter")
                from tavern.advisor.transfer import import_bundle as import_advisor

                import_advisor(db, row["id"], bundle.get("advisor"), ids)
                if adapter:
                    insert(db, "message_adapter_configs", {**remap(adapter, ids), "revision": 0})
                else:
                    store.create_message_adapter(db, "story", row["id"], row["agent_mode"])
                result["sessions"].append(row["id"])
    except sqlite3.IntegrityError:
        raise ValueError("数据包记录不完整或存在冲突，未导入任何数据") from None
    return result
