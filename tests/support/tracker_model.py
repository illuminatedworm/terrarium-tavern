"""Native-tool test models for the unified Tracker; no external model calls."""

import copy
import json

from kohakuterrarium.llm.base import ChatResponse, NativeToolCall


def batch_payload(messages):
    for message in reversed(messages):
        if message["role"] not in ("user", "tool"):
            continue
        try:
            value = json.loads(message["content"])
        except (ValueError, TypeError):
            continue
        if "next_memory_task" in value:
            return value["next_memory_task"]
        if "memory_batch" in value:
            return value["memory_batch"]
    return None


class TrackerModel:
    def __init__(self, *steps):
        self.steps = list(steps)
        self.requests = []
        self.last_tool_calls = []
        self.closed = False

    async def chat_complete(self, messages, **kwargs):
        self.requests.append({"messages": copy.deepcopy(messages), **copy.deepcopy(kwargs)})
        index = len(self.requests) - 1
        step = self.steps[index] if index < len(self.steps) else "完成"
        if callable(step):
            step = step(messages)
        self.last_tool_calls = []
        if isinstance(step, Exception):
            raise step
        if isinstance(step, tuple):
            step = [step]
        if isinstance(step, list):
            self.last_tool_calls = [
                NativeToolCall(f"call{index}-{i}", name, json.dumps(args, ensure_ascii=False))
                for i, (name, args) in enumerate(step)
            ]
            step = ""
        return ChatResponse(content=step, finish_reason="stop", usage={}, model="fixture")

    async def close(self):
        self.closed = True

    @property
    def call_count(self):
        return len(self.requests)

    @property
    def call_log(self):
        return [r["messages"] for r in self.requests]

    @property
    def last_messages(self):
        return self.requests[-1]["messages"] if self.requests else None


class MemoryModel(TrackerModel):
    """Adapt old memory proposal fixtures to the actual write_memory protocol."""

    def __init__(self, proposals):
        self.proposals = list(proposals)
        self.seen = 0
        super().__init__()

    async def chat_complete(self, messages, **kwargs):
        batch = batch_payload(messages)
        # A successful batch may be followed by another batch or a review in
        # the same result. A result without next_memory_task ends this work.
        latest = messages[-1]
        complete = latest["role"] == "tool" and json.loads(latest["content"]).get("memory_complete")
        if batch and not complete:
            value = self.proposals[min(self.seen, len(self.proposals) - 1)]
            self.seen += 1
            if callable(value):
                value = value(messages)
            value = json.loads(value) if isinstance(value, str) else value
            self.steps.append(("write_memory", value))
        else:
            self.steps.append("完成")
        return await super().chat_complete(messages, **kwargs)


class PatchModel(TrackerModel):
    def __init__(self, proposal):
        try:
            operations = json.loads(proposal) if isinstance(proposal, str) else proposal
        except ValueError:
            operations = proposal
        super().__init__(
            ("patch_state", {"operations": operations, "comment": "Fixture turn evidence"}), "完成"
        )
