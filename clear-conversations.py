"""离线清空剧情对话，保留角色卡、用户角色、世界书库、设置和制卡资料。

Windows 双击 clear-conversations.cmd；也可使用项目 Python 执行本文件：
  .venv/Scripts/python.exe clear-conversations.py --dry-run
  .venv/Scripts/python.exe clear-conversations.py --data "D:/MyStories"
  .venv/Scripts/python.exe clear-conversations.py --yes

先停止酒馆服务。默认操作根目录下的 data，相对路径也以项目根目录为准。
会删除所选目录的全部剧情对话，包括分支、未完成和待审核回合；并非只删除
某个版本的对话。只预览用 --dry-run，无交互执行用 --yes。

处理只发生在完整副本中，复用当前程序的结构升级、对话删除和资源引用规则。
全部校验成功后才切换目录；完整原目录保存在同级 backups 下，含密钥与剧情。
回退时停止服务，将当前数据目录另行改名，再把备份中的 original 目录移回
原数据路径。旧剧情仍需使用与其兼容的程序版本打开。

本工具不绑定某个 schema 数字。比当前程序更新、无法升级或无法完整清理的
格式会保留原数据并报错，不猜测未来格式，也不直接篡改数据库版本号。
"""

import argparse
import asyncio
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from contextlib import closing
from datetime import datetime
from pathlib import Path

PROJECT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT / "src"))


def quote(name):
    return '"' + name.replace('"', '""') + '"'


def connect(path, mode="ro"):
    return sqlite3.connect(path.resolve().as_uri() + f"?mode={mode}", uri=True, timeout=0)


def inspect_data(root):
    from tavern.core.settings import migrate_settings
    from tavern.storage.store import SCHEMA_VERSION, SETTINGS_VERSIONS

    if root == PROJECT or PROJECT.is_relative_to(root):
        raise ValueError("请选择运行数据目录，不能选择项目目录或它的上级目录")
    with closing(connect(root / "tavern.sqlite3")) as db:
        version = db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        if not version or not version[0].isdigit() or not 1 <= int(version[0]) <= SCHEMA_VERSION:
            raise ValueError(f"不支持此数据库格式；当前程序支持 schema 1–{SCHEMA_VERSION}，原数据未修改")
        settings = json.loads(db.execute("SELECT value FROM meta WHERE key='settings'").fetchone()[0])
        if int(version[0]) > 1 and settings.get("version") not in SETTINGS_VERSIONS:
            raise ValueError("设置版本与数据库版本不一致，原数据未修改")
        migrate_settings(settings)
        return {
            "schema": int(version[0]),
            **{
                table: db.execute(f"SELECT count(*) FROM {quote(table)}").fetchone()[0]
                for table in ("sessions", "messages", "cards", "personas")
            },
        }


def ensure_stopped():
    """Check the project's normal launch commands, including non-default ports."""
    if os.name == "nt":
        command = r"""
$ErrorActionPreference = 'Stop'
Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe' OR Name='uvicorn.exe' OR Name='tavern.exe'" |
    Where-Object { $_.Name -eq 'tavern.exe' -or $_.CommandLine -match '(-m\s+tavern(?:\s|$)|tavern\.application\.api)' } |
    ForEach-Object { $_.ProcessId }
"""
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True,
            check=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        running = result.stdout.strip()
    else:
        running = False
        for path in Path("/proc").glob("[0-9]*/cmdline"):
            try:
                command = path.read_bytes().replace(b"\0", b" ")
            except FileNotFoundError:
                continue  # A process may exit while /proc is being enumerated.
            if re.search(rb"(?:-m\s+tavern(?:\s|$)|tavern\.application\.api)", command):
                running = True
                break
    if running:
        raise ValueError("请先关闭酒馆服务（包括其他端口的服务），再运行清理脚本")


def inventory(root):
    """Reject links before copying or allowing application cleanup to remove files."""
    result = {}
    for directory, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            path = Path(directory) / name
            if path.is_symlink() or path.is_junction() or not path.resolve().is_relative_to(root):
                raise ValueError(f"数据目录包含链接或越界路径，未处理：{path}")
            if path.is_file():
                stat = path.stat()
                result[path.relative_to(root).as_posix()] = (stat.st_size, stat.st_mtime_ns)
    return result


def is_database_file(name):
    return name in ("tavern.sqlite3", "tavern.sqlite3-wal", "tavern.sqlite3-shm")


def is_upgrade_backup(name):
    return name.startswith("before-") and ".sqlite3" in name


def copy_data(root, staged):
    def ignore(directory, names):
        if Path(directory) == root:
            # Historical automatic backups remain in the complete original backup,
            # rather than bringing old story databases back into the cleaned data.
            return [name for name in names if is_database_file(name) or is_upgrade_backup(name)]
        return []

    shutil.copytree(root, staged, ignore=ignore)
    with closing(connect(root / "tavern.sqlite3")) as source:
        with closing(sqlite3.connect(staged / "tavern.sqlite3")) as target:
            source.backup(target)  # Includes committed WAL pages; plain file copying does not.


