"""Standalone worldbook library and shelf-card defaults for new conversations.

Library books are templates: mounting copies their entries into a conversation
(see ``tavern.character.worldbooks``), so later library edits never change an
existing story. Card defaults only preselect options when a conversation is
created; foreign keys drop them together with their card, persona or book.
"""

import json
import time

from tavern.character.worldbooks import validate_book
from tavern.core.errors import NotFound
from tavern.storage.store import Conflict, dump, uid


def initialize(store):
    with store.connection() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS worldbooks(id TEXT PRIMARY KEY, name TEXT NOT NULL,
                book TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS card_defaults(
                card TEXT PRIMARY KEY REFERENCES cards(id) ON DELETE CASCADE,
                persona TEXT REFERENCES personas(id) ON DELETE SET NULL,
                version INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS card_default_worldbooks(
                card TEXT NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
                worldbook TEXT NOT NULL REFERENCES worldbooks(id) ON DELETE CASCADE,
                position INTEGER NOT NULL, PRIMARY KEY(card, worldbook));
        """)


def _row(row):
    return {**dict(row), "book": json.loads(row["book"])}


def summaries(store):
    # Count in SQL so listings never load book bodies; json_each covers list and dict entries.
    with store.connection() as db:
        rows = db.execute(
            "SELECT id,name,revision,created,"
            "(SELECT count(*) FROM json_each(book,'$.entries')) AS entries "
            "FROM worldbooks ORDER BY rowid DESC"
        ).fetchall()
    return [dict(row) for row in rows]


def read(db, wid):
    row = db.execute("SELECT * FROM worldbooks WHERE id=?", (wid,)).fetchone()
    if row is None:
        raise NotFound("世界书不存在或已删除")
    return _row(row)


def worldbook(store, wid):
    with store.connection() as db:
        return read(db, wid)


def insert(db, name, book):
    """Write inside the caller's transaction; callers validate the book first."""
    wid = uid()
    db.execute(
        "INSERT INTO worldbooks(id,name,book,created) VALUES (?,?,?,?)", (wid, name, dump(book), time.time())
    )
    return read(db, wid)


def update(db, wid, name, book, expected_revision):
    """Write inside the caller's transaction; Card Author saves share it with its draft."""
    current = read(db, wid)
    if expected_revision != current["revision"]:
        raise Conflict("世界书已改变；草稿已保留，请重新载入")
    db.execute("UPDATE worldbooks SET name=?,book=?,revision=revision+1 WHERE id=?", (name, dump(book), wid))
    return read(db, wid)


def save(store, wid, edit):
    validate_book(edit.name, edit.book)
    with store.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        if wid is None:
            return insert(db, edit.name, edit.book)
        return update(db, wid, edit.name, edit.book, edit.expected_revision)


def delete(store, wid):
    with store.connection() as db:
        # Conversations own their mounted copies; only card defaults cascade.
        if db.execute("DELETE FROM worldbooks WHERE id=?", (wid,)).rowcount != 1:
            raise NotFound("世界书不存在或已删除")


def read_defaults(db, cid):
    if not db.execute("SELECT 1 FROM cards WHERE id=?", (cid,)).fetchone():
        raise NotFound("角色卡不存在或已删除")
    row = db.execute("SELECT persona,version FROM card_defaults WHERE card=?", (cid,)).fetchone()
    books = db.execute(
        "SELECT worldbook FROM card_default_worldbooks WHERE card=? ORDER BY position", (cid,)
    ).fetchall()
    return {
        "persona_id": row["persona"] if row else None,
        "worldbook_ids": [book[0] for book in books],
        "version": row["version"] if row else 0,
    }


def card_defaults(store, cid):
    with store.connection() as db:
        return read_defaults(db, cid)


def cards_defaults(store, cids):
    """Defaults for many cards in one read; cards without a row get empty defaults."""
    result = {cid: {"persona_id": None, "worldbook_ids": [], "version": 0} for cid in cids}
    with store.connection() as db:
        for row in db.execute("SELECT card,persona,version FROM card_defaults"):
            if row["card"] in result:
                result[row["card"]].update(persona_id=row["persona"], version=row["version"])
        for card, wid in db.execute(
            "SELECT card,worldbook FROM card_default_worldbooks ORDER BY card,position"
        ):
            if card in result:
                result[card]["worldbook_ids"].append(wid)
    return result


def write_defaults(db, cid, persona_id, worldbook_ids, version):
    """Replace a card's defaults; callers check that every target exists."""
    db.execute(
        "INSERT INTO card_defaults(card,persona,version) VALUES (?,?,?) "
        "ON CONFLICT(card) DO UPDATE SET persona=excluded.persona,version=excluded.version",
        (cid, persona_id, version),
    )
    db.execute("DELETE FROM card_default_worldbooks WHERE card=?", (cid,))
    db.executemany(
        "INSERT INTO card_default_worldbooks VALUES (?,?,?)",
        [(cid, wid, position) for position, wid in enumerate(worldbook_ids)],
    )


def save_defaults(store, cid, edit):
    with store.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        current = read_defaults(db, cid)
        if edit.expected_version != current["version"]:
            raise Conflict("新建对话默认项已改变，请重新打开")
        if (
            edit.persona_id
            and not db.execute("SELECT 1 FROM personas WHERE id=?", (edit.persona_id,)).fetchone()
        ):
            raise NotFound("用户角色不存在或已删除")
        for wid in edit.worldbook_ids:
            read(db, wid)
        write_defaults(db, cid, edit.persona_id, edit.worldbook_ids, current["version"] + 1)
        return read_defaults(db, cid)


def copy_defaults(db, source, target):
    current = read_defaults(db, source)
    if current["version"]:
        write_defaults(db, target, current["persona_id"], current["worldbook_ids"], 1)
