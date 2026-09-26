"""Typed tools over a turn draft; storage identities never become model arguments."""
import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from tavern.character.cards import lore_entries, normalize
from tavern.conversation.context import get_path
from tavern.core.model_view import state_for_model
from tavern.core.tokens import count_text
from tavern.full_agent.state import note_view, rollover, source_hash


class Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Query(Arguments):
    query: str = Field(min_length=1, max_length=1000)


class Search(Query):
    limit: int = Field(default=5, ge=1, le=10)


class HistorySearch(Search):
    scope: Literal["dialogue", "tools", "all"] = "dialogue"


class Read(Arguments):
    ref: str = Field(min_length=1, max_length=80)
    offset: int = Field(default=0, ge=0)
    length: int = Field(default=4000, ge=1, le=12000)


class Checkpoint(Arguments):
    content: str = Field(min_length=1, max_length=64000)


class NewContext(Checkpoint):
    keep_recent: int = Field(default=2, ge=0, le=12)


class Note(Arguments):
    key: str = Field(min_length=1, max_length=80, pattern=r"^[\w\-/ .\u4e00-\u9fff]+$")
    content: str = Field(min_length=1, max_length=64000)
    kind: Literal["fact", "hypothesis", "plan"]
    sources: list[str] = Field(default_factory=list, max_length=20)


class NoteKey(Arguments):
    key: str = Field(min_length=1, max_length=80)


class Patch(Arguments):
    operations: list[dict] = Field(min_length=1, max_length=100)
    reason: str = Field(min_length=1, max_length=1000)


class StateRead(Arguments):
    path: str = Field(default="", max_length=200)
    offset: int = Field(default=0, ge=0)
    length: int = Field(default=4000, ge=1, le=12000)


SPECS = {
    "search_lore": (Query, "自拟 query 调用与自动检索相同的世界书索引、准入和中途注入；主动检索优先。返回已注入条目的 L 引用。"),
    "read_lore": (Read, "按 L 引用分页读取已启用的世界书资料，不重复注入。"),
    "search_history": (HistorySearch, "搜索当前分支的原始正文或历史工具记录。返回 H/T 引用和片段；工具记录是过去资料。"),
    "read_history": (Read, "按 H/T 引用分页读取原始历史，offset 是字符位置；next_offset 非空表示还有原文。"),
    "write_checkpoint": (Checkpoint, "替换随窗口携带的交接笔记，写入本回合草稿；不会换窗。"),
    "new_context": (NewContext, "用 content 保存完整 checkpoint 并主动换窗；保留当前用户输入和最近 keep_recent 条完整交流。必须单独调用。"),
    "search_notes": (Search, "按内容/语义 key 搜索本分支的长期笔记，query=* 列出；笔记不自动注入。"),
    "read_note": (Read, "ref 是笔记 key；分页读取笔记及来源、可信性质和原文编辑警告。"),
    "write_note": (Note, "新增或替换长期笔记。fact 必须引用已读取的 H 原文；hypothesis/plan 明确为推测/计划。不自动分类整理。"),
    "archive_note": (NoteKey, "归档过时或已完成笔记，保留存档和旧分支；不从原文自动恢复。"),
    "read_state": (StateRead, "分页读取剧情状态值，path 可用点分隔选择子字段；隐藏运行时账本。"),
    "patch_state": (Patch, "使用 JSON Patch 更新 stat_data，经 MVU 校验；本轮允许更新变量时可用。"),
    "revise_content": (Patch, "JSON Patch 修改当前分支角色/世界书草稿。遵循授权和审核策略，不修改书架原卡。"),
    "read_character": (Read, "ref 是角色顶层字段名，如 description、extensions；分页读取可逆角色视图。"),
    "set_retrieval_query": (Query, "保存供下一回合自动检索使用的简短剧情线索；不进入正文或上下文。"),
}


def tool_definitions(request, session):
    names = set(SPECS)
    if not request.update_variables:
        names.discard("patch_state")
    if session["policy_effective"] == "directed" and not request.allow_content_edit:
        names.discard("revise_content")
    return [{"type": "function", "function": {"name": name, "description": description,
             "parameters": model.model_json_schema()}} for name, (model, description) in SPECS.items() if name in names]


def encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class AgentTools:
    def __init__(self, owner):
        self.owner = owner
        self.read_sources = {}

    def cap_text(self, content, maximum, label):
        if not content.strip() or count_text(content, self.owner.settings) > maximum:
            raise ValueError(f"{label}不能为空，且须在 {maximum} tokens 内；请自行精简")

    def page(self, text, args, **metadata):
        cap = self.owner.settings.full_agent_config.tool_result_tokens
        if args.offset > len(text):
            raise ValueError("offset 超出原文长度")
        end = min(len(text), args.offset + args.length)
        def result(at):
            return {**metadata, "text": text[args.offset:at], "offset": args.offset,
                    "next_offset": at if at < len(text) else None, "total_chars": len(text)}
        low, high = args.offset, end
        while low < high:
            mid = (low + high + 1) // 2
            if count_text(encoded(result(mid)), self.owner.settings) <= cap:
                low = mid
            else:
                high = mid - 1
        if low == args.offset and low < len(text):
            raise ValueError("工具结果预算不足，请使用更短的读取范围")
        return result(low)

    def archive(self):
        owner = self.owner
        rows = [{"ref": f"H{i+1}", "role": m["role"], "text": m["raw"], "source": m}
                for i, m in enumerate(owner.source_history)]
        positions = {m["id"]: i + 1 for i, m in enumerate(owner.history)}
        for record in owner.trace:
            number = positions[record["message_id"]]
            rows.append(self.trace_row(record["message"], f"T{number}.{record['seq']}"))
        for i, record in enumerate(owner.new_trace, 1):
            rows.append(self.trace_row(record["message"], f"T{len(owner.history)+2}.{i}"))
        return rows

    def trace_row(self, message, ref):
        value = {"role": message["role"], "content": message.get("_archive_content", message.get("content", ""))}
        if message.get("tool_calls"):
            value["calls"] = [c["function"] for c in message["tool_calls"]]
        return {"ref": ref, "role": "tool_record", "text": encoded(value)}

    def search(self, rows, query, limit):
        terms = set(re.findall(r"\w+", query.casefold()))
        def score(row):
            text = (row.get("key", "") + " " + row["text"]).casefold()
            return sum(term in text for term in terms) + 2 * (query.casefold() in text)
        ranked = sorted(((score(row), i, row) for i, row in enumerate(rows)), key=lambda x: (-x[0], -x[1]))
        results = []
        for rank, _, row in ranked:
            if len(results) >= limit or (not rank and query != "*"):
                break
            text = row["text"]
            at = max(0, min((text.casefold().find(t) for t in terms if t in text.casefold()), default=0) - 80)
            item = {k: v for k, v in row.items() if k not in ("source", "text")}
            item.update(preview=text[at:at+400], offset=at)
            if count_text(encoded(results + [item]), self.owner.settings) > self.owner.settings.full_agent_config.tool_result_tokens - 128:
                break
            results.append(item)
            if row.get("source"):
                self.read_sources[row["ref"]] = row["source"]
        return {"matches": results, "notice": "使用 read 工具按 offset 读取完整原文。"}

    async def execute(self, name, arguments):
        owner = self.owner
        allowed = {t["function"]["name"] for t in owner.definitions}
        if name not in allowed:
            raise ValueError("当前模式或授权不允许此工具")
        args = SPECS[name][0].model_validate_json(arguments)
        state = owner.state
        if name == "search_lore":
            return await owner.retrieve(args.query, source="agent")
        if name == "read_lore":
            entries = lore_entries(owner.document, "writer")
            entry_id = owner.lore_refs().get(args.ref)
            entry = next((e for e in entries if e["id"] == entry_id and e["enabled"]), None)
            if not entry:
                raise ValueError("世界书引用不存在或未启用")
            return self.page(owner.expand(entry["content"]), args, ref=args.ref, title=entry.get("comment", ""))
        if name == "search_history":
            rows = [r for r in self.archive() if args.scope == "all" or
                    (r["role"] == "tool_record") == (args.scope == "tools")]
            return self.search(rows, args.query, args.limit)
        if name == "read_history":
            row = next((r for r in self.archive() if r["ref"] == args.ref), None)
            if row is None:
                raise ValueError("当前分支没有此历史引用")
            result = self.page(row["text"], args, ref=args.ref, role=row["role"])
            if row.get("source"):
                self.read_sources[args.ref] = row["source"]
            return result
        if name in ("write_checkpoint", "new_context"):
            self.cap_text(args.content, owner.settings.full_agent_config.checkpoint_tokens, "checkpoint")
            if name == "new_context":
                rollover(state, owner.history, args.content, args.keep_recent)
                owner.rolled = True
            else:
                state["checkpoint"] = {"text": args.content, "revision": state["checkpoint"]["revision"] + 1, "stale": False}
            state.pop("edited_sources", None)
            return {"saved_to_draft": True, "window": state["window"]["number"],
                    "notice": "正文完成后与本回合统一提交。"}
        if name == "search_notes":
            rows = [{**note_view(n, owner.source_history), "text": n["content"]} for n in state["notes"].values()
                    if n["status"] == "active"]
            for row in rows:
                row.pop("content")
            return self.search(rows, args.query, args.limit)
        if name == "read_note":
            note = state["notes"].get(args.ref)
            if note is None:
                raise ValueError("笔记 key 不存在")
            meta = note_view(note, owner.source_history)
            return self.page(meta.pop("content"), args, **meta)
        if name == "write_note":
            self.cap_text(args.content, owner.settings.full_agent_config.note_tokens, "笔记")
            if args.kind == "fact" and not args.sources:
                raise ValueError("fact 必须提供已读取的 H 原文依据；尚未确立的内容请用 hypothesis/plan")
            sources = []
            for ref in dict.fromkeys(args.sources):
                source = self.read_sources.get(ref)
                if source is None:
                    raise ValueError("sources 只能引用本回合已搜索或读取的 H 原文；先调用历史工具")
                sources.append({"message_id": source["id"], "hash": source_hash(source)})
            old = state["notes"].get(args.key, {})
            state["notes"][args.key] = {"key": args.key, "content": args.content, "kind": args.kind,
                                       "status": "active", "revision": old.get("revision", 0) + 1, "sources": sources}
            return {"key": args.key, "saved_to_draft": True}
        if name == "archive_note":
            if args.key not in state["notes"]:
                raise ValueError("笔记 key 不存在")
            state["notes"][args.key]["status"] = "archived"
            state["notes"][args.key]["revision"] += 1
            return {"key": args.key, "archived": True}
        if name == "read_state":
            result = get_path(state_for_model(owner.snapshot["state"]), owner.model_view.restore(args.path))
            return self.page(encoded(owner.model_view.expose(result)), args, path=args.path)
        if name == "patch_state":
            result = await owner.script.call("process", "", owner.model_view.operations(args.operations), "")
            owner.snapshot["state"], owner.snapshot["scopes"] = result["state"], result["scopes"]
            return {"saved_to_draft": True, "notice": "变量已通过 MVU 校验。"}
        if name == "read_character":
            data = owner.model_view.expose(normalize(owner.document))
            if args.ref not in data:
                raise ValueError("角色字段不存在")
            value = data[args.ref]
            return self.page(value if isinstance(value, str) else encoded(value), args, ref=args.ref)
        if name == "revise_content":
            from tavern.character.card_workflow import validate_document
            revised = owner.model_view.patch(owner.document, args.operations)
            validate_document(revised)
            owner.document = revised
            owner.snapshot["content"] = {"document": revised, "reason": args.reason, "turn": owner.tid}
            return {"saved_to_draft": True}
        if name == "set_retrieval_query":
            owner.retrieval_query = args.query[:owner.settings.retrieval.query_chars]
            return {"saved_to_draft": True}
        raise ValueError("未知工具")
