"""Resolve the Writer's one explicit cache breakpoint on the active story branch."""

from tavern.memories.memory import window_history


def automatic_message_id(history):
    """Use the assistant reply two completed exchanges behind the latest reply."""
    roles = {message["id"]: message["role"] for message in history}
    replies = [message["id"] for message in history
               if message["role"] == "assistant" and roles.get(message.get("parent")) == "user"]
    return replies[-3] if len(replies) >= 3 else None


def effective_message_id(session, settings):
    from tavern.conversation.context import get_path

    state = session["snapshot"].get("state", {})
    if not any(block.get("source") == "history" and block.get("enabled", True)
               and (not block.get("condition") or get_path(state, block["condition"]))
               for block in session.get("recipe", {}).get("blocks", [])):
        return None
    candidate = (automatic_message_id(session["messages"])
                 if session.get("cache_breakpoint_auto", True)
                 else session.get("cache_breakpoint_message"))
    if not candidate:
        return None
    visible, _ = window_history(session["messages"], session["snapshot"],
                                enabled=settings.chunk_mode != "off")
    return candidate if any(message["id"] == candidate for message in visible) else None


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
                    else entry.get("source") == source)]
        if indices:
            return indices[-1]
    return None
