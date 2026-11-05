"""Public dialogue activity, separated from provider/debug records and reasoning."""

import json

from tavern.core.errors import NotFound
from tavern.full_agent.protocol import failed


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
    # Stored traces from before the argument rename still use reason.
    summary = next(
        (
            str(args[k])
            for k in (
                "question",
                "message",
                "query",
                "key",
                "ref",
                "path",
                "comment",
                "reason",
                "handoff",
                "content",
            )
            if isinstance(args, dict) and args.get(k)
        ),
        "",
    )
    if name == "write_plot":
        summary = "包含未来剧情，展开查看"
    elif name in ("roleplay", "submit_story", "kemini_emit_story"):
        summary = "正文交付；展开查看原始参数"
    elif name == "finish_turn":
        summary = "申请正文回调与统一校验"
    write = args.get("handoff") if name == "new_context" and isinstance(args, dict) else args
    if name in ("write_handoff", "new_context") and isinstance(write, dict):
        # Activity also reads rejected raw model arguments, before schema validation.
        chapters = write.get("chapters")
        titles = (
            [op["title"] for op in chapters if isinstance(op, dict) and isinstance(op.get("title"), str)]
            if isinstance(chapters, list)
            else []
        )
        summary = "章节：" + "、".join(titles) if titles else "更新当前交接"
    elif name == "new_context" and write is None:
        summary = "沿用现有交接并换窗"
    return {
        "id": key,
        "order": order,
        "kind": "tool",
        "name": name,
        "summary": " ".join(summary.split())[:140],
        "status": "running",
        "details": {"arguments": pretty(arguments), "result": ""},
    }


def finish_tool(item, result):
    item["details"]["result"] = pretty(result)
    try:
        result = json.loads(result) if isinstance(result, str) else result
    except (ValueError, TypeError):
        pass
    item["status"] = "error" if failed(result) else "returned"


def text_item(key, order, content, *, is_story=False):
    return {
        "id": key,
        "order": order,
        "kind": "assistant",
        "is_story": is_story,
        "summary": str(content)[:180],
        "status": "returned",
        "details": {"content": str(content)},
    }


def novel_report_item(seq, message):
    if "_novel_mail" not in message:
        return None
    body = json.loads(message["content"].partition("\n")[2])
    if body["kind"] != "report":
        return None
    return {
        "id": f"runtime-{seq}",
        "order": seq * 10,
        "kind": "runtime",
        "name": "novel_report_delivery",
        "summary": f"{body['investigator']} · 原著调查报告已送达",
        "status": "running",
        "details": {},
    }


def observe_report_dispatch(request, reports, emit):
    """Preparing/capturing a request does not prove its body left the client."""
    previous = request.extensions.get("trace")

    async def trace(name, info):
        if previous:
            await previous(name, info)
        if name in ("http11.send_request_body.complete", "http2.send_request_body.complete"):
            for item in reports:
                emit("full_agent_runtime", {**item, "status": "returned"})
            reports.clear()  # A retry or duplicate transport notification is not a new delivery.

    request.extensions["trace"] = trace


def from_trace(rows):
    items, pending, reports = [], {}, []
    for row in rows:
        seq, message = row["seq"], json.loads(row["payload"])
        if message.get("_full_agent_final") or message.get("_full_agent_rejected"):
            continue
        if message.get("role") == "assistant":
            # A durable model response also proves dispatch, including old traces
            # and forks whose transient transport events have been cleaned up.
            for item in reports:
                item["status"] = "returned"
            reports.clear()
            if message.get("content") and (
                not message.get("_story_call_id")
                or any(
                    c["id"] == message["_story_call_id"] and c["function"]["name"] == "submit_story"
                    for c in message.get("tool_calls", [])
                )
            ):
                items.append(
                    text_item(
                        f"text-{seq}",
                        seq * 10,
                        message["content"],
                        is_story=bool(message.get("_story_received")) and not message.get("_story_call_id"),
                    )
                )
            for index, call in enumerate(message.get("tool_calls", [])):
                function = call.get("function", {})
                item = tool_item(
                    f"call-{seq}-{index}",
                    seq * 10 + index + 1,
                    function.get("name", "tool"),
                    function.get("arguments", ""),
                )
                items.append(item)
                pending[call.get("id")] = item
        elif message.get("role") == "tool" and message.get("tool_call_id") in pending:
            item = pending.pop(message["tool_call_id"])
            finish_tool(item, message.get("_archive_content", message.get("content", "")))
        elif item := novel_report_item(seq, message):
            items.append(item)
            reports.append(item)
    for item in [*pending.values(), *reports]:
        item["status"] = "interrupted"
    return items


def message_items(store, session, message_ids):
    allowed = {m["id"] for m in session["messages"] if m["role"] == "assistant"}
    if not set(message_ids) <= allowed:
        raise NotFound("工具记录不属于当前可见的回复版本")
    groups = {mid: [] for mid in message_ids}
    if not groups:
        return groups
    with store.connection() as db:
        rows = db.execute(
            "SELECT message_id,seq,payload FROM full_agent_trace WHERE session=? AND message_id IN ("
            + ",".join("?" for _ in groups)
            + ") ORDER BY seq",
            (session["id"], *groups),
        ).fetchall()
    for row in rows:
        groups[row["message_id"]].append(row)
    return {mid: from_trace(records) for mid, records in groups.items()}


def turn_items(store, sid, tid):
    turn = store.turn(tid)
    if turn["session"] != sid:
        raise NotFound("回合不属于当前会话")
    items = {}
    for event in store.events(tid):
        payload, kind = event["payload"], event["kind"]
        if kind == "full_agent_runtime":
            key = payload["id"]
            items[key] = {
                **{k: payload[k] for k in ("id", "order", "kind", "name", "summary", "status")},
                "details": {},
                "event_seq": event["seq"],
            }
            continue
        if kind not in ("full_agent_assistant", "full_agent_tool_started", "full_agent_tool"):
            continue
        key = payload.get("activity_id", f"event-{event['seq']}")
        order = payload.get("order", event["seq"] * 10)
        if kind == "full_agent_assistant":
            items[key] = text_item(key, order, payload["content"], is_story=payload.get("is_story", False))
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


def compact(items, *, visuals=False):
    result = []
    for item in items:
        public = {k: v for k, v in item.items() if k != "details"}
        if visuals and item.get("name") == "present_ui" and item["status"] == "returned":
            # Only a validated successful call can supply a preview. Historical
            # activity stays compact and continues to load graphics lazily.
            receipt = json.loads(item["details"]["result"])
            if isinstance(receipt, dict) and receipt.get("ok") is True:
                public["visual"] = json.loads(item["details"]["arguments"])["spec"]
        result.append(public)
    return result


def part_page(items, activity_id, part, offset):
    item = next((item for item in items if item["id"] == activity_id), None)
    if item is None or part not in item["details"]:
        raise NotFound("工具记录字段不存在")
    text = item["details"][part]
    end = min(len(text), offset + 12000)
    return {"text": text[offset:end], "next": end if end < len(text) else None, "total": len(text)}
