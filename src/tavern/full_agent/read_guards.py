"""Current-context evidence for the versions targeted by draft edits."""

import copy

from tavern.core.cache_policy import fingerprint
from tavern.full_agent.content import card_data
from tavern.full_agent.protocol import ToolError

VERSION = 4


def state_scope(value):
    """Normalize legacy tool arguments without advertising retired scope names."""
    return {"variables": "mvu", "story": "full"}.get(value, value) if isinstance(value, str) else value


class DraftReads:
    def __init__(self, owner):
        self.owner = owner
        self.clear()

    def clear(self):
        self.records = {"version": VERSION, "state": None, "fields": {}, "entries": {}, "notes": {}}

    def observe_context(self, messages):
        # Reconstruct from the actual, post-window/post-compaction context. A
        # persisted read flag alone cannot prove that its source is still here.
        from tavern.full_agent.visibility import observe_context

        self.clear()
        observe_context(self, messages)

    def restore(self, records):
        if records.get("version") == VERSION:
            self.records["state"] = copy.deepcopy(records.get("state"))
            self.records["fields"] = copy.deepcopy(records.get("fields", {}))
            self.records["entries"] = copy.deepcopy(records.get("entries", {}))
            self.records["notes"] = copy.deepcopy(records.get("notes", {}))

    def stamp(self, value):
        return {"window": self.owner.state["window"]["id"], "fingerprint": fingerprint(value)}

    def state_stamp(self):
        state = self.owner.snapshot["state"]
        return self.stamp({"exists": "stat_data" in state, "value": state.get("stat_data")})

    def read_state(self):
        self.records["state"] = self.state_stamp()

    def require_state(self):
        if self.records["state"] != self.state_stamp():
            raise ToolError(
                "READ_REQUIRED", '当前上下文未包含完整最新变量；请调用 read_state(path="", scope="mvu")'
            )

    def character_stamp(self, field):
        data = card_data(self.owner.document)
        return self.stamp({"exists": field in data, "value": data.get(field)})

    def read_character(self, field):
        self.records["fields"][field] = self.character_stamp(field)

    def require_character(self, field):
        if self.records["fields"].get(field) != self.character_stamp(field):
            raise ToolError(
                "READ_REQUIRED", f"当前上下文未包含完整最新字段；请用 read_character 读取：{field}"
            )

    def entry_stamp(self, raw):
        value = copy.deepcopy(raw)
        value.pop("id", None)
        if isinstance(value.get("extensions"), dict):
            value["extensions"].pop("id", None)
        return self.stamp(value)

    def read_entry(self, ref, raw):
        self.records["entries"][ref] = self.entry_stamp(raw)

    def require_entry(self, ref, raw):
        if self.records["entries"].get(ref) != self.entry_stamp(raw):
            raise ToolError(
                "READ_REQUIRED", "当前上下文未包含完整最新条目正文；请用 read_lore_entry 读取后修改或删除"
            )

    def note_stamp(self, key):
        return self.stamp(self.owner.state["notes"][key]["content"])

    def read_note(self, key):
        self.records["notes"][key] = self.note_stamp(key)

    def require_note(self, key):
        if key in self.owner.state["notes"] and self.records["notes"].get(key) != self.note_stamp(key):
            raise ToolError("READ_REQUIRED", "当前上下文未包含该笔记的完整最新正文；请先用 read_note 读取")

    def invalidate_content(self, fields, refs):
        for field in fields:
            self.records["fields"].pop(field, None)
        for ref in refs:
            self.records["entries"].pop(ref, None)
