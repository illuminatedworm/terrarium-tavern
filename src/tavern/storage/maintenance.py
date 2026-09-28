"""Bound derived data without deleting any story or recoverable transaction.

Plans are recomputed under SQLite's write lock before applying. Files are named
by known turn IDs and confined to runs/, never inferred from arbitrary payloads.
"""
import json
import time
from collections import defaultdict
from pathlib import Path

from tavern.storage.store import Conflict, dump


def _files(root: Path):
    if not root.exists():
        return []
    resolved = root.resolve()
    return [p for p in root.rglob("*") if p.is_file() and not p.is_symlink()
            and p.resolve().is_relative_to(resolved)]


def _plan(store, db, now):
    settings = store.settings()
    rows = db.execute("""SELECT t.id,t.status,t.created,s.revision,
        json_extract(t.request,'$.expected_revision') AS expected_revision,
        length(CAST(coalesce(t.preview,'') AS BLOB)) AS preview_bytes,
        length(CAST(t.draft AS BLOB)) AS draft_bytes
        FROM turns t JOIN sessions s ON s.id=t.session ORDER BY t.created""").fetchall()
    run_files = _files(store.root / "runs")
    run_groups = defaultdict(list)
    for path in run_files:
        owner = path.relative_to(store.root / "runs").parts[0].split(".", 1)[0].removesuffix("-maintenance")
        run_groups[owner].append(path)
    turns = []
    for row in rows:
        protected = row["status"] in ("queued", "running", "awaiting_review") or (
            row["status"] in ("error", "cancelled", "interrupted")
            and row["expected_revision"] == row["revision"])
        files = run_groups[row["id"]]
        events = db.execute("""SELECT seq,kind,length(CAST(payload AS BLOB)) AS bytes
            FROM events WHERE turn=? AND coalesce(json_extract(payload,'$.detail_expired'),0)=0""",
                            (row["id"],)).fetchall()
        detailed = [e for e in events
                    if e["kind"] not in ("usage", "error", "memory_failed", "committed", "cancelled", "started")]
        size = sum(p.stat().st_size for p in files)
        size += sum(e["bytes"] for e in detailed)
        size += row["preview_bytes"]
        if row["status"] == "committed":
            size += row["draft_bytes"]
        turns.append({"id": row["id"], "created": row["created"], "protected": protected,
                      "bytes": size, "files": files, "events": detailed})
    total = sum(t["bytes"] for t in turns)
    remaining, selected = total, []
    for turn in turns:
        expired = turn["created"] < now - settings.log_retention_days * 86400
        if turn["bytes"] and not turn["protected"] and (expired or remaining > settings.log_max_mib * 1024**2):
            selected.append(turn)
            remaining -= turn["bytes"]
    cached = []
    for table in ("retrieval_indexes", "retrieval_vectors"):
        if db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone():
            cached.extend({"table": table, "key": r["key"], "bytes": r["bytes"], "rowid": r["rowid"]}
                          for r in db.execute(f"SELECT rowid,key,length(CAST(value AS BLOB)) AS bytes FROM {table}"))
    cache_total = sum(r["bytes"] for r in cached)
    # Collections record current references. Remove obsolete versions first;
    # current derived indexes can be rebuilt when quota requires eviction.
    references = set()
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='retrieval_collections'").fetchone():
        for r in db.execute("SELECT keys FROM retrieval_collections WHERE session IN (SELECT id FROM sessions)"):
            references.update(json.loads(r[0]))
    cache_remove, cache_remaining = [], cache_total
    for row in sorted(cached, key=lambda r: (r["key"] in references, r["rowid"])):
        if row["key"] not in references or cache_remaining > settings.retrieval_cache_max_mib * 1024**2:
            cache_remove.append(row)
            cache_remaining -= row["bytes"]
    return turns, selected, cache_remove, {
        "detail_bytes": total, "reclaimable_bytes": total - remaining,
        "protected_bytes": sum(t["bytes"] for t in turns if t["protected"]),
        "remaining_detail_bytes": remaining, "log_limit_bytes": settings.log_max_mib * 1024**2,
        "cache_bytes": cache_total, "reclaimable_cache_bytes": cache_total - cache_remaining,
        "turns_to_compact": len(selected), "cache_items_to_remove": len(cache_remove),
        "retention_days": settings.log_retention_days,
    }


