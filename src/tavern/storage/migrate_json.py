"""Bounded rewrites of known JSON columns inside a schema migration."""

import json


def rewrite_json_records(db, fields, marker):
    for table, column, convert, condition in fields:
        last_row = 0
        while rows := db.execute(
            f"SELECT rowid,{column} FROM {table} WHERE rowid>? AND instr({column},?)>0 {condition} "
            "ORDER BY rowid LIMIT 32",
            (last_row, marker),
        ).fetchall():
            for rowid, raw in rows:
                record = json.loads(raw)
                if convert(record):
                    db.execute(
                        f"UPDATE {table} SET {column}=? WHERE rowid=?",
                        (json.dumps(record, ensure_ascii=False, separators=(",", ":")), rowid),
                    )
            last_row = rows[-1][0]
