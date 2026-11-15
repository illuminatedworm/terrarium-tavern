"""Transactional removal of complete stories and their owned database records."""

import sqlite3

from tavern.storage.retrieval_cleanup import prune_orphan_retrieval


def delete_session_rows(db: sqlite3.Connection, predicate: str, parameters: tuple[object, ...] = ()) -> None:
    """Use a caller-owned transaction and a fixed internal session predicate."""
    selected = f"SELECT id FROM sessions WHERE {predicate}"
    turns = f"SELECT id FROM turns WHERE session IN ({selected})"
    for table in ("changes", "events"):
        db.execute(f"DELETE FROM {table} WHERE turn IN ({turns})", parameters)
    for table in ("turns", "story_trace", "context_operations", "messages"):
        db.execute(f"DELETE FROM {table} WHERE session IN ({selected})", parameters)
    db.execute(f"DELETE FROM sent_cache_breakpoints WHERE owner IN ({selected})", parameters)
    db.execute(
        f"DELETE FROM context_token_samples WHERE owner IN ("
        f"SELECT 'novel:' || id FROM novel_jobs WHERE session IN ({selected}) "
        f"UNION ALL SELECT 'story:' || id FROM sessions WHERE {predicate} "
        f"UNION ALL SELECT 'advisor:' || id FROM sessions WHERE {predicate})",
        parameters * 3,
    )
    # Advisor and novel rows and adapter preferences follow their owning session.
    db.execute(f"DELETE FROM sessions WHERE {predicate}", parameters)
    prune_orphan_retrieval(db)
