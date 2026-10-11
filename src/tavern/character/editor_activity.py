"""Public editor conversation activity survives diagnostic-log retention."""
import json

PUBLIC_KINDS = ("started", "model_call", "text", "tool_started", "tool_finished", "completed", "error", "cancelled")


def compact_text_events(db):
    """Coalesce idle streamed chunks without changing their visible text/order.

    Cleanup holds the write transaction and rejects active editor runs first.
    Keep the first sequence ID so a retained message stays at the same position.
    """
    rows = db.execute("SELECT run,seq,kind,payload FROM card_edit_events WHERE kind IN ('model_call','text') ORDER BY run,seq").fetchall()
    chunks, run = [], None

    def flush():
        if len(chunks) > 1:
            payload = json.dumps({"text": "".join(text for _, text in chunks)}, ensure_ascii=False)
            db.execute("UPDATE card_edit_events SET payload=? WHERE seq=?", (payload, chunks[0][0]))
            db.executemany("DELETE FROM card_edit_events WHERE seq=?", [(seq,) for seq, _ in chunks[1:]])
        chunks.clear()

    for row in rows:
        if row["run"] != run or row["kind"] == "model_call":
            flush()
        run = row["run"]
        if row["kind"] == "text":
            text = json.loads(row["payload"]).get("text")
            if isinstance(text, str):
                chunks.append((row["seq"], text))
    flush()
