"""Lossless provider attachments beside the portable conversation, not instead of it."""

import copy
from dataclasses import dataclass
from typing import Literal

from tavern.core.cache_policy import fingerprint
from tavern.scripting.http_errors import upstream_detail

REASONING_FIELDS = ("reasoning", "reasoning_details", "reasoning_content")


@dataclass(frozen=True)
class ResponseCompletion:
    action: Literal["tools", "continue", "end", "incomplete"]
    reason: str
    detail: str = ""


def response_completion(api, payload, calls, *, secret=""):
    """Interpret an entire provider response, independently of transport EOF.

    Keep ChatResponse.finish_reason compatible with the other agents. Full
    Agent needs the original distinction between yielding for tools and ending
    its turn; a Responses completion or Gemini STOP alone does not supply it.
    """
    if api == "chat_completions":
        reason = payload["choices"][0]["finish_reason"]
        action = {"stop": "end", "tool_calls": "tools"}.get(reason, "incomplete")
    elif api == "anthropic":
        reason = payload["stop_reason"]
        action = {
            "end_turn": "end",
            "stop_sequence": "end",
            "tool_use": "tools",
            "pause_turn": "continue",
        }.get(reason, "incomplete")
    else:
        reason = payload["status"] if api == "responses" else payload["candidates"][0]["finishReason"]
        complete = reason == ("completed" if api == "responses" else "STOP")
        action = ("tools" if calls else "end") if complete else "incomplete"
    if action == "tools" and not calls:
        action = "incomplete"
    return ResponseCompletion(action, reason, upstream_detail(payload, secret=secret))


def completion_message(fallback: str, completion: ResponseCompletion | None, reason: str = "") -> str:
    detail = (completion.detail or completion.reason) if completion is not None else reason
    return f"{detail}；{fallback}" if detail else fallback


def message_digest(message):
    calls = [
        {"id": call["id"], "name": call["function"]["name"], "arguments": call["function"]["arguments"]}
        for call in message.get("tool_calls", [])
    ]
    return fingerprint({"text": message.get("content") or "", "calls": calls})


def attachment(payload, origin, message):
    api = origin["api"]
    if api not in ("anthropic", "responses"):
        return None
    return {
        "version": 1,
        "origin": copy.deepcopy(origin),
        "message_sha256": message_digest(message),
        "response_id": payload.get("id"),
        "items": copy.deepcopy(payload.get("content" if api == "anthropic" else "output", [])),
    }


def portable(message):
    """Drop only opaque/model-specific state; keep prose and paired tool exchanges."""
    result = copy.deepcopy(message)
    for key in (*REASONING_FIELDS, "_native_response", "_response_origin", "_response_digest"):
        result.pop(key, None)
    if result.get("web_sources") and isinstance(result.get("content"), str):
        # A model/account switch invalidates native items, not the public evidence.
        result["content"] += "\n\n网页来源：\n" + "\n".join(
            f"{row['title']} — {row['url']}" for row in result["web_sources"]
        )
    return result


def prepare_messages(messages, origin):
    result, invalid = [], []
    for index, message in enumerate(messages):
        native = message.get("_native_response")
        previous = native.get("origin") if native else message.get("_response_origin")
        reason = None
        if previous and previous != origin:
            reason = "provider_or_model_changed"
        elif native and (
            native.get("version") != 1 or native.get("message_sha256") != message_digest(message)
        ):
            reason = "message_changed"
        elif message.get("_response_digest") and message["_response_digest"] != message_digest(message):
            reason = "message_changed"
        if reason:
            invalid.append({"index": index, "reason": reason})
        result.append(portable(message) if reason else copy.deepcopy(message))
    return result, invalid


def native_items(message, api):
    """Adapters can also be used standalone; never trust a stale text attachment."""
    native = message.get("_native_response")
    if (
        native
        and native.get("version") == 1
        and native.get("origin", {}).get("api") == api
        and native.get("message_sha256") == message_digest(message)
    ):
        return copy.deepcopy(native["items"])
    return None
