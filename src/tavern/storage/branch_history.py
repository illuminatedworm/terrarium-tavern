"""Read branch payloads separately from the small graph used to order reply versions."""

import json
import sqlite3
from typing import Literal

from tavern.core.history_refs import message_ref

BRANCH_MESSAGES = (
    "WITH RECURSIVE branch(id,parent,role) AS ("
    "SELECT id,parent,role FROM messages WHERE session=? AND id=? UNION "
    "SELECT m.id,m.parent,m.role FROM messages m JOIN branch b ON m.id=b.parent WHERE m.session=?"
    ") "
)


HistoryView = Literal["full", "reader", "preview"]

# A script-bearing display needs its frozen document until the API has resolved its
# assets. Other reader messages need only the historical names for {{char}}.
READER_COLUMNS = """
    m.id,m.session,m.parent,m.role,m.raw,m.model,m.display,m.created,m.retrieval_query,
    m.auxiliary,m.story_segments,m.ooc,
    (SELECT json_group_object(key,value) FROM json_each(m.snapshot)
        WHERE key IN ('state','scopes','lore_injections')) AS snapshot,
    CASE WHEN json_type(m.snapshot,'$.content.document')='object' THEN
        CASE WHEN ? AND instr(m.display,'<') THEN json_extract(m.snapshot,'$.content.document')
        ELSE (SELECT json_group_object(key,value) FROM json_each(m.snapshot,
            CASE WHEN json_type(m.snapshot,'$.content.document.data')='object'
            THEN '$.content.document.data' ELSE '$.content.document' END)
            WHERE key IN ('name','nickname')) END
    END AS document"""

# Legacy lore replay checks removal markers at their original message. The rest of
# context compilation uses the current head snapshot, even for historical macros.
PREVIEW_COLUMNS = """
    m.id,m.parent,m.role,m.raw,m.model,m.ooc,m.story_segments,
    (SELECT json_group_object(key,value) FROM json_each(m.snapshot) WHERE key='lore_injections') AS snapshot"""


def branch_history(
    db: sqlite3.Connection, sid: str, head: str | None, view: HistoryView = "full", scripts: bool = False
) -> list[dict]:
    if not head:
        return []
    if view == "preview":
        rows = db.execute(BRANCH_MESSAGES + "SELECT * FROM branch", (sid, head, sid))
    else:
        rows = db.execute(
            "SELECT id,parent,role FROM messages WHERE session=? ORDER BY created,rowid", (sid,)
        )
    graph = {row["id"]: row for row in rows}
    branch = {}
    while head:
        if head not in graph or head in branch:
            raise ValueError("剧情分支无效")
        branch[head] = None
        head = graph[head]["parent"]
    ids = json.dumps(list(branch))
    columns = {"reader": READER_COLUMNS, "preview": PREVIEW_COLUMNS, "full": "m.*"}[view]
    params = (scripts, ids, sid) if view == "reader" else (ids, sid)
    for row in db.execute(
        f"SELECT {columns} FROM json_each(?) ids CROSS JOIN messages m WHERE m.id=ids.value AND m.session=?",
        params,
    ):
        message = dict(row)
        message["snapshot"] = json.loads(message["snapshot"])
        if view == "reader" and (document := message.pop("document")) is not None:
            message["snapshot"]["content"] = {"document": json.loads(document)}
        message["story_segments"] = json.loads(message["story_segments"])
        if view != "preview":
            message["auxiliary"] = json.loads(message["auxiliary"])
        branch[message["id"]] = message
    result = list(reversed(branch.values()))
    if view == "preview":
        return result
    for record in db.execute(
        "SELECT a.message_id,a.anchor_id,a.id,a.title FROM json_each(?) ids "
        "JOIN story_lore_admissions a ON a.message_id=ids.value ORDER BY a.rowid",
        (ids,),
    ):
        branch[record["message_id"]].setdefault("lore_admissions", []).append(
            {key: record[key] for key in ("anchor_id", "id", "title")}
        )

    def version_group(message):
        parent = graph.get(message["parent"])
        if parent is not None and parent["role"] == "user":
            return "exchange", parent["parent"]
        return "opening", message["parent"]

    versions = {}
    for row in graph.values():
        if row["role"] == "assistant":
            versions.setdefault(version_group(row), []).append(row["id"])
    for position, message in enumerate(result, 1):
        message["ref"] = message_ref(position)
        if message["role"] == "assistant":
            siblings = versions[version_group(message)]
            message.update(versions=siblings, version_index=siblings.index(message["id"]))
    return result
