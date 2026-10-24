"""Story evidence and portable replay of the retired delivery protocol."""

import copy
import json

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
    return {**portable(message), "content": text}
