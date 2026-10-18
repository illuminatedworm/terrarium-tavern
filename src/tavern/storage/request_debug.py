"""Bounded, opt-in HTTP evidence, independent of story/recovery state."""
import json
import uuid
import zipfile
from collections.abc import Collection
from pathlib import Path
from time import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tavern.core.models import Settings

MAX_RECORDS = 100
BODY_LIMIT = 8 * 1024**2


class RequestDebugLog:
    def __init__(self, data_root: Path, settings: "Settings"):
        self.root = (data_root / "runs" / "request-debug").resolve()
        if not self.root.is_relative_to(data_root.resolve()):
            raise ValueError("请求调试记录目录超出应用数据目录")
        self.days, self.max_bytes = settings.log_retention_days, settings.log_max_mib * 1024**2

    def path(self, identity: str) -> Path:
        path = self.root / (uuid.UUID(identity).hex + ".zip")
        if path.is_symlink() or not path.resolve().is_relative_to(self.root):
            raise ValueError("请求调试记录路径无效")
        return path

    def entries(self) -> list[Path]:
        return [p for p in self.root.glob("*.zip") if not p.is_symlink()]

    def records(self, offset: int = 0) -> list[dict]:
        records = []
        for path in self.entries():
            records.append({**self._metadata(path), "bytes": path.stat().st_size})
        return sorted(records, key=lambda r: (r["created"], r["id"]), reverse=True)[offset:offset + MAX_RECORDS]

    @staticmethod
    def _metadata(path: Path) -> dict:
        with zipfile.ZipFile(path) as archive:
            return json.loads(archive.read("metadata.json"))

    @staticmethod
    def _protected(metadata: dict, protected: Collection[str]) -> bool:
        return (metadata.get("turn_id") or "").split(":", 1)[0] in protected

    def plan(self, protected: Collection[str] = ()) -> tuple[list[Path], dict[str, int]]:
        entries = self.entries()
        protected_paths = set()
        created = {}
        for path in entries:
            metadata = self._metadata(path)
            created[path] = metadata["created"]
            if self._protected(metadata, protected):
                protected_paths.add(path)
        # Backup restore changes filesystem timestamps; the recorded request
        # time must still determine ordering and expiry.
        entries.sort(key=lambda p: (created[p], p.name), reverse=True)
        protected_bytes = sum(p.stat().st_size for p in protected_paths)
        total, kept, count, remove = sum(p.stat().st_size for p in entries), protected_bytes, len(protected_paths), []
        cutoff = time() - self.days * 86400
        for path in entries:
            if path in protected_paths:
                continue
            size = path.stat().st_size
            if created[path] < cutoff or count >= MAX_RECORDS or kept + size > self.max_bytes:
                remove.append(path)
            else:
                kept += size
                count += 1
        # An interrupted archive write is never published. Expired staging
        # files have the same lifecycle, without touching a current writer.
        for path in self.root.glob("*.tmp"):
            if not path.is_symlink():
                size = path.stat().st_size
                total += size
                if path.stat().st_mtime < cutoff:
                    remove.append(path)
                else:
                    kept += size
        return remove, {"request_debug_bytes": total, "request_debug_reclaimable_bytes": total - kept,
                        "request_debug_records": len(entries), "request_debug_protected_bytes": protected_bytes}

    def cleanup(self, protected=()):
        paths, report = self.plan(protected)
        for path in paths:
            if not path.resolve().is_relative_to(self.root) or path.is_symlink():
                raise ValueError("请求调试记录清理路径无效")
            path.unlink(missing_ok=True)
        return report

    def clear(self, protected: Collection[str] = ()) -> dict[str, int]:
        """Delete every published record except those of running or recoverable work.

        Staging files belong to a writer that may still be active; they keep the
        retention lifecycle of ``plan``.
        """
        removed = kept = 0
        for path in self.entries():
            if self._protected(self._metadata(path), protected):
                kept += 1
            else:
                path.unlink(missing_ok=True)
                removed += 1
        return {"removed": removed, "protected": kept}

    def save(self, metadata: dict, request_body: bytes, response_body: bytes | None) -> None:
        identity = uuid.uuid4().hex
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.path(identity)
        temporary = path.with_suffix(".tmp")
        with zipfile.ZipFile(temporary, "x", zipfile.ZIP_STORED) as archive:
            archive.writestr("metadata.json", json.dumps(
                {**metadata, "version": 1, "id": identity, "created": time()}, ensure_ascii=False, indent=2))
            archive.writestr("request.bin", request_body)
            if response_body is not None:
                archive.writestr("response.bin", response_body)
        temporary.replace(path)
