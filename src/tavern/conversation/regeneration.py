"""Opt-in rewrite reference, separate from committed history and story state."""


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
