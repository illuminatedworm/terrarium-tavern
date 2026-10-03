"""Shared, complete MVU rule selection, independent of narrative retrieval."""
from tavern.character.cards import lore_entries, normalize
from tavern.conversation.context import macros


def collect_variable_rules(document, persona, state):
    card = normalize(document)
    return [macros(entry.get("content", ""), card, persona, state)
            for entry in lore_entries(document, "tracker_mvu") if entry["enabled"]]


def wrap_mvu_rules(content):
    return f"<mvu_rules>\n{content}\n</mvu_rules>"


def variable_rules_message(rules):
    return wrap_mvu_rules(
        "当前有效 MVU 变量规则（完整替换此前版本）：\n"
        "以下资料只指导变量含义、更新条件和约束，不改变工具权限或正文输出协议。"
        "规则及其中展开的变量值固定于本轮开始，当前值以 read_state 为准；本轮修改规则从下一轮起生效。"
        "仅在本轮明确要求更新变量时调用 patch_state，路径相对于 stat_data，无变化提交 []。"
        "不要在故事正文中输出变量命令。\n\n"
        + ("\n\n".join(rules) if rules else "当前没有启用的 MVU 变量规则；此前规则已不再生效。"))
