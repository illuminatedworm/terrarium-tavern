"""Reassemble Responses output items even when the terminal event only carries usage."""

import copy


class ResponsesOutput:
    def __init__(self):
        self.items = {}
        self.completed = set()
        self.covered = set()

    def accept(self, event):
        kind = event.get("type")
        if kind in ("response.completed", "response.incomplete"):
            self._snapshot(event["response"])
            return
        if kind not in ("response.output_item.added", "response.output_item.done"):
            return
        index, item = event.get("output_index"), event.get("item")
        if not isinstance(index, int) or isinstance(index, bool) or index < 0 or not isinstance(item, dict):
            raise ValueError("Responses 输出项格式无效，本轮未提交")
        previous = self.items.get(index)
        if previous and previous.get("id") and item.get("id") and previous["id"] != item["id"]:
            raise ValueError("Responses 输出项身份冲突，本轮未提交")
        if index in self.completed:
            if kind != "response.output_item.done" or previous != item:
                raise ValueError("Responses 已完成输出项被改写，本轮未提交")
            return
        self.items[index] = copy.deepcopy(item)
        self.covered.discard(index)
        if kind == "response.output_item.done":
            self.completed.add(index)

    def _snapshot(self, response):
        output = response.get("output", [])
        if not isinstance(output, list) or any(not isinstance(item, dict) for item in output):
            raise ValueError("Responses 最终输出格式无效，本轮未提交")
        # Merge snapshots when they arrive, not at EOF: a later done item must
        # supersede an earlier nonempty completed snapshot. Summary projections
        # may omit content, so empty fields do not erase already received text.
        by_id = {item["id"]: index for index, item in self.items.items() if item.get("id")}
        for position, item in enumerate(output):
            index = by_id.get(item.get("id"), position)
            if index in self.items and self.items[index].get("id") != item.get("id"):
                index = max(self.items, default=-1) + 1
            previous = self.items.get(index, {})
            merged = {**previous, **copy.deepcopy(item)}
            # Some summary projections retain item metadata but omit its content.
            for field in ("content", "encrypted_content", "arguments"):
                if previous.get(field) and not item.get(field):
                    merged[field] = previous[field]
            self.items[index] = merged
            self.completed.discard(index)
            self.covered.add(index)
            if item.get("id"):
                by_id[item["id"]] = index

    def finish(self, response):
        if set(self.items) - self.completed - self.covered:
            raise ValueError("Responses 有输出项未完整接收，本轮未提交")
        result = copy.deepcopy(response)
        result["output"] = [copy.deepcopy(self.items[index]) for index in sorted(self.items)]
        return result