def storage_report(store):
    with store.connection() as db:
        report = _plan(store, db, time.time())[3]
    report["directories"] = {name: sum(p.stat().st_size for p in _files(store.root / name))
                             for name in ("blobs", "runs", "dependencies", "models")}
    report["database_bytes"] = store.path.stat().st_size
    return report


def cleanup(store, *, vacuum=False):
    from tavern.conversation.work_records import LABELS

    files = []
    with store.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        if db.execute("SELECT 1 FROM card_edit_runs WHERE status IN ('queued','running')").fetchone():
            raise Conflict("请先结束角色卡编辑任务，再清理存储")
        if db.execute("SELECT 1 FROM turns WHERE status IN ('queued','running','awaiting_review')").fetchone():
            raise Conflict("请先结束运行或待审核回合，再清理存储")
        from tavern.storage.retrieval_cleanup import prune_orphan_retrieval
        prune_orphan_retrieval(db)
        _, selected, cached, report = _plan(store, db, time.time())
        for turn in selected:
            for event in turn["events"]:
                payload = json.loads(db.execute("SELECT payload FROM events WHERE seq=?", (event["seq"],)).fetchone()[0])
                if not isinstance(payload, dict):
                    payload = {"message": str(payload)}
                if event["kind"] == "text":
                    db.execute("DELETE FROM events WHERE seq=?", (event["seq"],))
                else:
                    compact = {"detail_expired": True, "purpose": payload.get("purpose", "runtime"),
                               "message": str(payload.get("message") or LABELS.get(event["kind"], event["kind"]))[:180]}
                    db.execute("UPDATE events SET payload=? WHERE seq=?", (dump(compact), event["seq"]))
            db.execute("UPDATE turns SET preview=NULL,draft=CASE WHEN status='committed' THEN '' ELSE draft END WHERE id=?",
                       (turn["id"],))
            files.extend(turn["files"])
        for row in cached:
            db.execute(f"DELETE FROM {row['table']} WHERE key=?", (row["key"],))
        # Editing conversations and undo versions are user data. Only detailed
        # diagnostics of saved, idle workspaces are subject to log retention.
        settings = store.settings()
        editor_events = db.execute("""SELECT e.seq,length(CAST(e.payload AS BLOB)) AS bytes,r.created,
            w.document=w.base_document AS saved FROM card_edit_events e
            JOIN card_edit_runs r ON r.id=e.run JOIN card_edits w ON w.id=r.workspace
            WHERE e.kind NOT IN ('completed','error','cancelled') ORDER BY e.seq""").fetchall()
        total = sum(e["bytes"] for e in editor_events)
        for event in editor_events:
            if event["saved"] and (event["created"] < time.time() - settings.log_retention_days * 86400
                                   or total > settings.log_max_mib * 1024**2):
                db.execute("DELETE FROM card_edit_events WHERE seq=?", (event["seq"],))
                total -= event["bytes"]
    for path in files:
        if path.resolve().is_relative_to((store.root / "runs").resolve()) and not path.is_symlink():
            path.unlink(missing_ok=True)
    # Empty run directories are disposable; no recursive deletion is required.
    runs = store.root / "runs"
    if runs.exists():
        for path in sorted(runs.rglob("*"), key=lambda p: len(p.parts), reverse=True):
            if path.is_dir() and not path.is_symlink() and path.resolve().is_relative_to(runs.resolve()):
                try:
                    path.rmdir()
                except OSError:
                    pass
    if vacuum:
        with store.connection() as db:
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            db.execute("VACUUM")
    return report
