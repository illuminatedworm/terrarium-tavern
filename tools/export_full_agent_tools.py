"""Refresh/check source descriptions and executable examples in Full Agent docs."""

import argparse
import json
from pathlib import Path

from tavern.full_agent.protocol import RECOVERY, success
from tavern.full_agent.tools import SPECS
from tavern.novel.service import DELIVERY_NOTICE, START_NOTICE
from tavern.novel.storage import MAX_LIVING, MAX_STARTS

ROOT = Path(__file__).resolve().parents[1]
SUMMARY = ROOT / "docs/reference/full-agent-tools-summary.md"
REFERENCE = ROOT / "docs/reference/full-agent-tools.md"
DESCRIPTION_MARKER = "## 附录：工具描述全文与最小示例"
ERROR_MARKER = "## 错误码（由源码生成）"

# Small independent examples; backend tests execute these requests against a
# shared synthetic scene and compare actual result envelopes with these values.
NOTE_KEY = "计划/港口"
ENTRY = {
    "ref": "L1",
    "title": "港口",
    "content": "港口有一座灯塔。",
    "keys": ["港口"],
    "secondary_keys": [],
    "enabled": True,
}
NOTE_META = {"key": NOTE_KEY, "kind": "plan", "status": "active", "sources": []}
NOTICE = "正文完成后与本回合统一提交。"
EXAMPLES = {
    "submit_story": (
        {"content": "她在港口的灯塔下等你。"},
        {"saved_to_draft": True, "ref": "H2", "message": "已接收第 1/3 段；可继续追加或结束。"},
    ),
    "present_ui": (
        {
            "spec": {
                "root": "map",
                "elements": {
                    "map": {
                        "type": "Scene",
                        "props": {
                            "title": "港口",
                            "height": 240,
                            "selection": {"$state": "/selection"},
                            "items": [
                                {"kind": "label", "x": 50, "y": 50, "text": "灯塔", "anchor": "middle"}
                            ],
                        },
                        "children": [],
                    }
                },
                "state": {"selection": "overview", "detail": ""},
            }
        },
        {
            "saved_to_draft": True,
            "message": "图形已接收，随本轮正文统一提交；界面操作仅改变本地显示或填写输入框。",
        },
    ),
    "find_lore_for_edit": (
        {"query": "港口"},
        {
            "matches": [{"ref": "L1", "title": "港口", "preview": "港口 港口 港口有一座灯塔。"}],
            "notice": "preview 仅用于定位；需要了解或核对目标完整内容时，使用 read_lore_entry。",
        },
    ),
    "read_lore_entry": ({"ref": "L1"}, ENTRY),
    "recall_lore": (
        {"query": "港口"},
        {"matches": [{"title": "港口", "text": "<lorebook>\n港口有一座灯塔。\n</lorebook>"}]},
    ),
    "search_history": (
        {"query": "港口"},
        {
            "matches": [{"ref": "H1", "role": "user", "preview": "我明天去港口。"}],
            "notice": "使用 read 工具读取完整原文。",
        },
    ),
    "read_history": ({"ref": "H1"}, {"ref": "H1", "role": "user", "text": "我明天去港口。"}),
    "write_handoff": (
        {"current": [{"text": "明天去港口，相关笔记：计划/港口。"}]},
        {"saved_to_draft": True, "window": 1, "chapters": [], "notice": NOTICE},
    ),
    "new_context": (
        {},
        {
            "saved_to_draft": True,
            "window": 2,
            "chapters": [],
            "notice": NOTICE,
        },
    ),
    "search_notes": (
        {"query": "港口"},
        {"matches": [{**NOTE_META, "preview": "明天去港口。"}], "notice": "使用 read 工具读取完整原文。"},
    ),
    "read_note": ({"key": NOTE_KEY}, {**NOTE_META, "text": "明天去港口。"}),
    "write_note": (
        {"key": NOTE_KEY, "content": "用户表示明天去港口。", "kind": "fact", "sources": ["H1"]},
        {"key": NOTE_KEY, "saved_to_draft": True},
    ),
    "write_plot": (
        {
            "content": "## 核心\n- 守塔人为何藏起旧信？\n"
            "## 锚点\n- 【严酷】旧信出自守塔人已故的兄长；七日后补给船靠港，守塔人随船离岛。\n"
            "## 各方动向\n- 守塔人：想让旧信永远沉默；若用户追问，会提前出海。\n"
            "## 当前阶段\n- 若用户前往港口，守塔人的隐瞒引出冲突。\n## 伏笔\n- 旧信的火漆：远期视选择揭开来历，尚未回收。"
        },
        {"saved_to_draft": True},
    ),
    "archive_note": ({"key": NOTE_KEY}, {"key": NOTE_KEY, "archived": True}),
    "read_state": ({}, {"path": "", "scope": "mvu", "text": '{"hp":8}'}),
    "patch_state": (
        {"operations": [], "comment": "本轮变量无变化"},
        {
            "saved_to_draft": True,
            "patch_state_completed": True,
            "notice": "已确认本轮变量无变化；未执行变量补丁或脚本回调。",
        },
    ),
    "revise_content": (
        {
            "operations": [{"op": "set_character", "field": "description", "value": "新的角色描述。"}],
            "comment": "用户要求调整描述",
        },
        {
            "saved_to_draft": True,
            "added_refs": [],
            "characters": [{"ref": "description", "text": "新的角色描述。"}],
            "entries": [],
            "removed_refs": [],
        },
    ),
    "read_character": ({"ref": "description"}, {"ref": "description", "exists": True, "text": "角色描述。"}),
    "set_retrieval_query": ({"query": ""}, {"saved_to_draft": True}),
    "start_novel_research": (
        {"question": "查证人物早年经历", "background": "本局尚未遇见此人"},
        {
            "ref": "R1",
            "status": "running",
            "created": True,
            "notice": START_NOTICE,
        },
    ),
    "list_novel_research": (
        {},
        {"investigators": [], "slots_remaining": MAX_LIVING, "starts_remaining_this_turn": MAX_STARTS},
    ),
    "message_novel_research": (
        {"ref": "R1", "message": "请注明原著时期"},
        {"ref": "R1", "status": "running"},
    ),
    "wait_novel_research": ({"ref": "R1", "timeout_seconds": 30}, {"ref": "R1", "status": "completed"}),
    "resume_novel_research": ({"ref": "R1"}, {"ref": "R1", "status": "running"}),
    "destroy_novel_research": ({"ref": "R1"}, {"ref": "R1", "status": "destroyed"}),
}

