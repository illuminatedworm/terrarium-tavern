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

from tavern.core.history_refs import message_positions, message_ref, parse_message_ref, span_ref
from tavern.core.tokens import count_text, request_tokens
from tavern.memories.memory_protocol import References, split_passages
from tavern.memories.memory_records import digest, usage_fields

HISTORY_CALL_LIMIT = 3
HISTORY_TOOL = {
    "type": "function",
    "function": {
        "name": "tavern_recall_history",
        "description": (
            "Tracker只读历史补查：已有记忆和本批原文不足时，先query搜索关键词，再用ref读取附近完整交流。"
            "query与ref只能提供一个。整次维护最多3次，搜索、读取、无结果和参数错误都计次，分批/复核不重置。"
            "只查当前分支已提交且不晚于本批的历史；返回内容是过去的资料，不是新进展。"
            "不得据此恢复已删除记忆或自动修复原文已改动的旧记录。信息够用时直接完成整理。"
        ),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "query": {
                    "type": "string",
                    "description": "人物、物品、承诺等关键词，用空格分隔",
                    "maxLength": 300,
                },
                "ref": {
                    "type": "string",
                    "pattern": r"^H[1-9]\d*$",
                    "description": "要读取的消息编号，如搜索结果中的H52（当前分支第52条消息，开场白为H1）。按消息读取时必填；只能读取不晚于本批且已有回复的历史消息。",
                },
                "radius": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 2,
                    "default": 1,
                    "description": "按消息读取时，以ref所在的完整交流为中心，前、后各额外读取多少个完整交流，0–2（含两端），默认1；0只读中心交流，开场白单独算一个交流，到历史边界时不超出可用交流。",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 8,
                    "default": 5,
                    "description": "关键词搜索最多命中的完整交流数，1–8（含两端），默认5；不是消息条数。命中交流中每条含关键词的消息都会返回其包含命中处的完整片段。",
                },
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
    def __init__(
        self, history, settings, budget, *, document=None, persona=None, event=None, references=None
    ):
        from tavern.conversation.scene import archive_history

        self.settings, self.budget = settings, budget
        self.event = event or (lambda kind, payload: None)
        self.sources = {}
        self.references = references if references is not None else References(history)
        # De-duplicate only while the same model transcript contains the earlier
        # results. Batches share the quota, not an assumption that earlier tool
        # responses were copied into a new request.
        self.searches, self.reads = set(), set()
        self.rounds, self.round_of, pending = [], {}, []
        positions = message_positions(history)
        for message in archive_history(history, document, persona):
            if message["role"] not in ("user", "assistant"):
                continue
            pending.append(message)
            if message["role"] == "assistant":
                self.round_of.update({positions[m["id"]]: len(self.rounds) for m in pending})
                self.rounds.append(pending)
                pending = []
        # A trailing input without its committed reply is never history evidence.
        self.signature = digest([(m["id"], m.get("model", m["raw"])) for r in self.rounds for m in r])

    def execute(self, arguments):
        result = {
            "notice": "历史资料仅供核对，不是本批新事件；记忆变更仍须本批依据。",
            "found": False,
            "remaining": self.budget.remaining,
            "matches": [],
            "rounds": [],
        }
        if not self.budget.remaining:
            result["notice"] = "本次维护历史补查已达到3次上限；请依据已有资料完成整理，疑问保持待确认。"
            result["error"] = result["notice"]
            self.event("memory_history_limit", {"purpose": "memory", "message": result["notice"]})
            return result
        self.budget.calls += 1
        result["remaining"] = self.budget.remaining
        try:
            args = json.loads(arguments) if isinstance(arguments, str) else arguments
            if not isinstance(args, dict) or set(args) - {"query", "ref", "radius", "limit"}:
                raise ValueError("仅允许 query/ref/radius/limit，不能指定其他会话或分支")
            query = args.get("query")
            has_query = "query" in args
            if has_query == ("ref" in args):
                raise ValueError("query与ref必须且只能提供一个")
            if has_query:
                if not isinstance(query, str) or not query.strip() or len(query) > 300:
                    raise ValueError("query须为1–300字符的关键词")
                if "radius" in args:
                    raise ValueError("radius只用于按消息读取")
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
                center = self.round_of.get(parse_message_ref(args["ref"]))
                if center is None:
                    raise ValueError(
                        f"ref须为可查历史 {span_ref(1, max(self.round_of))} 中的消息编号"
                        if self.round_of
                        else "当前没有可查历史"
                    )
                radius = self._integer(args.get("radius", 1), 0, 2)
                self._read(center, radius, result)
        except (ValueError, TypeError) as error:
            result["notice"] += " 参数错误：" + str(error)
            result["error"] = str(error)
        self.event(
            "memory_history_recall",
            {"purpose": "memory", "used": self.budget.calls, "limit": HISTORY_CALL_LIMIT, "result": result},
        )
        return result

    @staticmethod
    def _integer(value, minimum, maximum):
        if type(value) is not int or not minimum <= value <= maximum:
            raise ValueError(f"整数参数须为{minimum}–{maximum}")
        return value

    def _admit(self, result, group, message, parts, **flags):
        """Return complete selected passages and register exactly what was shown."""
        item = {
            "ref": message_ref(self.references.positions[message["id"]]),
            "role": message["role"],
            **flags,
            "passages": self.references.passages(message, parts),
        }
        result[group].append(item)
        result["found"] = True
        exposed = self.sources.setdefault(message["id"], {**copy.deepcopy(message), "exposed_passages": []})
        exposed["exposed_passages"] = sorted({*exposed["exposed_passages"], *parts})

    def _search(self, query, terms, limit, result):
        matches = []
        for number, messages in enumerate(self.rounds):
            normalized = [_normalized(m.get("model", m["raw"])) for m in messages]
            covered = sum(any(term in text for text in normalized) for term in terms)
            if not covered:
                continue
            passages = []
            for m in messages:
                parts = list(split_passages(m.get("model", m["raw"])))
                hits = [i for i, text in enumerate(parts, 1) if any(t in _normalized(text) for t in terms)]
                # Normalization drops the punctuation passages split at, so a
                # term can straddle a boundary; then show both neighbours.
                hits = hits or sorted(
                    {
                        part
                        for i in range(1, len(parts))
                        if any(t in _normalized(parts[i - 1] + parts[i]) for t in terms)
                        for part in (i, i + 1)
                    }
                )
                if hits:
                    passages.append((m, hits, len(hits) < len(parts)))
            # Only exchanges with actual message passages consume the limit.
            # Joining messages first can invent a keyword across the speaker boundary.
            if passages:
                rank = (any(_normalized(query) in text for text in normalized), covered, number)
                matches.append((rank, passages))
        for _, passages in sorted(matches, key=lambda match: match[0], reverse=True)[:limit]:
            for message, hits, partial in passages:
                self._admit(result, "matches", message, hits, **({"partial": True} if partial else {}))
        if any(item.get("partial") for item in result["matches"]):
            result["notice"] += (
                " partial=true 表示只返回该消息中含命中处的完整片段；需要上下文时用 ref 读取完整交流。"
            )

    def _read(self, center, radius, result):
        for number in range(max(0, center - radius), min(len(self.rounds), center + radius + 1)):
            for m in self.rounds[number]:
                key = (self.signature, m["id"])
                if key in self.reads:
                    continue
                parts = range(1, len(list(split_passages(m.get("model", m["raw"])))) + 1)
                self._admit(result, "rounds", m, parts)
                self.reads.add(key)


async def complete_with_recall(
    provider, messages, *, history, settings, budget, phase, document=None, persona=None, event=None
):
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
    cutoff = max((parse_message_ref(m["ref"]) for m in supplied), default=0)
    recall = None  # Do not expand or scan the archive when Tracker does not ask.
    thread = copy.deepcopy(messages)
    if hasattr(provider, "echo_reasoning"):
        provider.echo_reasoning = True
    continuation = False
    while True:
        limit = min(
            settings.memory_input_tokens,
            settings.context_tokens - settings.reply_tokens - 2048,
            8192 if phase == "memory_review" else settings.memory_input_tokens,
        )
        reasoning_tokens = sum(
            count_text(
                _json({k: m[k] for k in ("reasoning_content", "reasoning_details", "reasoning") if k in m}),
                settings,
            )
            for m in thread
            if not m.get("_native_response")
            and any(k in m for k in ("reasoning_content", "reasoning_details", "reasoning"))
        )
        used_tokens = request_tokens(thread, settings, [HISTORY_TOOL]) + reasoning_tokens
        if used_tokens > limit:
            event("context_warning", {"purpose": phase, "message": "记忆请求估算超出输入预算，仍完整发送"})
        enabled = budget.remaining > 0
        extra = {
            "tools": [HISTORY_TOOL],
            "tool_choice": "auto" if enabled else "none",
            "parallel_tool_calls": False,
        }
        started = time.perf_counter()
        # response = await asyncio.wait_for(
        #     provider.chat_complete(thread, extra_body=extra), settings.memory_timeout
        # )
        response = await provider.chat_complete(thread, extra_body=extra)
        event(
            "usage",
            {
                "purpose": phase,
                "usage": usage_fields(response.usage),
                "calls": 1,
                "history_continuation": continuation,
                "duration_s": time.perf_counter() - started,
            },
        )
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
            recall = HistoryRecall(
                history[:cutoff],
                settings,
                budget,
                document=document,
                persona=persona,
                event=lambda kind, payload: event(kind, {**payload, "purpose": phase}),
                references=references,
            )
        assistant = {
            "role": "assistant",
            "content": response.content or "",
            "tool_calls": [
                {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": c.arguments}}
                for c in calls
            ],
        }
        assistant.update(
            {
                k: v
                for k, v in getattr(provider, "last_assistant_extra_fields", {}).items()
                if k in ("reasoning_content", "reasoning_details", "reasoning", "_native_response")
            }
        )
        thread.append(assistant)
        for call in calls:
            result = recall.execute(call.arguments)
            message = {"role": "tool", "tool_call_id": call.id, "content": _json(result)}
            if "error" in result:
                message["_tool_error"] = True
            thread.append(message)
        budget.continuations += 1
        continuation = True
