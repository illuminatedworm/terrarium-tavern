"""Browser views of committed consultations. The runtime keeps its complete tool trace."""

import json

from tavern.core.errors import NotFound


def public_trace(trace):
    fields = {"role", "content", "tool_calls", "tool_call_id", "web_sources", "web_activity"}
    return [{k: v for k, v in message.items() if k in fields} for message in trace]


def exchange_activity(trace):
    offers, offer_ids, web, count = [], set(), [], 0
    for message in trace:
        calls = message.get("tool_calls") or []
        count += len(calls)
        choices = [c for c in calls if c["function"]["name"] in ("offer_inputs", "offer_titles")]
        if choices:
            offers.append({"role": "assistant", "tool_calls": choices})
            offer_ids.update(c["id"] for c in choices)
        if message.get("role") == "tool" and message.get("tool_call_id") in offer_ids:
            offers.append({key: message[key] for key in ("role", "tool_call_id", "content")})
        if message.get("web_activity") or message.get("web_sources"):
            web.append({key: message[key] for key in ("web_activity", "web_sources") if key in message})
    return {"tool_count": count, "offer_trace": offers, "web": web}


def exchange_trace(db, sid, seq):
    row = db.execute("SELECT trace FROM advisor_exchanges WHERE session=? AND seq=?", (sid, seq)).fetchone()
    if row is None:
        raise NotFound("咨询记录不存在或已清空")
    return public_trace(json.loads(row[0]))


def preview_view(preview):
    # The dialog renders the wire messages. For the same positions it only
    # needs classification and local-count metadata from the two traces.
    preview["rows"] = [
        {
            **{key: meta.get(key) for key in ("message_type", "runtime_phase")},
            **{key: row[key] for key in ("local_tokens", "local_excluded")},
        }
        for row, meta in zip(preview.pop("trace"), preview.pop("runtime_trace"), strict=True)
    ]
    del preview["runtime_messages"]
    preview["has_summary"] = bool(preview.pop("summary"))
    return preview
