"""Decode complete SSE events, including multiline data and a final event at EOF."""
import json


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
