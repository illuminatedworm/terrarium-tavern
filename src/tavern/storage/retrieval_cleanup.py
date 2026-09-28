"""Remove deleted-session index references without evicting shared live indexes."""
import json


def prune_orphan_retrieval(db):
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    result = {"collections": 0, "indexes": 0, "vectors": 0}
    if "retrieval_collections" not in tables:
        return result
    dead = db.execute("SELECT session,keys FROM retrieval_collections WHERE session NOT IN (SELECT id FROM sessions)").fetchall()
    if not dead:
        return result
    orphan_keys, live_keys = set(), set()
    for _, keys in dead:
        orphan_keys.update(json.loads(keys))
    for row in db.execute("SELECT keys FROM retrieval_collections WHERE session IN (SELECT id FROM sessions)"):
        live_keys.update(json.loads(row[0]))
    for table, label in (("retrieval_indexes", "indexes"), ("retrieval_vectors", "vectors")):
        if table in tables:
            for key in orphan_keys - live_keys:
                result[label] += db.execute(f"DELETE FROM {table} WHERE key=?", (key,)).rowcount
    for sid, _ in dead:
        result["collections"] += db.execute("DELETE FROM retrieval_collections WHERE session=?", (sid,)).rowcount
    return result
