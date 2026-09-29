"""Incremental SSE accumulators; a disconnected stream never becomes a completed turn."""
import copy
import json


class StreamState:
    def __init__(self, api):
        self.api, self.done = api, False
        self.result, self.calls, self.blocks = {}, {}, {}
        self.text, self.reasoning, self.parts = "", "", []

    def feed(self, event):
        api = self.api
        if api == "responses":
            kind = event.get("type")
            if kind == "response.output_text.delta":
                return event.get("delta", "")
            if kind == "response.completed":
                self.result, self.done = event["response"], True
            elif kind in ("response.failed", "response.incomplete", "error"):
                raise ValueError("Responses 流未完整结束")
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
                item = self.calls.setdefault(call["index"], {"id": "", "type": "function",
                    "function": {"name": "", "arguments": ""}})
                if call.get("id"):
                    item["id"] = call["id"]
                for field in ("name", "arguments"):
                    item["function"][field] += call.get("function", {}).get(field) or ""
            if choice.get("finish_reason"):
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
                raise ValueError("Anthropic 流返回错误")
            return ""
        if event.get("error"):
            raise ValueError("Gemini 流返回错误")
        if event.get("usageMetadata"):
            self.result["usageMetadata"] = event["usageMetadata"]
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
