"""Shelf and session list projections, including the actual source of each portrait."""

import json
import mimetypes

from tavern.core.errors import NotFound

CARD_IMAGE = """container,original,resources,
    CASE WHEN coalesce(json_extract(document,'$.spec'),'')!='' AND json_type(document,'$.data')='object'
    THEN json_quote(json_extract(document,'$.data.assets')) ELSE json_quote(json_extract(document,'$.assets')) END AS assets"""
SESSION_IMAGE = """json_extract(s.card_data,'$.container') AS container,
    json_extract(s.card_data,'$.original') AS original,json_extract(s.card_data,'$.resources') AS resources,
    CASE WHEN coalesce(json_extract(s.card_data,'$.document.spec'),'')!='' AND json_type(s.card_data,'$.document.data')='object'
    THEN json_quote(json_extract(s.card_data,'$.document.data.assets')) ELSE json_quote(json_extract(s.card_data,'$.document.assets')) END AS assets"""


def _image(row):
    if row["container"] == "png":
        return row["original"], "image/png"
    assets = json.loads(row["assets"] or "null")
    if not isinstance(assets, list):
        return None, None
    resources = json.loads(row["resources"] or "{}")
    for asset in assets:
        if (
            not isinstance(asset, dict)
            or asset.get("type") != "icon"
            or not isinstance(asset.get("uri"), str)
        ):
            continue
        path = asset["uri"].removeprefix("embeded://")
        mime = mimetypes.guess_type(path)[0]
        if path in resources and mime in ("image/png", "image/jpeg", "image/webp", "image/gif", "image/avif"):
            return resources[path], mime
    return None, None


def _summaries(rows):
    return [
        {key: row[key] for key in row.keys() if key not in ("container", "original", "resources", "assets")}
        | {"portrait": _image(row)[0]}
        for row in rows
    ]


def card_summaries(store):
    with store.connection() as db:
        return _summaries(
            db.execute(
                "SELECT id,name,revision,json_extract(report,'$.variables') AS variables,"
                "substr(CASE WHEN json_type(document,'$.data')='object' THEN json_extract(document,'$.data.description') "
                "ELSE json_extract(document,'$.description') END,1,400) "
                f"AS description,{CARD_IMAGE} FROM cards ORDER BY rowid DESC"
            )
        )


def session_summaries(store):
    with store.connection() as db:
        return _summaries(
            db.execute(
                "SELECT s.id,s.title,s.revision,s.persona,s.created,s.channel_origin,s.kemini,"
                "coalesce(c.name,json_extract(s.card_data,'$.name')) AS name,"
                f"{SESSION_IMAGE} FROM sessions s LEFT JOIN cards c ON c.id=s.card ORDER BY s.created DESC,s.rowid DESC"
            )
        )


def portrait(store, owner: str, *, session=False):
    query = (
        f"SELECT {SESSION_IMAGE} FROM sessions s WHERE s.id=?"
        if session
        else f"SELECT {CARD_IMAGE} FROM cards WHERE id=?"
    )
    with store.connection() as db:
        row = db.execute(query, (owner,)).fetchone()
    if row is None:
        raise NotFound("会话不存在" if session else "角色不存在")
    return _image(row)
