"""Incremental SSE accumulators; a disconnected stream never becomes a completed turn."""

import copy
import json

from tavern.scripting.http_errors import upstream_message
from tavern.scripting.responses_stream import ResponsesOutput
from tavern.scripting.sse import stream_error


class StreamState:
    def __init__(self, api, *, secret=""):
        self.api, self.completion_seen = api, False
        self.secret = secret
        self.result, self.calls, self.blocks = {}, {}, {}
        self.text, self.reasoning, self.parts = "", "", []
        self.response_output = ResponsesOutput() if api == "responses" else None

    def feed(self, event):
        api = self.api
        if not isinstance(event, dict):
            raise ValueError("模型流事件格式无效，本回合未提交")
        if "error" in event or event.get("type") == "error":
            raise ValueError(stream_error("模型流返回错误", event, secret=self.secret))
        # Completion metadata never closes the reader or seals accumulated
        # content. One response can continue with text, reasoning and tools;
        # only finish(), after HTTP body EOF, interprets the complete output.
        if api == "responses":
            kind = event.get("type")
            self.response_output.accept(event)
            if kind == "response.output_text.delta":
                return event.get("delta", "")
            if kind in ("response.completed", "response.incomplete"):
                self.result = {
                    key: copy.deepcopy(value) for key, value in event["response"].items() if key != "output"
                }
                self.completion_seen = True
            elif kind == "response.failed":
                raise ValueError(stream_error("Responses 流未完整结束", event, secret=self.secret))
            return ""
        if api == "chat_completions":
            if event.get("usage"):
                self.result["usage"] = event["usage"]
            self.result["model"] = event.get("model", self.result.get("model"))
            choices = event.get("choices") or []
            if not choices:
                return ""
            choice = choices[0]
            delta = choice.get("delta") or {}
            self.text += delta.get("content") or ""
            self.reasoning += delta.get("reasoning_content") or ""
            if delta.get("reasoning_details"):
                self.parts.extend(copy.deepcopy(delta["reasoning_details"]))
            for call in delta.get("tool_calls") or []:
                item = self.calls.setdefault(
                    call["index"], {"id": "", "type": "function", "function": {"name": "", "arguments": ""}}
                )
                if call.get("id"):
                    item["id"] = call["id"]
                for field in ("name", "arguments"):
                    item["function"][field] += call.get("function", {}).get(field) or ""
            if choice.get("finish_reason"):
                self.completion_seen = True
                reason, previous = choice["finish_reason"], self.result.get("finish_reason")
                # A plain stop tail cannot cancel a request for tool results.
                # Truncation/filtering/unknown reasons outrank success and stay
                # failures even if a relay later appends a normal stop marker.
                if previous in (None, "stop") or reason not in ("stop", "tool_calls"):
                    self.result["finish_reason"] = reason
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
                for key in ("context_management", "input_transformations", "diagnostics"):
                    if key in event:
                        self.result[key] = copy.deepcopy(event[key])
            elif kind == "message_stop":
                self.completion_seen = True
            return ""
        if event.get("usageMetadata"):
            self.result["usageMetadata"] = event["usageMetadata"]
        if event.get("promptFeedback"):
            self.result["promptFeedback"] = event["promptFeedback"]
        self.result["modelVersion"] = event.get("modelVersion", self.result.get("modelVersion"))
        candidates = event.get("candidates") or []
        if not candidates:
            return ""
        candidate = candidates[0]
        pieces = candidate.get("content", {}).get("parts", [])
        for piece in copy.deepcopy(pieces):
            if set(piece) == {"thoughtSignature"} and self.parts:
                self.parts[-1]["thoughtSignature"] = piece["thoughtSignature"]
            else:
                self.parts.append(piece)
        if candidate.get("finishReason"):
            self.completion_seen = True
            self.result["finishReason"] = candidate["finishReason"]
            if "tokenCount" in candidate:
                self.result["candidateTokenCount"] = candidate["tokenCount"]
        if candidate.get("finishMessage"):
            self.result["finishMessage"] = candidate["finishMessage"]
        return "".join(p.get("text", "") for p in pieces if not p.get("thought"))

    def finish(self):
        """Called only after the HTTP body iterator ends without an error.

        Native stop reasons are metadata, not a content boundary. Consume the
        whole response before interpreting its final state or executing tools;
        same-response text fragments still belong to one story.
        """
        if not self.completion_seen:
            raise ValueError(
                upstream_message("模型流连接提前结束，本回合未提交", self.result, secret=self.secret)
            )
        if self.api == "chat_completions":
            message = {"content": self.text, "tool_calls": [self.calls[i] for i in sorted(self.calls)]}
            if self.reasoning:
                message["reasoning_content"] = self.reasoning
            if self.parts:
                message["reasoning_details"] = self.parts
            self.result["choices"] = [{"message": message, "finish_reason": self.result["finish_reason"]}]
        elif self.api == "responses":
            if self.result.get("status") != "completed":
                raise ValueError(
                    upstream_message("Responses 流未完整结束，本回合未提交", self.result, secret=self.secret)
                )
            self.result = self.response_output.finish(self.result)
        elif self.api == "anthropic":
            for block in self.blocks.values():
                if "_json" in block:
                    block["input"] = json.loads(block.pop("_json"))
            self.result["content"] = [self.blocks[i] for i in sorted(self.blocks)]
        elif self.api == "gemini":
            self.result["candidates"] = [
                {
                    "content": {"role": "model", "parts": self.parts},
                    "finishReason": self.result["finishReason"],
                    **(
                        {"tokenCount": self.result["candidateTokenCount"]}
                        if "candidateTokenCount" in self.result
                        else {}
                    ),
                    **(
                        {"finishMessage": self.result["finishMessage"]}
                        if "finishMessage" in self.result
                        else {}
                    ),
                }
            ]
        return self.result
