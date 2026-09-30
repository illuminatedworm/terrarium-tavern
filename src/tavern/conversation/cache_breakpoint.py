"""Resolve the Writer's one explicit cache breakpoint on the active story branch."""

import copy

from tavern.core.cache_policy import fingerprint
from tavern.memories.memory import window_history


def reply_ids(history):
    roles = {message["id"]: message["role"] for message in history}
    return [message["id"] for message in history
            if message["role"] == "assistant" and roles.get(message.get("parent")) == "user"]


def automatic_message_id(history, offset=3):
    replies = reply_ids(history)
    return replies[-offset] if len(replies) >= offset else None


def reset_follow(history, follow=None):
    follow = follow or {}
    return {"interval": follow.get("interval", 8), "offset": follow.get("offset", 3),
            "anchor": automatic_message_id(history, follow.get("offset", 3)), "progress": 0}


def advance_follow(history, follow, advance=False):
    if "anchor" not in follow:
        return reset_follow(history, follow)
    result = dict(follow)
    if advance:
        result["progress"] += 1
    if result["progress"] >= result["interval"]:
        return reset_follow(history, result)
    if result["anchor"] and result["anchor"] not in reply_ids(history):
        result["anchor"] = automatic_message_id(history, result["offset"])
    return result


def planned_message_id(session):
    if not session.get("cache_breakpoint_auto", True):
        return session.get("cache_breakpoint_message")
    follow = session.get("cache_breakpoint_follow", {})
    return advance_follow(session["messages"], follow)["anchor"]


def effective_message_id(session, settings):
    from tavern.conversation.context import get_path

    state = session["snapshot"].get("state", {})
    full = session.get("agent_mode") == "full_agent"
    if not full and not any(block.get("source") == "history" and block.get("enabled", True)
               and (not block.get("condition") or get_path(state, block["condition"]))
               for block in session.get("recipe", {}).get("blocks", [])):
        return None
    candidate = planned_message_id(session)
    if not candidate or candidate not in reply_ids(session["messages"]):
        return None
    if full:
        start = session["snapshot"].get("full_agent", {}).get("window", {}).get("start", 0)
        visible = session["messages"][start:]
    else:
        visible, _ = window_history(session["messages"], session["snapshot"],
                                    enabled=settings.chunk_mode != "off")
    return candidate if any(message["id"] == candidate for message in visible) else None


def breakpoint_reason(session, effective_id):
    candidate = planned_message_id(session)
    if candidate and candidate not in reply_ids(session["messages"]):
        return "所选回复已不在当前分支，暂用固定前缀"
    if candidate and not effective_id:
        return "所选回复不在实际上下文窗口，暂用固定前缀"
    if not candidate:
        follow = session.get("cache_breakpoint_follow", {})
        if session.get("cache_breakpoint_auto", True) and len(reply_ids(session["messages"])) >= follow.get("offset", 3):
            return "尚未到下次跟进，暂用固定前缀"
        return ("已完成回复不足目标位置，暂用固定前缀" if session.get("cache_breakpoint_auto", True)
                else "手动固定于可用的固定前缀")
    return None


def context_breakpoint_index(trace, message_id):
    if message_id:
        for index, entry in enumerate(trace):
            if (entry.get("source") == "history" and entry.get("role") == "assistant"
                    and entry.get("message_id") == message_id):
                return index
    first_history = next((index for index, entry in enumerate(trace)
                          if entry.get("source") == "history"), len(trace))
    for source in ("constant", "character"):
        indices = [index for index, entry in enumerate(trace)
                   if index < first_history and
                   (entry.get("lore_region") == source if source == "constant"
                    else entry.get("context_source", entry.get("source")) == source)]
        if indices:
            return indices[-1]
    indices = [index for index, entry in enumerate(trace[:first_history]) if is_static(entry)]
    return indices[-1] if indices else None


def is_static(entry):
    source = entry.get("context_source", entry.get("source"))
    return source in {"rules", "system", "character", "persona", "examples", "custom", "session_preference"} or entry.get("lore_region") == "constant"


def prepare_context(context, session, history, snapshot, settings):
    """One branch/window-aware plan used by preview, runtime, and all serializers."""
    current = {**session, "messages": history, "snapshot": snapshot}
    target = effective_message_id(current, settings)
    trace = context["trace"]
    at = context_breakpoint_index(trace, target)
    messages = context["messages"]
    leading = True
    for index, message in enumerate(messages):
        entry = trace[index]
        fixed = leading and is_static(entry)
        if not fixed:
            leading = False
        entry["cache_static"] = fixed
    context["cache"] = {"mode": settings.cache.mode, "status": "planned" if settings.cache.mode == "runtime" else "cloud",
                        "planned_index": at, "message_id": trace[at].get("message_id") if at is not None else None,
                        "source": trace[at]["source"] if at is not None else None,
                        "scope": session["id"],
                        "revision": session.get("revision"),
                        "window": str(snapshot.get("full_agent", {}).get("window", {}).get("id") or
                                      fingerprint(snapshot.get("context_window", {})))}
    return context


def model_messages(context):
    messages = copy.deepcopy(context["messages"])
    cache = context.get("cache", {})
    for i, message in enumerate(messages):
        message["_cache_static"] = context["trace"][i].get("cache_static", False)
        if cache.get("mode") == "runtime" and i == cache.get("planned_index"):
            message.update(_cache_breakpoint=True, _cache_scope=cache["scope"], _cache_window=cache["window"])
            message.update(_cache_message_id=cache.get("message_id"), _cache_source=cache.get("source"))
    return messages
