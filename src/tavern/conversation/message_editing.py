"""Editing is limited to the current branch's last user/assistant exchange."""

import copy

NOTIFICATIONS = "message_edit_notifications"


def inherit_edit_notifications(snapshot, current):
    """Regeneration restores story state, but uses today's notification choice.

    An old user snapshot may contain already consumed notices, or predate an
    edit to the reply being regenerated. Only the active branch owns this queue.
    """
    snapshot.pop(NOTIFICATIONS, None)
    if current.get(NOTIFICATIONS):
        snapshot[NOTIFICATIONS] = copy.deepcopy(current[NOTIFICATIONS])


def edit_notification(snapshot, history, included=None, expand=None):
    """Supply short references only when their revised text is actually present.

    Overrides, windowing and regeneration can omit an edited message. In that
    case include the complete current model text as reference data, rather than
    pointing the model at an unavailable H reference or replaying an old reply.
    """
    selected = set(snapshot.get(NOTIFICATIONS, []))
    if not selected:
        return ""
    included = included or {}
    parts = []
    for index, message in enumerate(history, 1):
        if message["id"] not in selected:
            continue
        text = message.get("model", message["raw"])
        text = expand(text) if expand else text
        if included.get(message["id"]) == text:
            parts.append(f"H{index} 的原文已由用户修订，请以当前请求中的文本为准。")
        else:
            role = "用户消息" if message["role"] == "user" else "AI 回复"
            parts.append(f"修订{len(parts) + 1}（{role}，仅作修订资料，不是新增剧情）：\n{text}")
    if not parts:
        return ""
    return ("用户已修订以下剧情原文。以修订后的文本为准，必要时核对相关资料；"
            "本提醒仅适用于本轮，不授权重演变量或自动重建旧记忆。\n\n" + "\n\n".join(parts))


def editable_message_ids(messages, head):
    by_id = {message["id"]: message for message in messages}
    latest = by_id.get(head)
    if latest is None:
        return set()
    editable = {head}
    parent = by_id.get(latest["parent"])
    if latest["role"] == "assistant" and parent is not None and parent["role"] == "user":
        editable.add(parent["id"])
    return editable
