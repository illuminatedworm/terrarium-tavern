"""Add one explicit breakpoint after the leading instruction messages."""

import copy


def mark_instruction_breakpoint(messages, *, text_type):
    """Keep the original messages intact; mark the last leading system/developer text block."""
    result = copy.deepcopy(messages)
    target = None
    for index, message in enumerate(result):
        if message.get("role") not in ("system", "developer"):
            break
        content = message.get("content")
        if isinstance(content, str) and content:
            target = (index, None)
        elif (isinstance(content, list) and content and isinstance(content[-1], dict)
              and content[-1].get("type") == text_type and content[-1].get("text")):
            target = (index, len(content) - 1)
    if target is None:
        return result
    index, part = target
    content = result[index]["content"]
    if part is None:
        result[index]["content"] = [{"type": text_type, "text": content,
                                     "prompt_cache_breakpoint": {"mode": "explicit"}}]
    else:
        content[part]["prompt_cache_breakpoint"] = {"mode": "explicit"}
    return result
