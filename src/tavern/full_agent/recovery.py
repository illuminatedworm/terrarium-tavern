"""Resume accepted work without replaying completed tool failures."""

import copy
import json

from tavern.full_agent.protocol import failed as tool_failed
from tavern.scripting.native_response import portable


def load_progress(progress):
    """Read the persisted envelope before any of its draft fields are used."""
    version = progress.get("version", 1)
    if type(version) is not int or version not in (1, 2, 3, 4):
        raise ValueError("Full Agent 回合进度格式不受支持；原进度未修改")
    if version >= 2 and (not isinstance(progress.get("output_id"), str) or not progress["output_id"]):
        raise ValueError("Full Agent 回合进度缺少有效的正文身份；原进度未修改")
    if version < 3:
        progress = copy.deepcopy(progress)
        if version == 1:
            progress["output_id"] = progress["script_mirror"]["messages"][-1]["id"]
        progress["story"] = progress.pop("roleplay", None)
        progress["model_completed"] = progress.pop("finish_requested", False)
        # Only saved, unpaired calls may use the compatibility executor. New
        # model responses never regain these tools through an old checkpoint.
        progress["legacy_protocol"] = True
        progress["version"] = 3
    return progress


def without_failed_tools(messages):
    failed, results, pending, returned = {}, set(), {}, set()
    for index, message in enumerate(messages):
        if message.get("role") == "assistant":
            pending = {call["id"]: index for call in message.get("tool_calls", [])}
        elif message.get("role") == "tool" and message.get("tool_call_id") in pending:
            call_id = message["tool_call_id"]
            # Providers may reuse a call ID in later responses; only its own
            # assistant batch can supply a matching result.
            returned.add((pending[call_id], call_id))
            try:
                result = json.loads(message.get("content", ""))
            except (ValueError, TypeError):
                continue
            if tool_failed(result):
                failed.setdefault(pending[call_id], set()).add(call_id)
                results.add(index)
    if not failed:
        return copy.deepcopy(messages), False
    for index, removed in failed.items():
        # A legacy finish from a failed batch cannot finalize a resumed draft.
        removed.update(
            call["id"]
            for call in messages[index]["tool_calls"]
            if call["function"]["name"] == "finish_turn" and (index, call["id"]) not in returned
        )

    retained, changed_prefix = [], False
    for index, message in enumerate(messages):
        item = copy.deepcopy(message)
        if index in failed:
            changed_prefix = True
            item["tool_calls"] = [call for call in item["tool_calls"] if call["id"] not in failed[index]]
            if not item["tool_calls"] and not item.get("_story_received"):
                continue
        if index in results:
            continue
        # A partial batch's native attachment still contains the removed call.
        # Later opaque reasoning can depend on it too; replay portable content.
        retained.append(portable(item) if changed_prefix else item)
    return retained, True


def resume_progress(progress):
    result = copy.deepcopy(load_progress(progress))
    terminal_failed = False
    for message in reversed(result["continuation"]):
        if message.get("role") == "assistant":
            terminal_failed &= message.get("_model_completion", {}).get("action") == "end"
            break
        terminal_failed |= bool(message.get("_tool_error"))
    result["continuation"], changed = without_failed_tools(result["continuation"])
    # Filter trace records with the same pairing rules, retaining their windows.
    messages = [{**item["message"], "_resume_window_id": item["window_id"]} for item in result["new_trace"]]
    messages, _ = without_failed_tools(messages)
    result["new_trace"] = [
        {"window_id": message.pop("_resume_window_id"), "message": message} for message in messages
    ]
    if changed:
        # Opaque provider routing may retain the failed request prefix.
        result.get("request_context", {}).pop("routing", None)
        if terminal_failed:
            result["model_completed"] = False
        # Earlier tool errors may already have been handled by the model. They
        # do not reopen an accepted natural end or turn a local callback retry
        # into another paid model request.
        if not result.get("model_completed") and not result.get("resume_reason"):
            result["resume_reason"] = "上次工具调用失败，已保留成功结果。"
    return result
