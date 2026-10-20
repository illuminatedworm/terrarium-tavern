"""Portable backup/restore. Restore only into a new empty directory."""

import hashlib
import json
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

from tavern.storage.store import Store


def backup(store: Store, destination: Path):
    if destination.exists():
        raise ValueError("备份目标已存在，请使用新文件名")
    with store.connection() as db:
        if db.execute("SELECT 1 FROM advisor_runs WHERE status IN ('queued','running')").fetchone():
            raise ValueError("请等待参谋 agent 结束或停止后备份")
        if db.execute("SELECT 1 FROM card_edit_runs WHERE status IN ('queued','running')").fetchone():
            raise ValueError("请等待编辑任务完成后备份")
        if db.execute("SELECT 1 FROM turns WHERE status IN ('queued','running')").fetchone():
            raise ValueError("请等待当前回合完成后备份")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as temporary:
        dbfile = Path(temporary) / "tavern.sqlite3"
        store.backup(dbfile)
        files = {"tavern.sqlite3": dbfile}
        for folder in ("blobs", "dependencies", "runs"):
            for path in (store.root / folder).rglob("*"):
                if path.is_file() and not path.name.endswith(("-wal", "-shm")):
                    files[path.relative_to(store.root).as_posix()] = path
        manifest = {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in files.items()}
        with zipfile.ZipFile(destination, "x", zipfile.ZIP_DEFLATED) as archive:
            for name, path in files.items():
                archive.write(path, name)
            archive.writestr("backup-manifest.json", json.dumps({"version": 1, "files": manifest}))


def restore(source: Path, destination: Path):
    if destination.exists() and any(destination.iterdir()):
        raise ValueError("恢复目标必须是新的空目录，不会覆盖已有剧情")
    with zipfile.ZipFile(source) as archive:
        infos = archive.infolist()
        if sum(i.file_size for i in infos) > 4 * 1024**3 or len(infos) > 100000:
            raise ValueError("备份大小超限")
        manifest = json.loads(archive.read("backup-manifest.json"))
        if manifest.get("version") != 1:
            raise ValueError("不支持的备份版本")
        for name, digest in manifest["files"].items():
            path = PurePosixPath(name)
            if (
                path.is_absolute()
                or ".." in path.parts
                or "\\" in name
                or ":" in name
                or ".chatgpt" in path.parts
            ):
                raise ValueError("备份包含非法路径")
            if hashlib.sha256(archive.read(name)).hexdigest() != digest:
                raise ValueError("备份校验失败")
        if "tavern.sqlite3" not in manifest["files"]:
            raise ValueError("备份缺少数据库")
        # Validate all entries before writing any restored data.
        destination.mkdir(parents=True, exist_ok=True)
        for name in manifest["files"]:
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.read(name))
    return Store(destination)
