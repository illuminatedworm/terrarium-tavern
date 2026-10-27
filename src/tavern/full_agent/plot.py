"""The current author outline, kept separate from memories and story facts."""

from tavern.core.cache_policy import fingerprint


def plan_value(state, enabled, *, compact=False):
    checkpoint = state.get("compaction") if compact else None
    return {
        "enabled": bool(enabled),
        "text": state["plot"]["text"] if enabled else "",
        # Internal ledger identity only, never part of the business message.
        # Re-emit the whole outline after a checkpoint may have swallowed it.
        "checkpoint": fingerprint(checkpoint) if checkpoint and checkpoint.get("items") else None,
    }


def plan_message(value):
    if not value["enabled"]:
        text = "剧情规划已关闭；此前大纲不再生效，不要维护或依据它安排剧情。"
    elif value["text"]:
        text = (
            "当前有效剧情规划（取代此前版本；后续以最新更新或成功写入为准；是可修订的未来可能，不是已发生事实或角色知识）。"
            "结合当前剧情核对，阶段推进或走向改变时在此基础上修订：\n" + value["text"]
        )
    else:
        text = (
            "剧情规划已开启，当前大纲为空；此前大纲不再生效。须在本轮结束前成功调用 write_plot 建立初始大纲。"
        )
    return "<tavern_plot>\n" + text + "\n</tavern_plot>"
