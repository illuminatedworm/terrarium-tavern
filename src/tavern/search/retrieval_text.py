"""Streaming side-channel parser. Hidden text never reaches MVU or story history."""

import re

OPEN = "<tavern_retrieval>"
CLOSE = "</tavern_retrieval>"


class RetrievalParser:
    def __init__(self, limit=512):
        self.limit = limit
        self.buffer = ""
        self.hidden = False
        self.parts = []
        self.current = ""
        self.diagnostics = []

    def feed(self, text, final=False):
        self.buffer += text
        output = ""
        while self.buffer:
            marker = CLOSE if self.hidden else OPEN
            index = self.buffer.find(marker)
            recovery = (
                re.search(r"<(?:UpdateVariable|JSONPatch)\b", self.buffer, re.I) if self.hidden else None
            )
            if recovery and (index < 0 or recovery.start() < index):
                self.diagnostics.append("检索线索未闭合，已忽略；保留 MVU 更新块")
                self.current = ""
                self.hidden = False
                self.buffer = self.buffer[recovery.start() :]
                continue
            if index >= 0:
                before, self.buffer = self.buffer[:index], self.buffer[index + len(marker) :]
                if self.hidden:
                    self.current += before
                    self.parts.append(self.current.strip())
                    if len(self.current.strip()) > self.limit:
                        self.diagnostics.append("检索线索较长，已完整保留")
                    self.current = ""
                else:
                    output += before
                self.hidden = not self.hidden
                continue
            hold = 0
            if not final:
                for boundary in [marker, "<UpdateVariable>", "<JSONPatch>"] if self.hidden else [marker]:
                    for n in range(1, min(len(boundary), len(self.buffer) + 1)):
                        if self.buffer.endswith(boundary[:n]):
                            hold = max(hold, n)
            # An unclosed hidden block must not swallow a valid trailing MVU block.
            if self.hidden and final:
                match = re.search(r"<(?:UpdateVariable|JSONPatch)\b", self.buffer, re.I)
                if match:
                    output += self.buffer[match.start() :]
                    self.buffer = self.buffer[: match.start()]
            ready = self.buffer[:-hold] if hold else self.buffer
            self.buffer = self.buffer[-hold:] if hold else ""
            if final and not self.hidden:
                for n in range(8, len(OPEN)):
                    if ready.endswith(OPEN[:n]):
                        ready = ready[:-n]
                        self.diagnostics.append("检索区块起始标签被截断，已隐藏")
                        break
            if self.hidden:
                self.current += ready
            else:
                output += ready
            break
        if final:
            if self.hidden:
                self.diagnostics.append("检索线索未闭合，已忽略")
            if not self.parts:
                self.diagnostics.append("模型未提供有效检索线索")
            if len(self.parts) > 1:
                self.diagnostics.append("重复检索线索，仅采用第一段")
        return output

    @property
    def query(self):
        return self.parts[0] if self.parts else ""


def split_retrieval(text, limit=512):
    parser = RetrievalParser(limit)
    clean = parser.feed(text, final=True)
    return clean, parser.query, parser.diagnostics


def query_text(history, user_input, config):
    clues = [m.get("retrieval_query", "") for m in history if m.get("retrieval_query")]
    clues = clues[-config.query_lookback :] if config.query_lookback else []
    current = user_input
    fallback = ""
    if not clues and current.strip().lower() in ("", "继续", "继续剧情", "continue", "続けて") and history:
        from tavern.search.retrieval import tokenize

        fallback = " ".join(dict.fromkeys(tokenize(history[-1].get("model", history[-1].get("raw", "")))))
    return "\n".join(filter(None, [*clues, fallback, current])).strip()


def query_instruction(config):
    return config.protocol_prompt + "\n" + OPEN + "\n" + config.query_prompt + "\n" + CLOSE
