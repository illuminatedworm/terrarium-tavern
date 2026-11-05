"""Story evidence and portable replay of the retired delivery protocol."""

import copy
import json

from tavern.core.story_segments import SEPARATOR
from tavern.scripting.native_response import portable

LEGACY_TURN_TOOLS = frozenset({"roleplay", "finish_turn"})


def story_history(messages):
    """Project old exchanges without rewriting their stored audit records.

    Only a successful delivery receipt is story evidence. Rejected deliveries
    and generated instructions for retired tools must not reappear as current
    work. Call IDs are paired inside each assistant response, not globally.
    """
    deliveries, retired, returned = {}, {}, set()
    owner, calls = None, {}
    for index, message in enumerate(messages):
        if message.get("role") == "assistant":
            owner = index
            calls = {call["id"]: call for call in message.get("tool_calls", [])}
            removed = {cid for cid, call in calls.items() if call["function"]["name"] in LEGACY_TURN_TOOLS}
            if removed:
                retired[index] = removed
        elif message.get("role") == "tool" and message.get("tool_call_id") in calls:
            cid = message["tool_call_id"]
            receipt = message.get("_roleplay_received")
            if receipt:
                deliveries[owner] = (calls[cid], receipt)
                # The Kemini transport also had receipts in the old protocol.
                retired.setdefault(owner, set()).add(cid)
            if cid in retired.get(owner, ()):
                returned.add(index)

    result, changed = [], False
    for index, message in enumerate(messages):
        if index in returned:
            changed = True
            continue
        if message.get("message_type") == "reply_correction" and any(
            name in message.get("content", "") for name in LEGACY_TURN_TOOLS
        ):
            changed = True
            continue
        item = copy.deepcopy(message)
        if index in retired:
            changed = True
            item["tool_calls"] = [call for call in item["tool_calls"] if call["id"] not in retired[index]]
            if index in deliveries:
                call, receipt = deliveries[index]
                if item.get("content"):
                    # Earlier ordinary text was process commentary. Keep it as
                    # its own historical record, never merge it into the story.
                    commentary = portable(item)
                    commentary["tool_calls"] = []
                    result.append(commentary)
                item["content"] = json.loads(call["function"]["arguments"])["content"]
                item["_story_received"] = copy.deepcopy(receipt)
            if not item.get("content") and not item.get("tool_calls"):
                continue
        result.append(portable(item) if changed else item)
    return result, changed


def committed_story(message, text):
    """The saved model text is authoritative after callbacks and regex rules."""
    call_id = message.get("_story_call_id")
    if call_id:
        call = next(call for call in message["tool_calls"] if call["id"] == call_id)
        if json.loads(call["function"]["arguments"])["content"] == text:
            return message
        item = portable(message)
        for candidate in item["tool_calls"]:
            if candidate["id"] == call_id:
                candidate["function"]["arguments"] = json.dumps({"content": text}, ensure_ascii=False)
        return item
    if message.get("content") == text:
        return message
    return portable({**message, "content": text})


def committed_records(records, text, segment_count):
    """Replay accepted segments once, or project the final text at the last delivery.

    Whole-message model regex/macros can change or remove segment boundaries.
    In that case one canonical delivery replaces the accepted delivery calls in
    the request only; their original arguments/results remain in the audit log.
    Other tools and commentary keep their order. Native state after a rewritten
    exchange cannot retain the superseded prose or deleted function calls.
    """
    deliveries = []
    for index, record in enumerate(records):
        if not record.get("_story_received"):
            continue
        segments = record.get("_story_segments")
        if not segments and record.get("_story_call_id"):
            segments = {record["_story_call_id"]: 1}
        if segments:
            for call in record.get("tool_calls", []):
                if call["id"] in segments:
                    deliveries.append(
                        (
                            index,
                            call["id"],
                            segments[call["id"]],
                            json.loads(call["function"]["arguments"])["content"],
                        )
                    )
        else:
            deliveries.append((index, None, 1, record.get("content", "")))
    if not deliveries:
        return records
    if [part for _, _, part, _ in deliveries] == list(range(1, segment_count + 1)) and SEPARATOR.join(
        content for _, _, _, content in deliveries
    ) == text:
        return records
    last_index, last_call, _, _ = deliveries[-1]
    accepted = {}
    for index, call_id, _, _ in deliveries:
        accepted.setdefault(index, set()).add(call_id)
    result, removed, changed = [], set(), False
    for index, original in enumerate(records):
        item = original
        if item.get("role") == "assistant":
            removed = accepted.get(index, set()).copy()
            if index == last_index:
                removed.discard(last_call)
            if removed or index == last_index:
                changed = True
                item = copy.deepcopy(item)
                item["tool_calls"] = [
                    call for call in item.get("tool_calls", []) if call["id"] not in removed
                ]
                if index == last_index:
                    item["_story_call_id"] = last_call
                    item = committed_story(item, text)
                else:
                    if None in removed:
                        item["content"] = ""
                    item.pop("_story_received", None)
                    item.pop("_story_call_id", None)
                item.pop("_story_segments", None)
        elif item.get("role") == "tool" and item.get("tool_call_id") in removed:
            continue
        result.append(portable(item) if changed else item)
    return result