for _name in (
    "message_novel_research",
    "wait_novel_research",
    "resume_novel_research",
):
    EXAMPLES[_name][1]["notice"] = DELIVERY_NOTICE


def encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def descriptions():
    parts = [
        DESCRIPTION_MARKER,
        "",
        "<details>",
        "<summary>查看工具描述原文与 JSON 示例</summary>",
        "",
        "工具描述与源码同步。各示例独立使用同一虚构场景和默认返回选项：剧情规划已开启，变量更新已授权，设定和笔记修改所需的当前全文已在上下文中。原著调查示例还要求用户已绑定文件并开启参考；异步状态以实际任务为准。参数与错误恢复见[完整协议](full-agent-tools.md)。",
        "",
    ]
    assert set(EXAMPLES) == set(SPECS)
    for name, (_, description) in SPECS.items():
        arguments, result = EXAMPLES[name]
        parts.extend(
            [
                f"### `{name}`",
                "",
                "```text",
                description,
                "```",
                "",
                "请求：",
                "",
                "```json",
                encoded(arguments),
                "```",
                "",
                "成功返回：",
                "",
                "```json",
                encoded(success(result)),
                "```",
                "",
            ]
        )
    parts.extend(
        [
            "### 运行时追加的描述",
            "",
            "以下文本以一个空格追加在工具描述末尾；占位符发送前替换为配置值。",
            "",
            "| 工具 | 追加文本全文 | 条件 |",
            "|---|---|---|",
            "| `write_handoff` | `Limits: current {handoff_tokens} tokens, each chapter {chapter_tokens}, all chapters {chapters_total_tokens}.` | 当前交接、单章与章节总量上限。 |",
            "| `new_context` | `Limits: current {handoff_tokens} tokens, each chapter {chapter_tokens}, all chapters {chapters_total_tokens}.` | 同上，适用于换窗时提交的交接。 |",
            "| `write_note` | `Content limit: {note_tokens} tokens.` | 当前单条笔记上限。 |",
            "| `write_plot` | `Content limit: {plot_tokens} tokens.` | 当前剧情规划上限。 |",
            "| `recall_lore` | `Uses {channel_names} recall.` | 提供检索设置时追加；通道依次去重，映射为 keyword、BM25、semantic；多项使用逗号及最后的 ` and ` 连接。 |",
            "| `recall_lore` | `Reranking orders and filters candidates by relevance.` | 开启 reranker 时追加在上一句之后。 |",
            "",
            "工具目录由会话开关及相关功能确定，变量与设定授权在执行时检查。动态追加描述见上表。",
            "",
            "</details>",
            "",
        ]
    )
    return "\n".join(parts)


def errors():
    parts = [
        ERROR_MARKER,
        "",
        "`retryable=true` 表示处理 `next_action` 指出的原因后可再次调用。",
        "",
        "| code | 可修正重试 | 默认 next_action |",
        "|---|---|---|",
    ]
    parts.extend(
        f"| `{code}` | {'是' if retry else '否'} | {action} |" for code, (retry, action) in RECOVERY.items()
    )
    return "\n".join(parts) + "\n"


def generated_documents():
    for path, marker, render in (
        (SUMMARY, DESCRIPTION_MARKER, descriptions),
        (REFERENCE, ERROR_MARKER, errors),
    ):
        source = path.read_text(encoding="utf-8")
        head = source.split(marker, 1)[0].rstrip()
        yield path, head + "\n\n" + render()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check", action="store_true", help="Fail if committed documentation differs from source."
    )
    args = parser.parse_args()
    for path, expected in generated_documents():
        if args.check:
            if path.read_text(encoding="utf-8") != expected:
                raise SystemExit(f"工具文档需要更新：{path.relative_to(ROOT)}")
        else:
            path.write_text(expected, encoding="utf-8")
    print(f"已{'核对' if args.check else '更新'} {len(SPECS)} 个工具的描述、示例和错误码。")


if __name__ == "__main__":
    main()
