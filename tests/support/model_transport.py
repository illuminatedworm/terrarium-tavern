"""Inspect the production adapter/cache preparation path without dispatching HTTP."""

import json

import httpx

from tavern.core.message_adapter import adapt_messages


async def request_payload(provider, messages, *, tools=None, **kwargs):
    messages, _, _ = adapt_messages(messages, provider.settings)
    body, _, _ = await provider._prepare(
        messages, tools or (kwargs.get("extra_body") or {}).get("tools"), False, kwargs
    )
    return body


def blocks(api, text):
    if api == "anthropic":
        return [
            {"type": "thinking", "thinking": "思考", "signature": "signed-opaque"},
            {"type": "text", "text": text},
        ]
    return [
        {"type": "reasoning", "id": "rs_opaque", "summary": [], "encrypted_content": "encrypted"},
        {
            "type": "message",
            "id": "msg_opaque",
            "role": "assistant",
            "content": [{"type": "output_text", "text": text, "annotations": []}],
        },
    ]


def sse(api, payload):
    events = []
    if api == "anthropic":
        events.append({"type": "message_start", "message": {**payload, "content": [], "stop_reason": None}})
        for index, part in enumerate(payload["content"]):
            if part["type"] not in ("text", "thinking"):
                events.append({"type": "content_block_start", "index": index, "content_block": part})
                continue
            events.append(
                {
                    "type": "content_block_start",
                    "index": index,
                    "content_block": {
                        **part,
                        **({"text": ""} if part["type"] == "text" else {"thinking": "", "signature": ""}),
                    },
                }
            )
            if part["type"] == "text":
                events.append(
                    {
                        "type": "content_block_delta",
                        "index": index,
                        "delta": {"type": "text_delta", "text": part["text"]},
                    }
                )
            else:
                for field in ("thinking", "signature"):
                    events.append(
                        {
                            "type": "content_block_delta",
                            "index": index,
                            "delta": {"type": field + "_delta", field: part[field]},
                        }
                    )
        events.extend(
            [
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": payload.get("stop_reason") or "end_turn"},
                    "usage": payload["usage"],
                },
                {"type": "message_stop"},
            ]
        )
    else:
        text = "".join(
            p["text"] for item in payload["output"] if item["type"] == "message" for p in item["content"]
        )
        events = [
            {"type": "response.output_text.delta", "delta": text},
            {"type": "response.completed", "response": payload},
        ]
    return httpx.Response(
        200,
        content="".join("data: " + json.dumps(e) + "\n\n" for e in events),
        headers={"content-type": "text/event-stream", "x-codex-turn-state": "routing"},
    )
