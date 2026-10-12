"""Refresh/check source descriptions and executable examples in Full Agent docs."""
import argparse
import json
from pathlib import Path

from tavern.full_agent.protocol import RECOVERY, success
from tavern.full_agent.tools import SPECS

ROOT = Path(__file__).resolve().parents[1]
SUMMARY = ROOT / "docs/reference/full-agent-tools-summary.md"
REFERENCE = ROOT / "docs/reference/full-agent-tools.md"
DESCRIPTION_MARKER = "## 附录：工具描述全文与最小示例"
ERROR_MARKER = "## 错误码（由源码生成）"

# Small independent examples; backend tests execute these requests against a
# shared synthetic scene and compare actual result envelopes with these values.
NOTE_KEY = "计划/港口"
ENTRY = {"ref": "L1", "title": "港口", "content": "港口有一座灯塔。", "keys": ["港口"],
         "secondary_keys": [], "enabled": True}
NOTE_META = {"key": NOTE_KEY, "kind": "plan", "status": "active", "sources": [], "stale": False}
NOTICE = "正文完成后与本回合统一提交。"
EXAMPLES = {
    "find_lore_for_edit": ({"query": "港口"}, {
        "matches": [{"ref": "L1", "title": "港口", "preview": "港口 港口 港口有一座灯塔。"}],
        "notice": "preview 仅用于定位；当前上下文没有目标的完整最新正文时，先用 read_lore_entry 读取再修改或删除。"}),
    "read_lore_entry": ({"ref": "L1"}, ENTRY),
    "recall_lore": ({"query": "港口"}, {"matches": [{"text": "<lorebook>\n港口有一座灯塔。\n</lorebook>"}]}),
    "search_history": ({"query": "港口"}, {
        "matches": [{"ref": "H1", "role": "user", "preview": "我明天去港口。"}],
        "notice": "使用 read 工具读取完整原文。"}),
    "read_history": ({"ref": "H1"}, {"ref": "H1", "role": "user", "text": "我明天去港口。"}),
    "write_handoff": ({"content": "明天去港口，相关笔记：计划/港口。"},
                      {"saved_to_draft": True, "window": 1, "notice": NOTICE}),
    "new_context": ({"handoff": "明天去港口，相关笔记：计划/港口。", "keep_recent": 0},
                    {"saved_to_draft": True, "window": 2, "notice": NOTICE}),
    "search_notes": ({"query": "港口"}, {
        "matches": [{**NOTE_META, "preview": "明天去港口。"}], "notice": "使用 read 工具读取完整原文。"}),
    "read_note": ({"key": NOTE_KEY}, {**NOTE_META, "text": "明天去港口。"}),
    "write_note": ({"key": NOTE_KEY, "content": "用户表示明天去港口。", "kind": "fact", "sources": ["H1"]},
                   {"key": NOTE_KEY, "saved_to_draft": True}),
    "write_plot": ({"content": "近期：若玩家前往港口，让守塔人的隐瞒引出冲突；远期：视选择揭开旧信来历。"},
                   {"saved_to_draft": True}),
    "archive_note": ({"key": NOTE_KEY}, {"key": NOTE_KEY, "archived": True}),
    "read_state": ({}, {"path": "", "scope": "mvu", "text": '{"hp":8}'}),
    "patch_state": ({"operations": [], "comment": "本轮变量无变化"}, {
        "saved_to_draft": True, "patch_state_completed": True, "notice": "已确认本轮变量无变化；未执行变量补丁或脚本回调。"}),
    "revise_content": ({"operations": [{"op": "set_character", "field": "description", "value": "新的角色描述。"}],
                        "comment": "用户要求调整描述"}, {
        "saved_to_draft": True, "added_refs": [], "characters": [{"ref": "description", "text": "新的角色描述。"}],
        "entries": [], "removed_refs": []}),
    "read_character": ({"ref": "description"}, {"ref": "description", "exists": True, "text": "角色描述。"}),
    "set_retrieval_query": ({"query": ""}, {"saved_to_draft": True}),
}


def encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def descriptions():
    parts = [DESCRIPTION_MARKER, "",
             "以下为源码中的工具级英文 description 全文，未删节；参数字段定义见前文和完整工具协议。示例相互独立，使用同一虚构场景、默认返回选项；剧情规划示例已开启功能，变量示例已获本轮授权，设定写入与笔记修改示例的目标当前全文已在上下文中。",
             ""]
    assert set(EXAMPLES) == set(SPECS)
    for name, (_, description) in SPECS.items():
        arguments, result = EXAMPLES[name]
        parts.extend([f"### `{name}`", "", "```text", description, "```", "",
                      "请求：", "", "```json", encoded(arguments), "```", "",
                      "成功返回：", "", "```json", encoded(success(result)), "```", ""])
    parts.extend(["### 运行时追加的描述", "",
        "以下文本以一个空格追加在工具描述末尾；占位符发送前替换为配置值。", "",
        "| 工具 | 追加文本全文 | 条件 |", "|---|---|---|",
        "| `write_handoff` | `content limit: {handoff_tokens} tokens.` | 当前交接笔记上限。 |",
        "| `new_context` | `handoff limit: {handoff_tokens} tokens.` | 新窗口交接笔记上限。 |",
        "| `write_note` | `content limit: {note_tokens} tokens.` | 当前单条笔记上限。 |",
        "| `write_plot` | `content limit: {plot_tokens} tokens.` | 当前剧情规划上限。 |",
        "| `recall_lore` | `Uses {channel_names} recall.` | 提供检索设置时追加；通道依次去重，映射为 keyword、BM25、semantic；多项使用逗号及最后的 ` and ` 连接。 |",
        "| `recall_lore` | `Reranking orders and filters candidates by relevance.` | 开启 reranker 时追加在上一句之后。 |", "",
        "其余工具无动态追加描述。工具是否下发由会话开关及设定授权决定；变量授权在执行时检查。", ""])
    return "\n".join(parts)


def errors():
    parts = [ERROR_MARKER, "", "`retryable=true` 表示修正原因后可再次调用，并非原样重试。", "",
             "| code | 可修正重试 | 默认 next_action |", "|---|---|---|"]
    parts.extend(f"| `{code}` | {'是' if retry else '否'} | {action} |"
                 for code, (retry, action) in RECOVERY.items())
    return "\n".join(parts) + "\n"


def generated_documents():
    for path, marker, render in ((SUMMARY, DESCRIPTION_MARKER, descriptions), (REFERENCE, ERROR_MARKER, errors)):
        source = path.read_text(encoding="utf-8")
        head = source.split(marker, 1)[0].rstrip()
        yield path, head + "\n\n" + render()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Fail if committed documentation differs from source.")
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
