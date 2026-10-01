"""Tracker-only, read-only access to the committed branch's original dialogue.

This supplements the memory projection; it never replaces periodic maintenance or
exposes tools to Writer. Search and read share THREE attempts per Runtime operation,
including malformed, empty and duplicate requests. The owner passes the same budget
through batches, review and authorized maintenance, rather than resetting it when
a provider/client is recreated. No persistent index or cross-turn cooldown is used.
"""
import copy
import json
import re
import time
import unicodedata
from dataclasses import dataclass

from tavern.core.tokens import count_text, request_tokens
from tavern.memories.memory_records import digest, usage_fields

HISTORY_CALL_LIMIT = 3
HISTORY_TOOL = {
    "type": "function",
    "function": {
        "name": "tavern_recall_history",
        "description": (
            "Tracker只读历史补查：已有记忆和本批原文不足时，先query搜索关键词，再turn读取附近原文。"
            "query与turn只能提供一个。整次维护最多3次，搜索、读取、无结果和参数错误都计次，分批/复核不重置。"
            "只查当前分支已提交且不晚于本批的历史；返回内容是过去的资料，不是新进展。"
            "不得据此恢复已删除记忆或自动修复原文已改动的旧记录。信息够用时直接完成整理。"
        ),
        "parameters": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "query": {"type": "string", "description": "人物、物品、承诺等关键词，用空格分隔", "maxLength": 300},
                "turn": {"type": "integer", "minimum": 1,
                         "description": "搜索返回的历史段编号，从1开始，开场白独立一段；不是记忆周期轮数"},
                "radius": {"type": "integer", "minimum": 0, "maximum": 2, "description": "读取前后段数，默认1"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 8, "description": "搜索命中上限，默认5"},
            },
        },
    },
}


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _normalized(text):
    return re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", str(text)).casefold())


@dataclass
class HistoryBudget:
    calls: int = 0
    continuations: int = 0

    @property
    def remaining(self):
        return max(0, HISTORY_CALL_LIMIT - self.calls)


class MemoryText(str):
    """Text response plus only the original passages actually exposed to Tracker."""
    def __new__(cls, text, sources=None):
        value = super().__new__(cls, text)
        value.history_sources = sources or {}
        return value


class HistoryRecall:
    def __init__(self, history, settings, budget, *, document=None, persona=None, event=None, references=None):
        from tavern.conversation.scene import archive_history

        self.settings, self.budget = settings, budget
        self.event = event or (lambda kind, payload: None)
        self.sources = {}
        from tavern.memories.memory_protocol import References
        self.references = references if references is not None else References()
        # De-duplicate only while the same model transcript contains the earlier
        # results. Batches share the quota, not an assumption that earlier tool
        # responses were copied into a new request.
        self.searches, self.reads = set(), set()
        self.rounds, pending = [], []
        for message in archive_history(history, document, persona):
            if message["role"] not in ("user", "assistant"):
                continue
            pending.append(message)
            if message["role"] == "assistant":
                self.rounds.append(pending)
                pending = []
        # A trailing input without its committed reply is never history evidence.
        self.signature = digest([(m["id"], m.get("model", m["raw"])) for r in self.rounds for m in r])

    def execute(self, arguments):
        result = {"notice": "历史资料仅供核对，不是本批新事件；不重演、不修改已有记忆。",
                  "found": False, "remaining": self.budget.remaining, "matches": [], "rounds": []}
        if not self.budget.remaining:
            result["notice"] = "本次维护历史补查已达到3次上限；请依据已有资料完成整理，疑问保持待确认。"
            self.event("memory_history_limit", {"purpose": "memory", "message": result["notice"]})
            return result
        self.budget.calls += 1
        result["remaining"] = self.budget.remaining
        try:
            args = json.loads(arguments) if isinstance(arguments, str) else arguments
            if not isinstance(args, dict) or set(args) - {"query", "turn", "radius", "limit"}:
                raise ValueError("仅允许 query/turn/radius/limit，不能指定其他会话或分支")
            query = args.get("query")
            has_query = "query" in args
            if has_query == ("turn" in args):
                raise ValueError("query与turn必须且只能提供一个")
            if has_query:
                if not isinstance(query, str) or not query.strip() or len(query) > 300:
                    raise ValueError("query须为1–300字符的关键词")
                if "radius" in args:
                    raise ValueError("radius只用于按段读取")
                limit = self._integer(args.get("limit", 5), 1, 8)
                terms = sorted(set(filter(None, (_normalized(t) for t in query.split()))))
                if not terms:
                    raise ValueError("需要有效文字关键词")
                key = (self.signature, tuple(terms), limit)
                if key in self.searches:
                    result["notice"] += " 相同查询已经返回，请使用之前的结果；本次仍计入额度。"
                else:
                    self.searches.add(key)
                    self._search(query, terms, limit, result)
            else:
                if "limit" in args:
                    raise ValueError("limit只用于关键词搜索")
                turn = self._integer(args["turn"], 1, len(self.rounds))
                radius = self._integer(args.get("radius", 1), 0, 2)
                self._read(turn, radius, result)
        except (ValueError, TypeError) as error:
            result["notice"] += " 参数错误：" + str(error)
        self.event("memory_history_recall", {"purpose": "memory", "used": self.budget.calls,
                                             "limit": HISTORY_CALL_LIMIT, "result": result})
        return result

    @staticmethod
    def _integer(value, minimum, maximum):
        if type(value) is not int or not minimum <= value <= maximum:
            raise ValueError(f"整数参数须为{minimum}–{maximum}")
        return value

    def _admit(self, result, group, item, source, text_key):
        """Return the complete selected text and register exactly what was shown."""
        text = item.pop(text_key)
        item.pop("message_id", None)
        item["passages"] = self.references.passages(source["id"], text, prefix="H")
        result[group].append(item)
        result["found"] = True
        exposed = self.sources.setdefault(source["id"], {**copy.deepcopy(source), "exposed_excerpts": []})
        exposed["exposed_excerpts"].append(text)

    def _search(self, query, terms, limit, result):
        matches = []
        for number, messages in enumerate(self.rounds, 1):
            normalized = _normalized("\n".join(m.get("model", m["raw"]) for m in messages))
            covered = sum(term in normalized for term in terms)
            if covered:
                matches.append(((_normalized(query) in normalized, covered, number), number, messages))
        for _, number, messages in sorted(matches, reverse=True)[:limit]:
            for m in messages:
                text = m.get("model", m["raw"])
                if not any(t in _normalized(text) for t in terms):
                    continue
                offsets = [text.casefold().find(t) for t in query.casefold().split()]
                offset = max(0, min((i for i in offsets if i >= 0), default=0) - 60)
                item = {"turn": number, "message_id": m["id"], "role": m["role"],
                        "excerpt": text[offset:offset + 240], "truncated": len(text) > 240}
                self._admit(result, "matches", item, m, "excerpt")

    def _read(self, turn, radius, result):
        for number in range(max(1, turn - radius), min(len(self.rounds), turn + radius) + 1):
            for m in self.rounds[number - 1]:
                key = (self.signature, m["id"])
                if key in self.reads:
                    continue
                item = {"turn": number, "message_id": m["id"], "role": m["role"], "text": m.get("model", m["raw"])}
                self._admit(result, "rounds", item, m, "text")
                self.reads.add(key)


