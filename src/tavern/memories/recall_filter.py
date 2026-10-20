"""Optional Tracker check of recalled archive entries before the Writer request.

Admission thresholds decide which archived episodes are candidates; this pass
asks the background model which of them the next reply actually needs. The
model sees request-local A1, A2… handles, the latest exchange and the current
input; Runtime maps its selection back to record IDs.
"""

import asyncio
import json
import time

import httpx

from tavern.core.message_adapter import provider_messages, runtime_messages
from tavern.memories.memory_records import usage_fields


class RecallFilterError(ValueError):
    """Expected provider or selection-response failure; admitted candidates may be kept."""


INSTRUCTION = (
    "你在为角色扮演的下一段正文筛选可参考的旧事。candidates 是从已归档经历中召回的候选；"
    "只保留与 current_input 和 recent_messages 直接相关、能帮助正文保持连续性的条目，"
    "只是沾边、与当前场景无关或可能误导当前状态的都不选，可以一条都不选。"
    "调用 select_recall 提交所选编号，不续写剧情。"
)
TOOL = {
    "type": "function",
    "function": {
        "name": "select_recall",
        "description": "Submit the candidate references the next reply should see; an empty list keeps none.",
        "parameters": {
            "type": "object",
            "properties": {
                "refs": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": 20,
                    "description": "Selected candidate references such as A1; each at most once.",
                }
            },
            "required": ["refs"],
            "additionalProperties": False,
        },
    },
}


def filter_messages(entries, history, user_input):
    refs = {f"A{index}": entry for index, entry in enumerate(entries, 1)}
    exchange = history[-2:] if len(history) >= 2 and history[-2]["role"] == "user" else history[-1:]
    payload = {
        "recent_messages": [{"role": m["role"], "text": m.get("model", m["raw"])} for m in exchange],
        "current_input": user_input,
        "candidates": [
            {
                "ref": ref,
                "content": entry["content"],
                "people": entry.get("people", []),
                "story_time": entry.get("story_time", "未知"),
                "kind": entry["kind"],
            }
            for ref, entry in refs.items()
        ],
    }
    messages = [
        {"role": "system", "content": INSTRUCTION},
        {
            "role": "user",
            "message_type": "tracker_input",
            "content": json.dumps({"memory_recall": payload}, ensure_ascii=False),
        },
    ]
    return messages, refs


async def filter_recall(provider, entries, history, user_input, settings, *, event=None):
    """Return the selected entries' record IDs, in candidate order."""
    event = event or (lambda kind, payload: None)
    messages, refs = filter_messages(entries, history, user_input)
    messages = provider_messages(provider, runtime_messages(messages, "background"), settings)
    started = time.perf_counter()
    try:
        response = await asyncio.wait_for(
            provider.chat_complete(
                messages,
                max_tokens=settings.memory_output_tokens,
                extra_body={
                    "tools": [TOOL],
                    "tool_choice": {"type": "function", "function": {"name": "select_recall"}},
                },
            ),
            settings.memory_timeout,
        )
    except (httpx.HTTPError, TimeoutError, ValueError) as error:
        # Transports report failed/incomplete responses as HTTPError or ValueError.
        # Keep this boundary around the request, not internal candidate assembly.
        raise RecallFilterError(str(error)) from error
    # A rejected selection still consumed a returned response's tokens.
    event(
        "usage",
        {
            "purpose": "memory_recall",
            "usage": usage_fields(response.usage),
            "calls": 1,
            "duration_s": time.perf_counter() - started,
        },
    )
    calls = getattr(provider, "last_tool_calls", []) or []
    if len(calls) != 1 or calls[0].name != "select_recall":
        raise RecallFilterError("召回筛选未返回 select_recall")
    try:
        selection = json.loads(calls[0].arguments)
    except json.JSONDecodeError as error:
        raise RecallFilterError("召回筛选未返回有效 JSON") from error
    if not isinstance(selection, dict):
        raise RecallFilterError("召回筛选须返回包含 refs 的对象")
    selected = selection.get("refs")
    if not isinstance(selected, list) or any(not isinstance(ref, str) or ref not in refs for ref in selected):
        raise RecallFilterError("召回筛选引用了本次未提供的候选编号")
    return [entry["id"] for ref, entry in refs.items() if ref in selected]
