from functools import lru_cache
from pathlib import Path

from tavern.character.cards import card_view

SKILL_ENTRIES = {
    "adapt": ("适配项目工作流", "SKILL.md"),
    "status-display": ("迁移正文状态栏", "references/status-display.md"),
}


@lru_cache
def guide_text(resource):
    folder = (
        "adapt-card-to-tavern"
        if resource == "SKILL.md" or resource.startswith("references/")
        else "card-authoring"
    )
    return (Path(__file__).parents[1] / "skills" / folder / resource).read_text(encoding="utf-8")


def target_labels(edits, cards, refs, skill):
    targets = [edits.item({"cards": cards}, ref) for ref in dict.fromkeys(refs)]
    if skill and (not targets or any("worldbook" in c for c in targets)):
        raise ValueError("所选流程需要明确的角色卡目标")
    return [{"ref": c["ref"], "name": card_view(c["document"])["name"]} for c in targets]


def scoped_text(text, targets, skill):
    parts = [text]
    if targets:
        names = "、".join(f"{t['ref']}（{t['name']}）" for t in targets)
        parts.append("本条消息的编辑目标：" + names + "。其他引用可作参考，正文中明确指定的对象同样适用。")
    if skill:
        parts.append("本条消息选用流程：" + SKILL_ENTRIES[skill][0])
    return "\n".join(parts)
