"""Committed editor request prefixes, independent of disposable activity logs."""
import copy

from tavern.conversation.half_context import attach_continuation
from tavern.core.cache_policy import fingerprint
from tavern.core.model_view import HANDLE, ModelView
from tavern.scripting.request_context import response_origin


def history_key(runs):
    return fingerprint([[r["id"], r["request"]["text"], r["response"]] for r in runs])


class EditorContext:
    def __init__(self, saved, *, workspace, runs, request, settings, instructions):
        self.origin = response_origin(settings)
        self.policy = fingerprint([instructions, settings.reasoning_effort, settings.reasoning_format])
        seed = {"document": workspace["document"], "input": request["text"], "previous": [
            [r["request"]["text"], r["response"]] for r in runs]}
        self.reason = None
        if not saved:
            self.reason = "initial_context"
        elif saved.get("version") != 1:
            self.reason = "context_version_changed"
        elif saved["origin"] != self.origin:
            self.reason = "provider_or_model_changed"
        elif saved["policy"] != self.policy:
            self.reason = "context_sources_changed"
        elif saved["history"] != history_key(runs):
            self.reason = "context_window_changed"
        elif saved["document"] != fingerprint(workspace["document"]):
            self.reason = "draft_changed"
        elif set(HANDLE.findall(str(seed))) & set(saved["view"]["reverse"]):
            self.reason = "literal_handle_collision"
        self.view = ModelView(seed if self.reason else None)
        if self.reason:
            self.messages = [{"role": "system", "content": text, "_cache_static": True}
                             for text in instructions]
            for run in runs:
                self.messages.extend([{"role": "user", "content": run["request"]["text"]},
                                      {"role": "assistant", "content": run["response"]}])
            self.messages = self.view.messages(self.messages)
            self.overview = None
            self.epoch = fingerprint([workspace["id"], self.origin, self.policy, history_key(runs),
                                      workspace["document"], request["operation_id"]])
        else:
            self.view.forward = copy.deepcopy(saved["view"]["forward"])
            self.view.reverse = copy.deepcopy(saved["view"]["reverse"])
            self.view.reserved = set(saved["view"]["reserved"]) | set(HANDLE.findall(str(seed)))
            self.view.seed(seed)
            self.messages = copy.deepcopy(saved["messages"])
            self.overview = saved["overview"]
            self.epoch = saved["epoch"]
        self.responses = {}
        self.continuation_count = 0
        self.started = False
        self.last_assistant = None

    def prepare(self, messages, overview, *, last_call=False):
        marker = next((i for i, m in enumerate(messages) if m.get("role") == "user"), len(messages))
        if not self.started:
            self.messages.extend(self.view.messages(messages[marker:marker + 1]))
            self.started = True
        continuation = attach_continuation(messages[marker + 1:], self.responses)
        self.messages.extend(self.view.messages(continuation[self.continuation_count:]))
        self.continuation_count = len(continuation)
        if overview != self.overview:
            self.messages.append({"role": "system", "_cache_static": False,
                "content": "当前草稿概览（取代此前概览；字段内容以最新成功修改或读取为准）：" + overview})
            self.overview = overview
        if last_call:
            self.messages.append({"role": "system", "_cache_static": False,
                "content": "本轮最后一次调用：不再调用工具，汇报完成和未完成的工作。此限制仅适用于本轮。"})
        return copy.deepcopy(self.messages)

    def capture(self, provider, response):
        native = getattr(provider, "last_native_response", None)
        if native:
            self.responses[native["message_sha256"]] = copy.deepcopy(native)
        self.last_assistant = {"role": "assistant", "content": response,
                              **copy.deepcopy(getattr(provider, "last_assistant_extra_fields", {}) or {})}
        if native:
            self.last_assistant["_native_response"] = copy.deepcopy(native)

    def saved(self, runs, document):
        return {"version": 1, "origin": self.origin, "policy": self.policy, "epoch": self.epoch,
                "history": history_key(runs), "document": fingerprint(document), "overview": self.overview,
                "messages": copy.deepcopy(self.messages + [self.last_assistant]),
                "view": {"forward": dict(self.view.forward), "reverse": dict(self.view.reverse),
                         "reserved": sorted(self.view.reserved)}}
