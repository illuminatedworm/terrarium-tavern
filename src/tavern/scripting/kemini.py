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


class ToolArguments:
    """Stream the content string of the first call to one of ``names``.

    Shared by Kemini transport and the Full Agent story preview. Only the first
    choice/candidate is read, as StreamState does; fragments are held while a
    split name can still match, so no argument prefix is lost.
    """

    def __init__(self, names):
        self.names = frozenset(names)
        self.call_names, self.pending = {}, {}
        self.selected = None
        self.decoder = ArgumentText()

    def _arguments(self, key, name, fragment):
        if name:
            self.call_names[key] = name
        name = self.call_names.get(key, "")
        if self.selected is not None and key != self.selected:
            return ""
        if name not in self.names:
            if any(target.startswith(name) for target in self.names):
                self.pending.setdefault(key, []).append(fragment)
            else:
                self.pending.pop(key, None)
            return ""
        self.selected = key
        return self.decoder.feed("".join(self.pending.pop(key, [])) + fragment)

    def feed(self, event: dict, api: str) -> str:
        chunks = []
        if api == "chat_completions":
            for choice in (event.get("choices") or [])[:1]:
                for call in (choice.get("delta") or {}).get("tool_calls") or []:
                    key, function = call.get("index", 0), call.get("function") or {}
                    name = self.call_names.get(key, "") + (function.get("name") or "")
                    chunks.append(self._arguments(key, name, function.get("arguments") or ""))
        elif api == "responses":
            kind, item = event.get("type"), event.get("item") or {}
            if kind == "response.output_item.added" and item.get("type") == "function_call":
                chunks.append(self._arguments(item["id"], item["name"], item.get("arguments") or ""))
            elif kind == "response.function_call_arguments.delta":
                chunks.append(self._arguments(event["item_id"], "", event.get("delta") or ""))
            elif (
                kind == "response.output_item.done"
                and item.get("type") == "function_call"
                and self.selected is None
            ):
                # Some relays send only completed items, without argument deltas.
                chunks.append(self._arguments(item["id"], item["name"], item.get("arguments") or ""))
        elif api == "anthropic":
            kind, block = event.get("type"), event.get("content_block") or {}
            if kind == "content_block_start" and block.get("type") == "tool_use":
                # Streamed input starts empty; StreamState rebuilds it from deltas.
                self._arguments(event["index"], block["name"], "")
            elif kind == "content_block_delta" and "partial_json" in (event.get("delta") or {}):
                chunks.append(self._arguments(event["index"], "", event["delta"]["partial_json"]))
        else:
            # Requests never enable Vertex streamFunctionCallArguments, so
            # Gemini delivers each functionCall with its complete args once.
            for candidate in (event.get("candidates") or [])[:1]:
                for index, part in enumerate(candidate.get("content", {}).get("parts", [])):
                    call = part.get("functionCall")
                    if call:
                        arguments = json.dumps(call.get("args", {}), ensure_ascii=False)
                        chunks.append(
                            self._arguments(call.get("id") or index, call.get("name", ""), arguments)
                        )
        return "".join(chunks)


class KeminiReply:
    def __init__(self, config: KeminiConfig, transport: bool, *, provisional=False):
        self.config, self.transport = config, transport
        self.parser = AuxiliaryParser(config)
        self.story = ToolArguments({TOOL_NAME})
        self.emitted = ""
        self.parts = []
        self.transported = False
        self.provisional, self.preview_stopped = provisional, False

    def feed(self, event: dict, plain: str, api: str) -> str:
        if self.preview_stopped:
            return ""
        try:
            delta = self.parser.feed(self.story.feed(event, api) if self.transport else plain)
        except ValueError:
            if not self.provisional:
                raise
            # Full Agent validates the complete response. A partially decoded
            # escape can invalidate only its preview, not interrupt the reader
            # before authoritative arguments and any error tail arrive.
            self.preview_stopped = True
            return ""
        self.emitted += delta
        return delta

    def finish(
        self, text: str, calls: list[NativeToolCall], *, preserve_story_tool=False
    ) -> tuple[str, list[NativeToolCall]]:
        if preserve_story_tool:
            # Full Agent counts ordinary prose and the visible experimental
            # transport together. Keep native calls/signatures until it accepts
            # the one story; tool results still belong to the model's loop.
            return text, calls
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
