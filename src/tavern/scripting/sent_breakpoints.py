"""Replay OpenAI breakpoints at content boundaries that were actually sent."""

import copy
import hashlib
import json

FIELD = "prompt_cache_breakpoint"
APIS = {"responses", "chat_completions"}


def boundaries(body, api):
    """Hash wire prefixes, excluding markers and normalizing string text blocks.

    A message index alone is not stable across edits, native replay or windows.
    Hash incrementally so checking every content boundary stays linear in input
    size. Block boundaries remain significant, including within one message.
    """
    digest = hashlib.sha256()

    def add(value):
        data = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)

    add(
        {
            key: body[key]
            for key in ("tools", "tool_choice", "instructions", "reasoning", "reasoning_effort")
            if key in body
        }
    )
    for index, item in enumerate(body.get("input" if api == "responses" else "messages", [])):
        content = item.get("content")
        if not isinstance(content, (str, list)):
            add(item)
            continue
        add({key: value for key, value in item.items() if key != "content"})
        parts = (
            [{"type": "input_text" if api == "responses" else "text", "text": content}]
            if isinstance(content, str)
            else content
        )
        for part_index, part in enumerate(parts):
            add({key: value for key, value in part.items() if key != FIELD})
            yield digest.hexdigest(), index, part_index, item, parts, part
        add("end_message")


def marked_boundaries(body, api):
    return [
        {
            "prefix_sha256": prefix,
            "message_index": index,
            "content_index": part_index,
            "value": copy.deepcopy(part[FIELD]),
        }
        for prefix, index, part_index, _, _, part in boundaries(body, api)
        if FIELD in part
    ]


class SentBreakpoints:
    def __init__(self, load=None, save=None):
        self.load, self.save = load, save
        self.entries = {}

    def restore(self, body, api, identity):
        known = self.entries.setdefault(identity, {})
        if self.load:
            known.update(self.load(identity))
        for prefix, _, _, item, parts, part in boundaries(body, api):
            if prefix in known and FIELD not in part:
                part[FIELD] = copy.deepcopy(known[prefix])
                item["content"] = parts

    def remember(self, identity, boundaries):
        known = self.entries.setdefault(identity, {})
        added = {
            entry["prefix_sha256"]: entry["value"]
            for entry in boundaries
            if known.get(entry["prefix_sha256"]) != entry["value"]
        }
        if added:
            if self.save:
                self.save(identity, added)
            known.update(copy.deepcopy(added))
