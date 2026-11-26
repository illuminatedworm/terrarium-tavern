"""Public dialogue activity, separated from provider/debug records and reasoning."""

import json
from collections.abc import Sequence

from tavern.core.errors import NotFound
from tavern.story.protocol import failed


def pretty(value):
    if value is None:
        return ""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return value
    return json.dumps(value, ensure_ascii=False, indent=2)


def tool_item(key, order, name, arguments, *, with_details=True):
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
    if name == "calculate":
        commentary = args.get("commentary") if isinstance(args, dict) else None
        summary = commentary if isinstance(commentary, str) else ""
    elif name == "write_plot":
        summary = "包含未来剧情，展开查看"
    elif name == "submit_story":
        summary = "正文交付；展开查看提交内容"
    elif name in ("roleplay", "kemini_emit_story"):
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
    item = {
        "id": key,
        "order": order,
        "kind": "tool",
        "name": name,
        "summary": " ".join(summary.split())[:140],
        "status": "running",
    }
    if with_details:
        item["details"] = {"arguments": pretty(arguments), "result": ""}
    return item


def finish_tool(item, result, *, arguments=None):
    if "details" in item:
        if arguments is not None:
            item["details"]["arguments"] = pretty(arguments)
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
        "summary": f"{body['investigator']} · 资料调查报告已送达",
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
                emit("story_runtime", {**item, "status": "returned"})
            reports.clear()  # A retry or duplicate transport notification is not a new delivery.

    request.extensions["trace"] = trace


def from_trace(rows, *, with_details=True):
    items, pending, reports = [], {}, []
    for row in rows:
        seq, message = row["seq"], json.loads(row["payload"])
        if message.get("_story_final") or message.get("_story_rejected"):
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
                arguments = message.get("_unicode_story_arguments", {}).get(
                    call.get("id"), function.get("arguments", "")
                )
                item = tool_item(
                    f"call-{seq}-{index}",
                    seq * 10 + index + 1,
                    function.get("name", "tool"),
                    arguments,
                    with_details=with_details,
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


def message_items(store, sid: str, message_ids: Sequence[str], *, with_details=True) -> dict[str, list[dict]]:
    groups = {mid: [] for mid in message_ids}
    with store.connection() as db:
        # Keep branch membership and the matching trace in the same read snapshot.
        db.execute("BEGIN")
        session = db.execute("SELECT head FROM sessions WHERE id=?", (sid,)).fetchone()
        if session is None:
            raise NotFound("会话不存在")
        if set(groups) != store.visible_reply_ids(db, sid, session["head"], groups):
            raise NotFound("工具记录不属于当前可见的回复版本")
        rows = db.execute(
            "SELECT message_id,seq,payload FROM story_trace WHERE session=? AND message_id IN ("
            + ",".join("?" for _ in groups)
            + ") ORDER BY seq",
            (sid, *groups),
        ).fetchall()
    for row in rows:
        groups[row["message_id"]].append(row)
    return {mid: from_trace(records, with_details=with_details) for mid, records in groups.items()}


def event_activity(event: dict, previous=None, *, with_details=True):
    payload, kind, seq = event["payload"], event["kind"], event["seq"]
    if kind == "story_runtime":
        return {
            **{k: payload[k] for k in ("id", "order", "kind", "name", "summary", "status")},
            "details": {},
            "event_seq": seq,
        }
    if kind not in ("story_assistant", "story_tool_started", "story_tool"):
        return None
    key = payload.get("activity_id", f"event-{seq}")
    order = payload.get("order", seq * 10)
    if kind == "story_assistant":
        item = text_item(key, order, payload["content"], is_story=payload.get("is_story", False))
    else:
        item = (previous or {}).get(key) or tool_item(
            key, order, payload["name"], payload.get("arguments", ""), with_details=with_details
        )
        if kind == "story_tool":
            finish_tool(item, payload.get("result"), arguments=payload.get("decoded_arguments"))
    item["event_seq"] = seq
    return item


def turn_items(store, sid: str, tid: str, *, summary: bool = False) -> list[dict]:
    with store.connection() as db:
        db.execute("BEGIN")
        turn = db.execute(
            "SELECT s.kemini,t.session,t.status FROM sessions s LEFT JOIN turns t ON t.id=? WHERE s.id=?",
            (tid, sid),
        ).fetchone()
        if turn is None:
            raise NotFound("会话不存在")
        if turn["session"] is None:
            raise NotFound("回合不存在")
        if turn["session"] != sid:
            raise NotFound("回合不属于当前会话")
        rows = db.execute(
            "SELECT seq,kind,payload FROM events WHERE turn=? "
            "AND kind IN ('story_assistant','story_tool_started','story_tool','story_runtime') ORDER BY seq",
            (tid,),
        ).fetchall()
    items = {}
    for row in rows:
        event = {**dict(row), "payload": json.loads(row["payload"])}
        item = event_activity(
            event,
            items,
            with_details=not summary or (not turn["kemini"] and event["payload"].get("name") == "present_ui"),
        )
        items[item["id"]] = item
    if turn["status"] not in ("running", "queued"):
        for item in items.values():
            if item["status"] == "running":
                item["status"] = "interrupted"
    ordered = sorted(items.values(), key=lambda item: item["order"])
    return compact(ordered, visuals=not turn["kemini"]) if summary else ordered


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
