"""Exclude retired broad card tools from model replay while preserving stored audit rows."""

import copy
import json

from tavern.full_agent.content import CHARACTER_FIELDS
from tavern.scripting.native_response import portable

CONTENT_TOOLS_VERSION = 1
CONTENT_ACTIONS = {"set_character", "add_lore", "update_lore", "remove_lore"}


def retired_content_call(message, call):
    if message.get("_content_tools_version") == CONTENT_TOOLS_VERSION:
        return False
    function = call["function"]
    if function["name"] not in {"read_character", "revise_content"}:
        return False
    try:
        args = json.loads(function["arguments"])
    except (ValueError, TypeError):
        return True
    if not isinstance(args, dict):
        return True
    if function["name"] == "read_character":
        return args.get("ref") not in CHARACTER_FIELDS
    operations = args.get("operations")
    return not isinstance(operations, list) or any(
        not isinstance(op, dict) or op.get("op") not in CONTENT_ACTIONS for op in operations
    )


def content_history(messages):
    calls, results, pending = {}, set(), {}
    for index, message in enumerate(messages):
        if message.get("role") == "assistant":
            pending = {}
            for call in message.get("tool_calls", []):
                retired = retired_content_call(message, call)
                pending[call["id"]] = retired
                if retired:
                    calls.setdefault(index, set()).add(call["id"])
        elif message.get("role") == "tool" and pending.get(message.get("tool_call_id")):
            results.add(index)
    retained, changed = [], False
    for index, message in enumerate(messages):
        item = copy.deepcopy(message)
        if index in calls:
            changed = True
            item["tool_calls"] = [call for call in item["tool_calls"] if call["id"] not in calls[index]]
            if not item["tool_calls"]:
                continue
        if index in results:
            continue
        retained.append(portable(item) if changed else item)
    return retained, changed