def prepare_store(staged, recovery):
    """Initialize a disposable copy without interpreting incompatible story snapshots."""
    from tavern.storage.store import Store

    with closing(connect(staged / "tavern.sqlite3", "rw")) as db, db:
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute("BEGIN IMMEDIATE")
        columns = [row[1] for row in db.execute("PRAGMA table_info(sessions)")]
        sessions = db.execute("SELECT * FROM sessions").fetchall()
        triggers = db.execute(
            "SELECT name,sql FROM sqlite_master WHERE type='trigger' AND tbl_name='sessions'"
        ).fetchall()
        # Keep every dependent row and the original session rows. Only this private
        # copy temporarily has no sessions, allowing the normal non-story upgrade.
        # The original directory is never opened with Store before deletion.
        for name, _ in triggers:
            db.execute(f"DROP TRIGGER {quote(name)}")
        db.execute("DELETE FROM sessions")
    store = Store(staged)
    with store.connection() as db:
        db.executemany(
            f"INSERT INTO sessions ({','.join(map(quote, columns))}) VALUES ({','.join('?' for _ in columns)})",
            sessions,
        )
        present = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
        for name, sql in triggers:
            if name not in present:
                db.execute(sql)
    # Store may have made its normal pre-upgrade backup. Keep it outside the
    # cleaned active directory; it still contains legacy messages and snapshots.
    for path in staged.iterdir():
        if path.is_file() and is_upgrade_backup(path.name):
            path.rename(recovery / path.name)
    return store, [dict(zip(columns, row, strict=True))["id"] for row in sessions]


def preserved_rows(db):
    """Compare retained data without printing private rows or holding large logs in memory."""
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    result = {}
    for table in sorted(tables):
        if table in {"meta", "cards", "personas", "worldbooks"} or table.startswith("card_"):
            query = f"SELECT * FROM {quote(table)} ORDER BY rowid"
        elif table == "message_adapter_configs":
            query = "SELECT * FROM message_adapter_configs WHERE owner_kind!='story' ORDER BY rowid"
        else:
            continue
        digest = hashlib.sha256()
        for row in db.execute(query):
            digest.update(repr(tuple(row)).encode("utf-8"))
            digest.update(b"\n")
        result[table] = digest.hexdigest()
    return result


async def delete_conversations(store, session_ids):
    from tavern.application.service import RoleplayRuntime
    from tavern.storage.request_debug import RequestDebugLog

    class OfflineDeletionRuntime(RoleplayRuntime):
        async def cancel(self, tid):
            # The live service is stopped and this is a disposable copy. There are
            # no running tasks to cancel. In particular, do not decode old pending
            # turns through the current retry/review format merely to delete them.
            pass

    with store.connection() as db:
        retained = preserved_rows(db)
        owners = {
            row[0]
            for table in ("turns", "advisor_runs", "context_operations")
            for row in db.execute(f"SELECT id FROM {quote(table)}")
        }
    runtime = OfflineDeletionRuntime(store)
    try:
        for index, sid in enumerate(session_ids, 1):
            await runtime.delete_session(sid)
            print(f"已清理剧情对话 {index}/{len(session_ids)}", flush=True)
    finally:
        await runtime.close()

    # All stories are gone, so no story retrieval cache has a surviving owner.
    # The per-session deletion already handled shared assets using the live
    # program's reference rules. Do not clear blobs, dependencies or runs wholesale.
    with store.connection() as db:
        for table in ("retrieval_collections", "retrieval_indexes", "retrieval_vectors"):
            db.execute(f"DELETE FROM {quote(table)}")
    logs = RequestDebugLog(store.root, store.settings_document().runtime("half_agent"))
    for path in logs.entries():
        if logs._protected(logs._metadata(path), owners):
            path.unlink()

    with store.connection() as db:
        if preserved_rows(db) != retained:
            raise ValueError("清理改变了应保留的非剧情资料，拒绝替换原数据")
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table in tables:
            columns = {row[1] for row in db.execute(f"PRAGMA table_info({quote(table)})")}
            # Detect new session-owned tables too, instead of trusting a frozen
            # table list after future application deletion logic changes.
            if columns & {"session", "session_id"} or table in {
                "sessions",
                "messages",
                "turns",
                "events",
                "changes",
                "full_agent_progress",
                "advisor_events",
            }:
                if db.execute(f"SELECT 1 FROM {quote(table)} LIMIT 1").fetchone():
                    raise ValueError(f"发现未清理的剧情记录：{table}；拒绝替换原数据")
        if db.execute("SELECT 1 FROM message_adapter_configs WHERE owner_kind='story' LIMIT 1").fetchone():
            raise ValueError("发现未清理的剧情消息适配设置，拒绝替换原数据")
        if db.execute("PRAGMA foreign_key_check").fetchone():
            raise ValueError("清理后关联检查失败，拒绝替换原数据")


