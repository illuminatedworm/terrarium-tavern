"""Decode complete SSE events, including multiline data and a final event at EOF."""

import json

from tavern.scripting.http_errors import redact_detail


async def stream_events(response):
    data, name = [], ""
    boundary_name = None
    empty = object()

    def decode(event_name):
        value = "\n".join(data)
        if not value.strip() or value.strip() == "[DONE]":
            return empty
        event = json.loads(value)
        if isinstance(event, dict) and event_name and (event_name == "error" or "type" not in event):
            event["type"] = event_name
        return event

    async for line in response.aiter_lines():
        if not line:
            if data:
                event = decode(name)
                if event is not empty:
                    yield event
            data, name = [], ""
            boundary_name = None
        elif line.startswith("data:"):
            if boundary_name is not None:
                # Relays may omit the blank line before a named error event.
                # Split only complete JSON; metadata within multiline JSON and
                # a trailing event field still belong to the current SSE event.
                try:
                    event = decode(boundary_name)
                except json.JSONDecodeError:
                    pass
                else:
                    if event is not empty:
                        yield event
                    data = []
                boundary_name = None
            data.append(line[5:].removeprefix(" "))
        elif line.startswith("event:"):
            if data and boundary_name is None:
                boundary_name = name
            name = line[6:].strip()
    if data:
        event = decode(name)
        if event is not empty:
            yield event


def stream_error(label, event, *, secret=""):
    """Describe a mid-stream provider error without dropping its explanation.

    Official APIs and relays nest the detail differently (`error` object,
    plain string or top-level `message`). Keep it bounded; it is shown in the
    UI and stored with the failed run.
    """
    error = event.get("error")
    if not isinstance(error, dict):
        error = {"message": error} if isinstance(error, str) else event
    codes = [
        str(error[key]) for key in ("code", "status", "type") if error.get(key) not in (None, "", "error")
    ]
    message = str(error.get("message") or "").strip()
    detail = (f"（{' '.join(codes)}）" if codes else "") + (f"：{message}" if message else "")
    # HTTP 200 SSE errors can echo credentials just like HTTP failures. Redact
    # before bounding the explanation, so a cut cannot expose a key prefix.
    detail = redact_detail(detail, secret)[:500]
    return f"{label}{detail}；本回合未提交"
