"""Audit persisted real application calls without invoking a model or exposing prose.

python tools/audit_runtime_cache.py --data data --session SESSION_ID --output .cache/cache-audit.json
Read-only SQLite access; credentials, request prose, tool arguments and response prose
are never exported. Missing cache counters remain unknown, not zero.
"""
import argparse
import copy
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from tavern.core.cache_policy import fingerprint, normalize_usage


def wire_prefix(body, api, field):
    """Hash the serialized boundary, including tools; no claim about relay internals."""
    prefix = {"model": body.get("model"), "tools": body.get("tools", [])}
    for key in ("tool_choice", "thinking", "reasoning", "reasoning_effort"):
        if key in body:
            prefix[key] = copy.deepcopy(body[key])
    if api == "anthropic":
        prefix["system"] = []
        for i, part in enumerate(body.get("system", [])):
            prefix["system"].append(copy.deepcopy(part))
            if isinstance(part, dict) and field in part:
                return fingerprint(prefix), f"system[{i}]"
    key = "input" if api == "responses" else "messages"
    prefix[key] = []
    for i, message in enumerate(body.get(key, [])):
        item = copy.deepcopy(message)
        content = item.get("content")
        prefix[key].append(item)
        if not isinstance(content, list):
            continue
        for j, part in enumerate(content):
            if isinstance(part, dict) and field in part:
                item["content"] = content[:j + 1]
                return fingerprint(prefix), f"{key}[{i}].content[{j}]"
    return None, None


def audit(data, session_id):
    path = Path(data) / "tavern.sqlite3"
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        session = db.execute("SELECT id,agent_mode FROM sessions WHERE id=?", (session_id,)).fetchone()
        if session is None:
            raise ValueError("Session does not exist")
        turns = db.execute("SELECT id,status,created FROM turns WHERE session=? ORDER BY created,id",
                           (session_id,)).fetchall()
        result = {"session": dict(session), "turns": []}
        for turn in turns:
            row = {"id": turn["id"], "status": turn["status"], "requests": [], "usage": [], "tools": []}
            events = db.execute("SELECT seq,kind,payload,created FROM events WHERE turn=? ORDER BY seq",
                                (turn["id"],)).fetchall()
            last_api = {}
            for event in events:
                payload = json.loads(event["payload"])
                purpose = payload.get("purpose", "runtime")
                if event["kind"] == "provider_request":
                    api = payload.get("api_mode", "chat_completions")
                    last_api[purpose] = api
                    body, cache = payload.get("wire", {}), payload.get("cache", {})
                    parameters = cache.get("parameters", {})
                    field = next((k for k in parameters if k in ("cache_control", "prompt_cache_breakpoint")), "cache_control")
                    digest, location = wire_prefix(body, api, field)
                    row["requests"].append({
                        "seq": event["seq"], "time": datetime.fromtimestamp(event["created"], ZoneInfo("Asia/Shanghai")).isoformat(),
                        "purpose": purpose, "api": api, "url": payload.get("url"), "model": payload.get("model"),
                        "estimated_input_tokens": payload.get("estimated_input_tokens"),
                        "cache_status": cache.get("status"), "cache_reason": cache.get("reason"),
                        "planned_index": cache.get("planned_index"), "anchor": cache.get("message_id"),
                        "wire_prefix_sha256": digest, "wire_breakpoint": location,
                        "wire_tools_sha256": fingerprint(body.get("tools", [])),
                        "wire_system_sha256": fingerprint(body.get("system", [
                            m for m in body.get("messages", []) if m.get("role") in ("system", "developer")])),
                    })
                elif event["kind"] == "usage":
                    usage = payload.get("usage", {})
                    raw = usage.get("raw_usage")
                    # Half Agent aggregates several provider calls in one event.
                    parts = raw if isinstance(raw, list) else [raw] if isinstance(raw, dict) else []
                    normalized = [normalize_usage(part, last_api.get(purpose, "chat_completions")) for part in parts]
                    current = {k: (sum(v[k] for v in normalized) if all(v[k] is not None for v in normalized) else None)
                               for k in ("prompt_tokens", "completion_tokens", "cached_tokens", "cache_write_tokens")} if normalized else {
                                   k: usage.get(k) for k in ("prompt_tokens", "completion_tokens", "cached_tokens", "cache_write_tokens")}
                    row["usage"].append({"seq": event["seq"], "purpose": purpose, **current,
                        "renormalized": any(current[k] != usage.get(k) for k in current)})
                elif event["kind"] == "full_agent_tool":
                    row["tools"].append({"name": payload.get("name"), "success": "error" not in payload.get("result", {})})
            result["turns"].append(row)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path("data"))
    parser.add_argument("--session", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = audit(args.data, args.session)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Audited {len(result['turns'])} turns: {args.output}")


if __name__ == "__main__":
    main()
