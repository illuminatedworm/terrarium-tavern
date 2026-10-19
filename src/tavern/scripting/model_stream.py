"""Incremental SSE accumulators; a disconnected stream never becomes a completed turn."""
import copy
import json

from tavern.core.kemini import TOOL_NAME
from tavern.scripting.responses_stream import ResponsesOutput
from tavern.scripting.sse import stream_error


class StreamState:
    def __init__(self, api, *, kemini_transport=False):
        self.api, self.done = api, False
        self.result, self.calls, self.blocks = {}, {}, {}
        self.text, self.reasoning, self.parts = "", "", []
        self.response_output = ResponsesOutput() if api == "responses" else None
        self.kemini_call = None
        self.kemini_transport = kemini_transport

    def feed(self, event):
        api = self.api
        if not isinstance(event, dict):
            raise ValueError("模型流事件格式无效，本回合未提交")
        if "error" in event or event.get("type") == "error":
            raise ValueError(stream_error("模型流返回错误", event))
        if self.done:
            # Chat Completions/Gemini can report usage after the terminal chunk.
            # Content or tool fragments after completion must never become story.
            # Some compatible endpoints append an empty stop after tool_calls.
            # Accept only empty terminal choices; keep the first finish reason
            # so a redundant stop cannot turn a tool response into a plain reply.
            if ((api == "chat_completions" and any(
                    choice.get("delta") != {} or choice.get("index", 0) != 0
                    or choice.get("finish_reason") not in ("stop", self.result["finish_reason"])
                    for choice in event.get("choices") or []))
                    or (api == "gemini" and event.get("candidates"))
                    or (api in ("responses", "anthropic") and event.get("type") != "ping")):
                raise ValueError("模型完成事件之后仍有响应内容，本回合未提交")
            if event.get("type") == "ping":
                return ""
        if api == "responses":
            kind = event.get("type")
            self.response_output.accept(event)
            if kind == "response.output_text.delta":
                return event.get("delta", "")
            if kind == "response.completed":
                self.result, self.done = self.response_output.finish(event["response"]), True
            elif kind in ("response.failed", "response.incomplete", "error"):
                raise ValueError("Responses 流未完整结束")
            return ""
        if api == "chat_completions":
            if event.get("usage"):
                self.result["usage"] = event["usage"]
            self.result["model"] = event.get("model", self.result.get("model"))
            choices = event.get("choices") or []
            if self.done or not choices:
                return ""
            choice = choices[0]
            delta = choice.get("delta") or {}
            self.text += delta.get("content") or ""
            self.reasoning += delta.get("reasoning_content") or ""
            if delta.get("reasoning_details"):
                self.parts.extend(copy.deepcopy(delta["reasoning_details"]))
            for call in delta.get("tool_calls") or []:
                item = self.calls.setdefault(call["index"], {"id": "", "type": "function",
                    "function": {"name": "", "arguments": ""}})
                if call.get("id"):
                    item["id"] = call["id"]
                for field in ("name", "arguments"):
                    item["function"][field] += call.get("function", {}).get(field) or ""
            if choice.get("finish_reason"):
                if choice["finish_reason"] not in ("stop", "tool_calls", "length", "content_filter"):
                    raise ValueError("模型流结束原因无效，本回合未提交")
                self.done = True
                self.result["finish_reason"] = choice["finish_reason"]
            return delta.get("content") or ""
        if api == "anthropic":
            kind = event.get("type")
            if kind == "message_start":
                self.result = copy.deepcopy(event["message"])
            elif kind == "content_block_start":
                self.blocks[event["index"]] = copy.deepcopy(event["content_block"])
                return event["content_block"].get("text", "")
            elif kind == "content_block_delta":
                block = self.blocks[event["index"]]
                delta = event["delta"]
                for field in ("text", "thinking", "signature"):
                    if field in delta:
                        block[field] = block.get(field, "") + delta[field]
                if "partial_json" in delta:
                    block["_json"] = block.get("_json", "") + delta["partial_json"]
                return delta.get("text", "")
            elif kind == "message_delta":
                self.result.update(event.get("delta", {}))
                self.result.setdefault("usage", {}).update(event.get("usage", {}))
            elif kind == "message_stop":
                self.done = True
            elif kind == "error":
                raise ValueError(stream_error("Anthropic 流返回错误", event))
            return ""
        if event.get("error"):
            raise ValueError(stream_error("Gemini 流返回错误", event))
        if event.get("usageMetadata"):
            self.result["usageMetadata"] = event["usageMetadata"]
        self.result["modelVersion"] = event.get("modelVersion", self.result.get("modelVersion"))
        candidates = event.get("candidates") or []
        if not candidates:
            return ""
        candidate = candidates[0]
        pieces = candidate.get("content", {}).get("parts", [])
        for piece in copy.deepcopy(pieces):
            call = piece.get("functionCall")
            if self.kemini_transport and call is not None:
                name = call.get("name")
                if name == TOOL_NAME and (call.get("willContinue") or "partialArgs" in call):
                    self.kemini_call = {"name": name, "args": {"content": ""}}
                    if call.get("id"):
                        self.kemini_call["id"] = call["id"]
                    self.parts.append({"functionCall": self.kemini_call})
                elif name:
                    self.kemini_call = None
                if self.kemini_call is not None and (not name or name == TOOL_NAME):
                    for fragment in call.get("partialArgs") or []:
                        if fragment.get("jsonPath") == "$.content":
                            self.kemini_call["args"]["content"] += fragment.get("stringValue", "")
                    if isinstance(call.get("args"), dict):
                        self.kemini_call["args"] = call["args"]
                    continue
            if set(piece) == {"thoughtSignature"} and self.parts:
                self.parts[-1]["thoughtSignature"] = piece["thoughtSignature"]
            else:
                self.parts.append(piece)
        if candidate.get("finishReason"):
            self.done = True
            self.result["finishReason"] = candidate["finishReason"]
        return "".join(p.get("text", "") for p in pieces if not p.get("thought"))

    def finish(self):
        if not self.done:
            raise ValueError("模型流连接提前结束，本回合未提交")
        if self.api == "chat_completions":
            message = {"content": self.text, "tool_calls": [self.calls[i] for i in sorted(self.calls)]}
            if self.reasoning:
                message["reasoning_content"] = self.reasoning
            if self.parts:
                message["reasoning_details"] = self.parts
            self.result["choices"] = [{"message": message, "finish_reason": self.result["finish_reason"]}]
        elif self.api == "anthropic":
            for block in self.blocks.values():
                if "_json" in block:
                    block["input"] = json.loads(block.pop("_json"))
            self.result["content"] = [self.blocks[i] for i in sorted(self.blocks)]
        elif self.api == "gemini":
            self.result["candidates"] = [{"content": {"role": "model", "parts": self.parts},
                                           "finishReason": self.result["finishReason"]}]
        return self.result
