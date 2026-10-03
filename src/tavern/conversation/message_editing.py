"""Editing is limited to the current branch's last user/assistant exchange."""


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
