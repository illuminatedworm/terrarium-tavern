"""Keep failed input retryable after settings edits without reusing stale progress."""
import json


def retry_head(db, turn):
    request = json.loads(turn["request"])
    if request.get("mode") == "regenerate":
        return request.get("message_id")
    row = db.execute("SELECT payload FROM events WHERE turn=? AND kind='input_ready' ORDER BY seq DESC LIMIT 1",
                     (turn["id"],)).fetchone()
    return json.loads(row[0]).get("message", {}).get("parent") if row else None


def can_retry(db, turn, session):
    request = json.loads(turn["request"])
    latest = db.execute("SELECT id FROM turns WHERE session=? ORDER BY created DESC,rowid DESC LIMIT 1",
                        (turn["session"],)).fetchone()
    anchor = retry_head(db, turn)
    return (turn["status"] in ("error", "cancelled", "interrupted") and not request.get("scene_refresh")
            and latest["id"] == turn["id"] and (request["expected_revision"] == session["revision"]
                or (anchor is not None and anchor == session["head"])))
