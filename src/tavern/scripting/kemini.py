"""Story-only response transport; never dispatches or repairs business tools."""

import json
import re

from kohakuterrarium.llm.base import NativeToolCall

from tavern.core.kemini import TOOL_NAME, KeminiConfig


class AuxiliaryParser:
    def __init__(self, config: KeminiConfig):
        self.tags = {
            tag: kind
            for enabled, tag, kind in (
                (config.enabled, "thinking", "thinking"),
                (config.enabled and config.normal_disclaimer_enabled, "disclaimer", "disclaimer"),
            )
            if enabled
        }
        self.buffer, self.hidden, self.current = "", "", ""
        self.fence = ""
        self.parts = []

    def feed(self, text: str, *, final: bool = False) -> str:
        self.buffer += text
        output = []
        while self.buffer:
            markers = (
                [f"</{self.hidden}>"]
                if self.hidden
                else [self.fence]
                if self.fence
                else [*[f"<{tag}>" for tag in self.tags], "```", "~~~"]
            )
            matches = [(self.buffer.find(marker), marker) for marker in markers if marker in self.buffer]
            if matches:
                index, marker = min(matches)
                before, self.buffer = self.buffer[:index], self.buffer[index + len(marker) :]
                if marker in ("```", "~~~"):
                    output.append(before + marker)
                    self.fence = "" if self.fence else marker
                    continue
                if self.hidden:
                    self.parts.append({"kind": self.tags[self.hidden], "text": self.current + before})
                    self.hidden, self.current = "", ""
                else:
                    output.append(before)
                    self.hidden = marker[1:-1]
                continue
            hold = (
                max(
                    (
                        n
                        for marker in markers
                        for n in range(1, len(marker))
                        if self.buffer.endswith(marker[:n])
                    ),
                    default=0,
                )
                if not final
                else 0
            )
            ready = self.buffer[:-hold] if hold else self.buffer
            self.buffer = self.buffer[-hold:] if hold else ""
            if self.hidden:
                self.current += ready
            else:
                output.append(ready)
            break
        if final and self.hidden:
            raise ValueError(f"实验输出的 <{self.hidden}> 未闭合，本回合未提交；原始回复保留在工作记录中")
        return "".join(output)


class ArgumentText:
    """Decode only the schema's content string, holding incomplete JSON escapes."""

    def __init__(self):
        self.buffer, self.started, self.done, self.high = "", False, False, ""

    def feed(self, fragment):
        self.buffer += fragment
        if not self.started:
            match = re.match(r'^\s*\{\s*"content"\s*:\s*"', self.buffer)
            if not match:
                return ""  # Full JSON validation, not speculative repair, decides at completion.
            self.buffer, self.started = self.buffer[match.end() :], True
        output, at = [], 0
        while at < len(self.buffer) and not self.done:
            char = self.buffer[at]
            if char == '"':
                self.done = True
                break
            if char == "\\":
                if at + 1 == len(self.buffer):
                    break
                width = 6 if self.buffer[at + 1] == "u" else 2
                if at + width > len(self.buffer):
                    break
                char = json.loads('"' + self.buffer[at : at + width] + '"')
                at += width
            else:
                at += 1
            if 0xD800 <= ord(char) <= 0xDBFF:
                self.high = char
                continue
            if self.high:
                if not 0xDC00 <= ord(char) <= 0xDFFF:
                    raise ValueError("正文传输参数含无效 Unicode")
                char = chr(0x10000 + ((ord(self.high) - 0xD800) << 10) + ord(char) - 0xDC00)
                self.high = ""
            output.append(char)
        self.buffer = self.buffer[at:]
        return "".join(output)


class KeminiReply:
    def __init__(self, config: KeminiConfig, transport: bool):
        self.config, self.transport = config, transport
        self.parser = AuxiliaryParser(config)
        self.names, self.decoders = {}, {}
        self.emitted = ""
        self.parts = []
        self.transported = False

    def _arguments(self, key, name, fragment):
        if name:
            self.names[key] = name
        if self.names.get(key) != TOOL_NAME:
            return ""
        return self.decoders.setdefault(key, ArgumentText()).feed(fragment)

    def feed(self, event: dict, plain: str, api: str) -> str:
        if not self.transport:
            delta = self.parser.feed(plain)
        else:
            carried = ""
            if api == "chat_completions":
                for choice in event.get("choices") or []:
                    for call in (choice.get("delta") or {}).get("tool_calls") or []:
                        key = call.get("index", 0)
                        fn = call.get("function") or {}
                        name = self.names.get(key, "") + (fn.get("name") or "")
                        carried += self._arguments(key, name, fn.get("arguments") or "")
            elif api == "responses":
                item = event.get("item") or {}
                if event.get("type") == "response.output_item.added" and item.get("type") == "function_call":
                    carried += self._arguments(item["id"], item["name"], item.get("arguments") or "")
                elif event.get("type") == "response.function_call_arguments.delta":
                    carried += self._arguments(event["item_id"], "", event.get("delta") or "")
            elif api == "anthropic":
                block = event.get("content_block") or {}
                if event.get("type") == "content_block_start" and block.get("type") == "tool_use":
                    self.names[event["index"]] = block["name"]
                elif event.get("type") == "content_block_delta":
                    carried += self._arguments(
                        event["index"], "", (event.get("delta") or {}).get("partial_json") or ""
                    )
            else:
                # Requests never enable Vertex streamFunctionCallArguments, so
                # Gemini delivers each functionCall with its complete args once.
                for candidate in event.get("candidates") or []:
                    for part in candidate.get("content", {}).get("parts", []):
                        call = part.get("functionCall") or {}
                        args = call.get("args")
                        if (
                            call.get("name") == TOOL_NAME
                            and isinstance(args, dict)
                            and isinstance(args.get("content"), str)
                        ):
                            carried += args["content"]
            delta = self.parser.feed(carried)
        self.emitted += delta
        return delta

    def finish(self, text: str, calls: list[NativeToolCall]) -> tuple[str, list[NativeToolCall]]:
        synthetic = [call for call in calls if call.name == TOOL_NAME] if self.transport else []
        if synthetic:
            if len(synthetic) != 1 or len(calls) != 1:
                raise ValueError("正文传输必须单独调用一次，不能与业务工具混合；本次响应未执行任何工具")
            try:
                args = json.loads(synthetic[0].arguments)
            except (ValueError, TypeError):
                raise ValueError("正文传输参数不是完整 JSON，本回合未提交") from None
            if not isinstance(args, dict) or set(args) != {"content"} or not isinstance(args["content"], str):
                raise ValueError("正文传输需要且仅接受字符串 content，本回合未提交")
            if text.strip():
                self.parts.append({"kind": "extra", "text": text})
            text, calls, self.transported = args["content"], [], True
        elif calls:
            # Genuine tool exchanges retain their original native response and permissions.
            return text, calls
        parser = AuxiliaryParser(self.config)
        clean = parser.feed(text, final=True)
        self.parts.extend(parser.parts)
        if not clean.strip():
            raise ValueError("实验输出未包含故事正文，本回合未提交")
        if not clean.startswith(self.emitted):
            raise ValueError("正文传输的最终回复与流式片段不一致，本回合未提交")
        return clean, calls

    def tail(self, text: str) -> str:
        return text[len(self.emitted) :]
