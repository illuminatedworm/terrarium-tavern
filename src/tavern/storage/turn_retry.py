"""Keep failed input retryable after settings edits without reusing stale progress."""

import json


def retry_head(db, turn):
    request = json.loads(turn["request"])
    if request.get("mode") == "regenerate":
        return request.get("message_id")
    row = db.execute(
        "SELECT payload FROM events WHERE turn=? AND kind='input_ready' ORDER BY seq DESC LIMIT 1",
        (turn["id"],),
    ).fetchone()
    return json.loads(row[0]).get("message", {}).get("parent") if row else None