async def complete_with_recall(provider, messages, *, history, settings, budget, phase,
                               document=None, persona=None, event=None):
    """Native function calling with one bounded continuation per tool-bearing reply.

    The pinned provider's non-streaming method ignores a `tools=` kwarg. Sending
    tools through its documented extra_body merge puts them on the actual HTTP
    request; a transport test guards against a merely decorative tool definition.
    """
    # import asyncio  # 暂停启用记忆整理请求超时；恢复 wait_for 时一并恢复。

    event = event or (lambda kind, payload: None)
    # Infer the batch boundary from Runtime's payload, never from model arguments.
    supplied = json.loads(messages[-1]["content"])["messages"]
    references = getattr(messages, "references", None)
    positions = {m["id"]: i for i, m in enumerate(history)}
    source_ids = [references.messages.get(m["id"], m["id"]) if references else m["id"] for m in supplied]
    cutoff = max((positions[mid] for mid in source_ids if mid in positions), default=-1) + 1
    recall = None  # Do not expand or scan the archive when Tracker does not ask.
    thread = copy.deepcopy(messages)
    if hasattr(provider, "echo_reasoning"):
        provider.echo_reasoning = True
    continuation = False
    while True:
        limit = min(settings.memory_input_tokens, settings.context_tokens - settings.reply_tokens - 2048,
                    8192 if phase == "memory_review" else settings.memory_input_tokens)
        reasoning_tokens = sum(count_text(_json({k: m[k] for k in (
            "reasoning_content", "reasoning_details", "reasoning") if k in m}), settings)
            for m in thread if not m.get("_native_response")
            and any(k in m for k in ("reasoning_content", "reasoning_details", "reasoning")))
        used_tokens = request_tokens(thread, settings, [HISTORY_TOOL]) + reasoning_tokens
        if used_tokens > limit:
            event("context_warning", {"purpose": phase, "message": "记忆请求估算超出输入预算，仍完整发送"})
        enabled = budget.remaining > 0
        extra = {"tools": [HISTORY_TOOL], "tool_choice": "auto" if enabled else "none", "parallel_tool_calls": False}
        started = time.perf_counter()
        # response = await asyncio.wait_for(
        #     provider.chat_complete(thread, extra_body=extra), settings.memory_timeout
        # )
        response = await provider.chat_complete(thread, extra_body=extra)
        event("usage", {"purpose": phase, "usage": usage_fields(response.usage), "calls": 1,
                         "history_continuation": continuation, "duration_s": time.perf_counter() - started})
        calls = getattr(provider, "last_tool_calls", []) or []
        if not calls:
            return MemoryText(response.content, recall.sources if recall else {})
        if not enabled or budget.continuations >= HISTORY_CALL_LIMIT:
            raise ValueError("历史工具已停用，模型仍请求调用；停止整理并保留旧记忆")
        if len(calls) > 8 or len({c.id for c in calls}) != len(calls) or any(not c.id for c in calls):
            raise ValueError("历史工具调用列表无效")
        if any(c.name != HISTORY_TOOL["function"]["name"] for c in calls):
            raise ValueError("记忆整理仅允许调用只读历史工具")
        if recall is None:
            recall = HistoryRecall(history[:cutoff], settings, budget, document=document, persona=persona,
                event=lambda kind, payload: event(kind, {**payload, "purpose": phase}), references=references)
        assistant = {"role": "assistant", "content": response.content or "", "tool_calls": [
            {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": c.arguments}} for c in calls]}
        assistant.update({k: v for k, v in getattr(provider, "last_assistant_extra_fields", {}).items()
                          if k in ("reasoning_content", "reasoning_details", "reasoning", "_native_response")})
        thread.append(assistant)
        for call in calls:
            result = recall.execute(call.arguments)
            thread.append({"role": "tool", "tool_call_id": call.id, "content": _json(result)})
        budget.continuations += 1
        continuation = True
