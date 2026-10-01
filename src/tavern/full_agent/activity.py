"""Public dialogue activity, separated from provider/debug records and reasoning."""
import json


def pretty(value):
    if value is None:
        return ""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return value
    return json.dumps(value, ensure_ascii=False, indent=2)


def tool_item(key, order, name, arguments):
    try:
        args = json.loads(arguments) if isinstance(arguments, str) else arguments
    except (ValueError, TypeError):
        args = {}
    summary = next((str(args[k]) for k in ("query", "key", "ref", "path", "reason", "content")
                    if isinstance(args, dict) and args.get(k)), "")
    return {"id": key, "order": order, "kind": "tool", "name": name,
            "summary": " ".join(summary.split())[:140], "status": "running",
            "details": {"arguments": pretty(arguments), "result": ""}}


def finish_tool(item, result):
    item["details"]["result"] = pretty(result)
    try:
        result = json.loads(result) if isinstance(result, str) else result
    except (ValueError, TypeError):
        pass
    item["status"] = "error" if isinstance(result, dict) and "error" in result else "returned"


def text_item(key, order, content):
    return {"id": key, "order": order, "kind": "assistant", "summary": str(content)[:180],
            "status": "returned", "details": {"content": str(content)}}


def from_trace(rows):
    items, pending = [], {}
    for row in rows:
        seq, message = row["seq"], json.loads(row["payload"])
        if message.get("_full_agent_final") or message.get("_full_agent_rejected"):
            continue
        if message.get("role") == "assistant":
            if message.get("content"):
                items.append(text_item(f"text-{seq}", seq * 10, message["content"]))
            for index, call in enumerate(message.get("tool_calls", [])):
                function = call.get("function", {})
                item = tool_item(f"call-{seq}-{index}", seq * 10 + index + 1,
                                 function.get("name", "tool"), function.get("arguments", ""))
                items.append(item)
                pending[call.get("id")] = item
        elif message.get("role") == "tool" and message.get("tool_call_id") in pending:
            item = pending.pop(message["tool_call_id"])
            finish_tool(item, message.get("_archive_content", message.get("content", "")))
    for item in pending.values():
        item["status"] = "interrupted"
    return items


def message_items(store, session, message_ids):
    allowed = {m["id"] for m in session["messages"] if m["role"] == "assistant"}
    if not set(message_ids) <= allowed:
        raise KeyError("工具记录不属于当前可见的回复版本")
    groups = {mid: [] for mid in message_ids}
    if not groups:
        return groups
    with store.connection() as db:
        rows = db.execute("SELECT message_id,seq,payload FROM full_agent_trace WHERE session=? AND message_id IN ("
                          + ",".join("?" for _ in groups) + ") ORDER BY seq",
                          (session["id"], *groups)).fetchall()
    for row in rows:
        groups[row["message_id"]].append(row)
    return {mid: from_trace(records) for mid, records in groups.items()}


def turn_items(store, sid, tid):
    turn = store.turn(tid)
    if turn["session"] != sid:
        raise KeyError("回合不属于当前会话")
    items = {}
    for event in store.events(tid):
        payload, kind = event["payload"], event["kind"]
        if kind not in ("full_agent_assistant", "full_agent_tool_started", "full_agent_tool"):
            continue
        key = payload.get("activity_id", f"event-{event['seq']}")
        order = payload.get("order", event["seq"] * 10)
        if kind == "full_agent_assistant":
            items[key] = text_item(key, order, payload["content"])
        else:
            if key not in items:
                items[key] = tool_item(key, order, payload["name"], payload.get("arguments", ""))
            if kind == "full_agent_tool":
                finish_tool(items[key], payload.get("result"))
        items[key]["event_seq"] = event["seq"]
    if turn["status"] not in ("running", "queued"):
        for item in items.values():
            if item["status"] == "running":
                item["status"] = "interrupted"
    return sorted(items.values(), key=lambda item: item["order"])


def compact(items):
    return [{k: v for k, v in item.items() if k != "details"} for item in items]


def part_page(items, activity_id, part, offset, limit=12000):
    item = next((item for item in items if item["id"] == activity_id), None)
    if item is None or part not in item["details"]:
        raise KeyError("工具记录字段不存在")
    text = item["details"][part]
    end = min(len(text), offset + limit)
    return {"text": text[offset:end], "next": end if end < len(text) else None, "total": len(text)}
