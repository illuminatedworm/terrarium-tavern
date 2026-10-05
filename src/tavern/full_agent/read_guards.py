"""Turn-local permissions for complete variable, setting-field and single-entry reads."""
import copy

from tavern.core.cache_policy import fingerprint
from tavern.full_agent.content import card_data

VERSION = 2


class DraftReads:
    def __init__(self, owner):
        self.owner = owner
        self.records = {"version": VERSION, "state": None, "fields": {}, "entries": {}}

    def restore(self, records):
        self.records["state"] = copy.deepcopy(records.get("state"))
        if records.get("version") == VERSION:
            self.records["fields"] = copy.deepcopy(records.get("fields", {}))
            self.records["entries"] = copy.deepcopy(records.get("entries", {}))

    def stamp(self, value):
        return {"window": self.owner.state["window"]["id"], "fingerprint": fingerprint(value)}

    def state_stamp(self):
        state = self.owner.snapshot["state"]
        return self.stamp({"exists": "stat_data" in state, "value": state.get("stat_data")})

    def read_state(self):
        self.records["state"] = self.state_stamp()

    def require_state(self):
        if self.records["state"] != self.state_stamp():
            raise ValueError('调用 patch_state 前必须在本回合当前窗口完整读取最新变量；'
                             '请调用 read_state，path 使用 "" 或 "stat_data"，空补丁也须先读')

    def character_stamp(self, field):
        data = card_data(self.owner.document)
        return self.stamp({"exists": field in data, "value": data.get(field)})

    def read_character(self, field):
        self.records["fields"][field] = self.character_stamp(field)

    def require_character(self, field):
        if self.records["fields"].get(field) != self.character_stamp(field):
            raise ValueError(f"修改前必须在本回合当前窗口用 read_character 完整读取最新字段：{field}")

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
            raise ValueError("修改或删除前必须在本回合当前窗口用 read_lore_entry 完整读取最新条目")

    def invalidate_content(self, fields, refs):
        for field in fields:
            self.records["fields"].pop(field, None)
        for ref in refs:
            self.records["entries"].pop(ref, None)
