"""A small resumable read-only tool loop, with fixed per-investigator compaction."""

import asyncio
import copy
import time

from pydantic import ValidationError

from tavern.conversation.compaction import checkpoint_items, opaque_message
from tavern.core.context_usage import context_overflow, context_usage, exceeds
from tavern.core.message_adapter import provider_messages
from tavern.core.model_view import HANDLE, ModelView
from tavern.core.usage import observed_usage
from tavern.novel.config import COMPACT_INSTRUCTION, SYSTEM
from tavern.novel.source import NovelText
from tavern.novel.tools import WORKER_SPECS, definitions
from tavern.scripting.native_response import completion_message, message_digest
from tavern.scripting.request_context import configure_operation, response_origin
from tavern.storage.store import dump

SUMMARY_PREFIX = "此前调查过程的摘要：\n"


class NovelWorker:
    def __init__(self, service, job, cfg, request_context):
        self.service, self.data, self.job, self.cfg = service, service.data, job, cfg
        self.request_context = request_context
        self.source = self.data.source(job["source"])
        self.book = NovelText(service.store.root, self.source)
        self.trace = [r["message"] for r in self.data.trace(job["id"])]
        self.definitions = definitions(WORKER_SPECS)
        self.tick = time.monotonic()
        self.header = [
            {"role": "system", "content": SYSTEM},
            {
                "role": "user",
                "content": dump(
                    {
                        "source": "N1",
                        "name": self.source["name"],
                        "total_characters": self.source["characters"],
                        "question": job["task"],
                        "background": job["background"],
                        "budget": {
                            "requests": job["config"]["max_requests"],
                            "seconds": job["config"]["timeout_seconds"],
                        },
                    }
                ),
            },
        ]

        self.view = ModelView()
        self.view.reverse = job["progress"].setdefault("model_references", {})
        self.view.forward = {raw: ref for ref, raw in self.view.reverse.items()}
        self.view.reserved = set(job["progress"].get("reserved_references", [])) | set(
            HANDLE.findall(str(self.header))
        )
        self.view.reserved.difference_update(self.view.reverse)
        self.header = self.view.messages(self.header)

    def account_time(self):
        now = time.monotonic()
        self.job["elapsed"] += now - self.tick
        self.tick = now

    def save(self, *, message=None, mail=None):
        self.account_time()
        self.job["progress"]["reserved_references"] = sorted(self.view.reserved)
        self.job["progress"]["routing"] = self.request_context.saved().get("routing", {})
        if not self.data.save(self.job, message=message, mail=mail):
            raise asyncio.CancelledError
        if message is not None:
            self.trace.append(message)
        self.service.notify()

    def messages(self):
        context = self.job["progress"].get("context", {})
        if context.get("checkpoint") and context.get("origin") != response_origin(self.cfg):
            # Rebuild from retained originals when a connection's opaque state is no longer usable.
            context = {}
            self.job["progress"].pop("context", None)
        prefix = []
        if context.get("checkpoint"):
            prefix = [opaque_message(context["checkpoint"], self.cfg)]
        elif context.get("summary"):
            prefix = [{"role": "system", "content": SUMMARY_PREFIX + context["summary"]}]
        body = [m for m in self.trace[context.get("through", 0) :] if not m.get("_novel_compaction")]
        return self.view.messages([*self.header, *prefix, *body])

    def slot(self):
        self.account_time()
        if self.job["requests"] >= self.job["config"]["max_requests"]:
            raise ValueError("调查者模型请求额度已耗尽；保留部分过程，追问和恢复不会重置额度")
        if self.job["elapsed"] >= self.job["config"]["timeout_seconds"]:
            raise ValueError("调查者累计运行时限已耗尽")
        self.job["requests"] += 1
        self.save()  # Reserve before network IO; a restart cannot refund a potentially billed request.

    async def request(self, provider, messages, *, compact=False):
        self.slot()
        provider.operation_purpose = "novel_maintenance" if compact else "novel_research"
        cloud = compact and self.job["config"]["channel"] == "chatgpt"
        response = await provider.chat_complete(
            provider_messages(provider, messages, self.cfg),
            extra_body={"tools": self.definitions, "tool_choice": "none"}
            if compact
            else {"tools": self.definitions, "tool_choice": "auto", "parallel_tool_calls": False},
            **(
                {
                    "_compact_threshold": 1024,
                    "_maintenance_instructions": "只压缩已完成的原著调查交流，不执行工具，不写故事，返回 OK。",
                }
                if cloud
                else {}
            ),
        )
        usage = getattr(provider, "last_usage", None) or getattr(response, "usage", None) or {}
        measured = observed_usage(usage, self.cfg.api_mode)
        for key in ("tokens", "output_tokens", "thinking_tokens", "cached_input_tokens"):
            value = measured.get(key)
            if value is not None:
                self.job["usage"][key] = self.job["usage"].get(key, 0) + value
        self.job["progress"]["latest_usage"] = measured
        self.save()
        completion = getattr(provider, "last_completion", None)
        if completion and completion.action == "incomplete":
            raise ValueError(completion_message("原著调查响应未完整结束，进度保留", completion))
        if getattr(response, "finish_reason", None) in {"length", "content_filter"}:
            raise ValueError("原著调查响应被截断或过滤，未保存为报告")
        return response

    async def compact(self, provider):
        context = self.job["progress"].get("context", {})
        start, boundaries, pending = context.get("through", 0), [], set()
        for index, message in enumerate(self.trace[start:], start):
            if message.get("_novel_compaction"):
                continue
            if message.get("tool_calls"):
                pending.update(c["id"] for c in message["tool_calls"])
            if message["role"] == "tool":
                pending.discard(message["tool_call_id"])
            if not pending and message["role"] in {"assistant", "tool"}:
                boundaries.append(index + 1)
        if len(boundaries) <= 2:
            return False
        # The dedicated research contract permits compacting completed tool exchanges,
        # while keeping the two latest exchanges and every unpaired call verbatim.
        end = boundaries[-3]
        previous = []
        if context.get("checkpoint"):
            previous = [opaque_message(context["checkpoint"], self.cfg)]
        elif context.get("summary"):
            previous = [{"role": "system", "content": SUMMARY_PREFIX + context["summary"]}]
        cloud = self.job["config"]["channel"] == "chatgpt"
        messages = [
            {"role": "system", "content": COMPACT_INSTRUCTION},
            *previous,
            *[m for m in self.trace[start:end] if not m.get("_novel_compaction")],
            # Ending on a tool exchange invites a standard model to keep investigating. Cloud
            # compaction takes separate instructions and would fold a trailing request into its state.
            *([] if cloud else [{"role": "user", "content": "现在只输出以上调查过程的摘要，不要调用工具。"}]),
        ]
        response = await self.request(provider, self.view.messages(messages), compact=True)
        if getattr(provider, "last_tool_calls", []):
            raise ValueError("调查压缩返回了工具调用，未执行；原文保留")
        checkpoint, summary = [], ""
        if cloud:
            checkpoint = checkpoint_items(
                (getattr(provider, "last_native_response", None) or {}).get("items", [])
            )
            if not checkpoint:
                raise ValueError("服务端未返回原著调查云端压缩状态；原文保留")
            checkpoint = [item for item in checkpoint if item.get("type") != "message"]
        else:
            summary = getattr(response, "text", None) or getattr(response, "content", "")
            if not isinstance(summary, str) or not summary.strip():
                raise ValueError("原著调查压缩没有返回有效摘要；原文保留")
        self.job["progress"]["context"] = {
            "through": end,
            "summary": summary,
            "checkpoint": checkpoint,
            "origin": response_origin(self.cfg),
        }
        self.save(
            message={
                "role": "system",
                "content": summary or "已完成云端压缩；完整阅读过程仍可回查。",
                "_novel_compaction": True,
                "through": end,
            }
        )
        return True

    async def execute_pending(self):
        pending = self.job["progress"].get("pending")
        if not pending:
            return
        returned = set(self.job["progress"].get("returned", []))
        for call in pending["tool_calls"]:
            if call["id"] in returned:
                continue
            name, arguments = call["function"]["name"], call["function"]["arguments"]
            mail = None
            try:
                if name not in WORKER_SPECS:
                    raise ValueError("没有此工具；只能使用当前提供的原著读取或询问工具")
                args = WORKER_SPECS[name][0].model_validate_json(arguments)
                if name == "ask_delegator":
                    if len(pending["tool_calls"]) != 1:
                        raise ValueError("ask_delegator 必须单独调用")
                    mail = {
                        "kind": "question",
                        "content": self.view.restore(args.question),
                        "operation": call["id"],
                    }
                    result = {"status": "waiting_for_delegator"}
                    self.job["status"] = "waiting"
                    self.job["progress"]["waiting_for_answer"] = True
                elif name == "read_novel":
                    result = await asyncio.to_thread(self.book.read, args.start, args.length)
                elif name == "search_novel":
                    result = await asyncio.to_thread(
                        self.book.search, self.view.restore(args.query), args.start, args.limit, args.end
                    )
                else:
                    result = await asyncio.to_thread(self.book.outline, args.start, args.limit)
            except ValidationError as error:
                result = {"error": error.errors(include_input=False, include_url=False)}
            except ValueError as error:
                result = {"error": str(error)}
            returned.add(call["id"])
            self.job["progress"]["returned"] = sorted(returned)
            self.account_time()
            result["budget"] = {
                "requests_remaining": max(0, self.job["config"]["max_requests"] - self.job["requests"]),
                "seconds_remaining": max(
                    0, round(self.job["config"]["timeout_seconds"] - self.job["elapsed"])
                ),
            }
            self.view.reserved.update(set(HANDLE.findall(str(result))) - self.view.reverse.keys())
            result = self.view.expose(result)
            self.save(
                message={"role": "tool", "tool_call_id": call["id"], "content": dump(result)}, mail=mail
            )
        self.job["progress"].pop("pending", None)
        self.job["progress"].pop("returned", None)
        self.save()

    async def run(self, provider):
        configure_operation(provider, self.request_context, lambda *_: None, "novel_research")
        await self.execute_pending()
        while True:
            incoming = self.data.receive_worker(self.job)
            self.trace.extend(incoming)
            if self.job["progress"].get("reported"):
                self.job["status"] = "completed"
                self.save()
                return
            if self.job["progress"].get("waiting_for_answer") and not incoming:
                self.job["status"] = "waiting"
                self.save()
                return
            self.job["status"] = "running"
            messages = self.messages()
            usage = context_usage(messages, self.cfg, self.definitions, purpose="novel_research")
            if exceeds(usage, self.cfg.context_tokens * 0.85):
                await self.compact(provider)
                messages = self.messages()
            try:
                response = await self.request(provider, messages)
            except Exception as error:
                # Only an explicit context-size rejection authorizes this one compaction retry.
                if not context_overflow(error) or not await self.compact(provider):
                    raise
                response = await self.request(provider, self.messages())
            calls = getattr(provider, "last_tool_calls", []) or []
            used = {call["id"] for message in self.trace for call in message.get("tool_calls", [])}
            if (
                len(calls) > 32
                or len({c.id for c in calls}) != len(calls)
                or any(not c.id or c.id in used for c in calls)
            ):
                raise ValueError("模型返回无效或重复工具调用，进度保留")
            text = getattr(response, "text", None) or getattr(response, "content", "")
            assistant = {"role": "assistant", "content": text}
            if calls:
                assistant["tool_calls"] = [
                    {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": c.arguments}}
                    for c in calls
                ]
            assistant.update(copy.deepcopy(getattr(provider, "last_assistant_extra_fields", {}) or {}))
            if native := getattr(provider, "last_native_response", None):
                assistant["_native_response"] = copy.deepcopy(native)
            assistant["_response_origin"] = response_origin(self.cfg)
            assistant["_response_digest"] = message_digest(assistant)
            if calls:
                self.job["progress"]["pending"] = assistant
                self.save(message=assistant)
                await self.execute_pending()
                if self.job["status"] == "waiting":
                    return
                continue
            completion = getattr(provider, "last_completion", None)
            if completion and completion.action != "end":
                self.save(message=assistant)
                continue
            if not isinstance(text, str) or not text.strip():
                raise ValueError("调查者未返回非空报告，进度保留")
            self.job["status"] = "completed"
            self.job["progress"]["reported"] = True
            self.save(
                message=assistant,
                mail={
                    "kind": "report",
                    "content": self.view.restore(text),
                    "operation": f"report-{self.job['requests']}",
                },
            )
            # A message arriving during the final request starts another exchange,
            # never changes that already-completed response or loses the queued follow-up.
            incoming = self.data.receive_worker(self.job)
            if not incoming:
                return
            self.trace.extend(incoming)
