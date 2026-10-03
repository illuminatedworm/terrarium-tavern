"""Replay durable runtime frames and append only changes to their effective values."""
import copy
import json

from tavern.core.cache_policy import fingerprint
from tavern.scripting.native_response import REASONING_FIELDS, portable, prepare_messages
from tavern.scripting.request_context import response_origin

# Retire old countdown frames and token-by-token pressure notices on rebuild.
VERSION = 6
CLEAR_TEXT = {
    "post": "<card_post_history_instructions>\n此前的角色后置要求已撤销。\n</card_post_history_instructions>",
    "turn_instruction": "此前的额外回合要求已撤销；遵守当前工具权限。",
    "regenerate_instruction": "此前的重生成要求已撤销。",
    "turn_variable_instruction": "本轮未要求额外更新 MVU 变量。",
    "context_warning": "此前的 checkpoint 过时提醒已解除。",
    "context_budget": "此前的窗口压力提醒已解除。",
}


class ContextLedger:
    def __init__(self, settings, fixed, tools, document, snapshot, records, input_id, history):
        self.origin = response_origin(settings)
        window = snapshot["full_agent"]["window"]
        signature = fingerprint({"fixed": fixed, "tools": tools, "document": document,
            "reasoning": [settings.reasoning_effort, settings.reasoning_format],
            "removed_lore": [r for r in snapshot.get("lore_injections", []) if r.get("manually_removed")]})
        previous = next((m["_context_baseline"] for m in reversed(records) if m.get("_context_baseline")), None)
        observed = {}
        for message in records:
            if message.get("_context_baseline"):
                observed = dict(message["_context_baseline"].get("history", {}))
            observed.update(message.get("_history_observations", {}))
        self.reason = None
        if previous is None:
            self.reason = "legacy_context" if records else "initial_context"
        elif previous.get("version") != VERSION:
            self.reason = "context_version_changed"
        elif previous.get("origin") != self.origin:
            self.reason = "provider_or_model_changed"
        elif previous.get("signature") != signature:
            self.reason = "context_sources_changed"
        elif any(history[key] != observed[key] for key in history.keys() & observed.keys()):
            self.reason = "history_content_changed"
        self.epoch = (fingerprint([VERSION, signature, self.origin, window["id"], input_id, history])
                      if self.reason else previous["epoch"])
        # Changing local instructions or tools requires a new runtime baseline,
        # but does not invalidate an unchanged response from the same provider.
        # Keep a separate generation so a model switch A -> B -> A cannot revive
        # opaque A-state from before the switch.
        self.native_epoch = (previous.get("native_epoch", previous["epoch"])
                             if previous and previous.get("origin") == self.origin
                             else self.epoch)
        self.additions, self.invalidations = [], []
        self.seen, self.calls = {}, {}
        if self.reason:
            self.additions.append({"role": "system", "content": "", "_context_baseline": {
                "version": VERSION, "signature": signature, "origin": self.origin, "epoch": self.epoch,
                "history": dict(history), "native_epoch": self.native_epoch}})

    def replay(self, message, source="agent_tools"):
        if message.get("_context_baseline") or message.get("_full_agent_final"):
            return None
        frame = message.get("_runtime_context")
        if frame:
            if frame.get("epoch") != self.epoch:
                return None
            self.seen[message["source"]] = copy.deepcopy(frame["value"])
            return copy.deepcopy(message)
        item = self.native_message(message)
        self.observe_tools(item)
        return {**item, "source": source}

    def native_message(self, message):
        has_native = any(k in message for k in (*REASONING_FIELDS, "_native_response"))
        reference = {"message_id": message.get("message_id"),
                     "response_id": (message.get("_native_response") or {}).get("response_id")}
        if has_native and message.get("_native_epoch", message.get("_context_epoch")) != self.native_epoch:
            self.invalidations.append({**reference, "reason": "native_context_changed"})
            return portable(message)
        items, invalid = prepare_messages([message], self.origin)
        self.invalidations.extend({**reference, **item} for item in invalid)
        return items[0]

    def observe_tools(self, message):
        for call in message.get("tool_calls", []):
            self.calls[call["id"]] = call["function"]
        if message.get("role") != "tool":
            return
        call = self.calls.get(message.get("tool_call_id"))
        if not call or call["name"] != "write_checkpoint":
            return
        try:
            result, args = json.loads(message.get("content", "")), json.loads(call["arguments"])
        except (ValueError, TypeError):
            return
        # A proposal in tool arguments is not an effective memory until the
        # matching tool result confirms that the local draft accepted it.
        if isinstance(result, dict) and result.get("saved_to_draft") and "error" not in result:
            self.seen["checkpoint"] = args["content"]

    def update(self, source, content, *, value=None):
        value = content if value is None else value
        if source in self.seen and self.seen[source] == value:
            return None
        if not content:
            if source not in self.seen or not self.seen[source]:
                return None
            content = CLEAR_TEXT[source]
        frame = {"role": "system", "content": content, "source": source,
                 "_runtime_context": {"epoch": self.epoch, "value": copy.deepcopy(value)}}
        self.seen[source] = copy.deepcopy(value)
        self.additions.append(frame)
        return frame

    def audit(self):
        return {"version": VERSION, "epoch": self.epoch, "native_epoch": self.native_epoch,
                "rebuild_reason": self.reason,
                "native_replay_invalidations": self.invalidations,
                "updates": [{"source": m["source"], "value_sha256": fingerprint(m["_runtime_context"]["value"])}
                            for m in self.additions if "_runtime_context" in m]}