def publish(staged, root, original):
    # Both moves are within the explicitly selected data directory's parent tree.
    # Keep the complete original directory, including credentials, models and WAL.
    parent = root.parent.resolve()
    if not staged.resolve().is_relative_to(parent) or not original.resolve().is_relative_to(parent):
        raise ValueError("备份或工作目录越界，未替换数据")
    if original.exists():
        raise FileExistsError(f"备份目录已存在：{original}")
    root.rename(original)
    try:
        staged.rename(root)
    except BaseException:
        # Includes Ctrl+C during the two-directory switch. Never leave the user
        # with an empty data path after an ordinary publish failure.
        original.rename(root)
        raise


def clear_conversations(root, expected):
    from filelock import FileLock

    from tavern.storage.store import Store

    ensure_stopped()
    backup_root = root.parent / ("tavern-backups" if root.name == "backups" else "backups")
    backup_root.mkdir(exist_ok=True)
    if backup_root.is_symlink() or backup_root.is_junction():
        raise ValueError("备份目录不能是链接")
    with FileLock(backup_root / f"clear-{root.name}.lock", timeout=0):
        # Block SQLite writers as well as a second copy of this script. No SQL
        # changes are made to the original; reads use separate read-only handles.
        with closing(connect(root / "tavern.sqlite3", "rw")) as guard:
            guard.execute("BEGIN IMMEDIATE")
            if inspect_data(root) != expected:
                raise ValueError("数据已变化，请重新预览后运行")
            before = inventory(root)
            recovery = backup_root / f"{root.name}-clear-{datetime.now():%Y%m%d-%H%M%S-%f}"
            recovery.mkdir()
            staged, original = recovery / "prepared", recovery / "original"
            print(
                f"工作及备份目录：{recovery}\n正在复制数据（包括模型与资源，请留出足够磁盘空间）……",
                flush=True,
            )
            copy_data(root, staged)
            store, session_ids = prepare_store(staged, recovery)
            asyncio.run(delete_conversations(store, session_ids))
            # Exercise the actual startup format check, without starting a server,
            # loading models, running scripts or contacting any external service.
            Store(staged)
            with closing(connect(staged / "tavern.sqlite3", "rw")) as db:
                if db.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                    raise ValueError("清理后数据库完整性检查失败，拒绝替换原数据")
                db.execute("VACUUM")
                db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                db.execute("PRAGMA journal_mode=DELETE")
            ensure_stopped()
            after = inventory(root)
            if {k: v for k, v in before.items() if not is_database_file(k)} != {
                k: v for k, v in after.items() if not is_database_file(k)
            }:
                raise ValueError("处理期间原目录中的文件发生变化，拒绝替换原数据")
            report = {
                "data": str(root),
                "backup": str(original),
                "before": expected,
                "restore": "停止服务，将当前数据目录另行改名，再将 original 目录移回 data 所指的路径。旧剧情用兼容旧版打开。",
            }
            (recovery / "recovery.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), "utf-8")
            guard.rollback()
        publish(staged, root, original)
    print(
        f"清理完成，可按原方式启动项目。\n保留：角色卡、用户角色、世界书库、设置、制卡资料及共享资源。\n完整备份：{original}"
    )
    return original


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--data", type=Path, default=Path("data"), help="运行数据目录（默认 data）")
    parser.add_argument("--dry-run", action="store_true", help="只预览，不写入或创建备份")
    parser.add_argument("--yes", action="store_true", help="已确认删除全部剧情对话，跳过交互确认")
    args = parser.parse_args()
    selected = args.data if args.data.is_absolute() else PROJECT / args.data
    if selected.is_symlink() or selected.is_junction():
        raise ValueError("数据目录不能是链接")
    root = selected.resolve(strict=True)
    info = inspect_data(root)
    print(f"数据目录：{root}\n数据库格式：schema {info['schema']}")
    print(f"将删除：{info['sessions']} 个剧情对话（含分支）、{info['messages']} 条消息及关联记录。")
    print(f"将保留：{info['cards']} 张角色卡、{info['personas']} 个用户角色、世界书库、设置及制卡资料。")
    if args.dry_run or not info["sessions"]:
        print("未修改任何数据。" if args.dry_run else "没有剧情对话，无需清理。")
        return
    print("请先停止酒馆服务。完整原目录会自动备份；包括当前版本的剧情对话也会被删除。")
    if not args.yes and input("确认备份并删除全部剧情对话？输入 y 继续 [y/N]：").strip().lower() != "y":
        print("已取消，未修改任何数据。")
        return
    clear_conversations(root, info)


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        print("\n已中止。处理副本不会覆盖原目录；如已输出备份路径，可从该路径恢复。", file=sys.stderr)
        sys.exit(130)
    except Exception as error:
        # A CLI boundary must report failure and return nonzero. Do not print
        # Pydantic input values: settings validation can contain API credentials.
        from pydantic import ValidationError

        detail = (
            f"设置或数据验证失败（{error.error_count()} 项）"
            if isinstance(error, ValidationError)
            else str(error)
        )
        print(
            f"\n处理失败：{detail}\n请保留上方工作及备份目录；未显示“清理完成”时不要将 prepared 目录当作成品。",
            file=sys.stderr,
        )
        sys.exit(1)
