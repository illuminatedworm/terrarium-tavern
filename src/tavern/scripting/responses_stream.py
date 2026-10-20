"""Reassemble Responses output items even when the terminal event only carries usage."""

import copy


class ResponsesOutput:
    def __init__(self):
        self.items = {}
        self.completed = set()

    def accept(self, event):
        kind = event.get("type")
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
        if kind == "response.output_item.done":
            self.completed.add(index)

    def finish(self, response):
        result = copy.deepcopy(response)
        output = result.get("output", [])
        if not isinstance(output, list) or any(not isinstance(item, dict) for item in output):
            raise ValueError("Responses 最终输出格式无效，本轮未提交")
        # output_item.done contains complete messages, function arguments and encrypted
        # reasoning. A valid response.completed may contain output=[]; it is not a deletion.
        items = copy.deepcopy(self.items)
        complete = self.completed.copy()
        by_id = {item["id"]: index for index, item in items.items() if item.get("id")}
        for position, item in enumerate(output):
            index = by_id.get(item.get("id"), position)
            if index in items and items[index].get("id") != item.get("id"):
                index = max(items, default=-1) + 1
            previous = items.get(index, {})
            merged = {**previous, **copy.deepcopy(item)}
            # Some summary projections retain item metadata but omit its content.
            for field in ("content", "encrypted_content", "arguments"):
                if previous.get(field) and not item.get(field):
                    merged[field] = previous[field]
            items[index] = merged
            complete.add(index)
        if set(items) - complete:
            raise ValueError("Responses 有输出项未完整接收，本轮未提交")
        result["output"] = [items[index] for index in sorted(items)]
        return result
