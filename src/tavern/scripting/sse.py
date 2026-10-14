"""Decode complete SSE events, including multiline data and a final event at EOF."""
import json


async def stream_events(response):
    data, name = [], ""
    empty = object()

    def decode():
        value = "\n".join(data)
        if not value.strip() or value.strip() == "[DONE]":
            return empty
        event = json.loads(value)
        if isinstance(event, dict) and name and (name == "error" or "type" not in event):
            event["type"] = name
        return event

    async for line in response.aiter_lines():
        if not line:
            if data:
                event = decode()
                if event is not empty:
                    yield event
            data, name = [], ""
        elif line.startswith("data:"):
            data.append(line[5:].removeprefix(" "))
        elif line.startswith("event:"):
            name = line[6:].strip()
    if data:
        event = decode()
        if event is not empty:
            yield event
