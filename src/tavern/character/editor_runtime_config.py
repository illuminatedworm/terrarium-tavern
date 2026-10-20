"""The same operation-local choices drive validation, preview and execution."""

import json

from tavern.core.errors import NotFound
from tavern.core.message_adapter import bind_adapter
from tavern.core.model_selection import select_runtime


def editor_runtime_config(store, wid, options):
    with store.connection() as db:
        row = db.execute(
            "SELECT channel_origin,model_selection FROM card_author_conversations WHERE id=?", (wid,)
        ).fetchone()
    if row is None:
        raise NotFound("制卡对话不存在")
    cfg = select_runtime(
        store.settings(), "card_author", json.loads(row["model_selection"]), row["channel_origin"]
    )
    for key in ("model", "reasoning_effort"):
        if options.get(key) is not None:
            setattr(cfg, key, options[key])
    return bind_adapter(cfg, store, "editor", wid)
