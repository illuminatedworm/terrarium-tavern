"""Resolve a whole exchange from its immutable, pre-turn story snapshot."""

import copy
from dataclasses import dataclass

from tavern.core.models import TurnRequest


@dataclass
class TurnStart:
    history: list[dict]
    snapshot: dict
    text: str
    reused_user: dict | None


def turn_start(session: dict, request: TurnRequest) -> TurnStart:
    history = session["messages"]
    user = None
    text = request.text
    if request.mode == "regenerate":
        target = request.message_id or session["head"]
        index = next((i for i, message in enumerate(history) if message["id"] == target), None)
        if (
            index is None
            or index == 0
            or history[index]["role"] != "assistant"
            or history[index - 1]["role"] != "user"
        ):
            raise ValueError("请选择可重生成的角色回复")
        user = history[index - 1]
        history = history[: index - 1]
        text = user["raw"] if request.replacement_text is None else request.replacement_text
    if not text.strip():
        raise ValueError("请输入消息")
    # A user snapshot is captured before scripts and Tracker run. Replaying a
    # replacement must never inherit decisions made from the previous input.
    snapshot = copy.deepcopy(user["snapshot"] if user else session["snapshot"])
    reused = copy.deepcopy(user) if user and request.replacement_text is None else None
    return TurnStart(history, snapshot, text, reused)


def reply_for_regeneration(request, session):
    if request.mode != "regenerate" or not request.regenerate_include_reply:
        return ""
    target = request.message_id or session["head"]
    message = next(m for m in session["messages"] if m["id"] == target)
    return message.get("model", message["raw"])


def regeneration_reference(text):
    if not text:
        return ""
    return (
        "以下是用户选择提供的待重写 AI 回复全文，仅作为本轮重写参考，"
        "不作为已发生剧情或当前状态。请根据当前用户输入和重写要求生成替代回复。\n"
        "<rewrite_reference>\n" + text + "\n</rewrite_reference>"
    )
