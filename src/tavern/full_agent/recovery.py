"""Resume accepted work without replaying completed tool failures."""
import copy
import json

from tavern.scripting.native_response import portable


def without_failed_tools(messages):
    failed, results, pending = {}, set(), {}
    for index, message in enumerate(messages):
        if message.get("role") == "assistant":
            pending = {call["id"]: index for call in message.get("tool_calls", [])}
        elif message.get("role") == "tool" and message.get("tool_call_id") in pending:
            try:
                result = json.loads(message.get("content", ""))
            except (ValueError, TypeError):
                continue
            if isinstance(result, dict) and "error" in result:
                call_id = message["tool_call_id"]
                failed.setdefault(pending[call_id], set()).add(call_id)
                results.add(index)
    if not failed:
        return copy.deepcopy(messages), False

    retained, changed_prefix = [], False
    for index, message in enumerate(messages):
        item = copy.deepcopy(message)
        if index in failed:
            changed_prefix = True
            item["tool_calls"] = [call for call in item["tool_calls"] if call["id"] not in failed[index]]
            if not item["tool_calls"]:
                continue
        if index in results:
            continue
        # A partial batch's native attachment still contains the removed call.
        # Later opaque reasoning can depend on it too; replay portable content.
        retained.append(portable(item) if changed_prefix else item)
    return retained, True


def resume_progress(progress):
    result = copy.deepcopy(progress)
    result["continuation"], changed = without_failed_tools(result["continuation"])
    # Filter trace records with the same pairing rules, retaining their windows.
    messages = [{**item["message"], "_resume_window_id": item["window_id"]}
                for item in result["new_trace"]]
    messages, _ = without_failed_tools(messages)
    result["new_trace"] = [{"window_id": message.pop("_resume_window_id"), "message": message}
                           for message in messages]
    if changed:
        # Opaque provider routing may retain the failed request prefix.
        result.get("request_context", {}).pop("routing", None)
    return result
