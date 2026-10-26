"""Append-only editor requests with explicit model-controlled context rollover."""

import copy
import json

from tavern.character.editor_attachments import message_content
from tavern.character.editor_draft_state import draft_changes, draft_versions
from tavern.character.editor_environment import environment_message
from tavern.character.editor_images import IMAGE_REFS, image_refs, image_stats
from tavern.character.editor_reads import reconcile
from tavern.core.cache_policy import fingerprint
from tavern.core.message_adapter import adapt_messages, runtime_messages
from tavern.core.model_view import HANDLE, ModelView
from tavern.core.tokens import request_tokens
from tavern.scripting.native_response import portable
from tavern.scripting.request_context import response_origin


def signature(working):
    return fingerprint(
        {
            "cards": [
                {k: c.get(k) for k in ("ref", "document", "target_mode", "brief")} for c in working["cards"]
            ],
            "state": working["state"],
        }
    )


class EditorContext:
    def refresh_tool_policy(self):
        self.policy = fingerprint(
            [
                self.owner.cfg.card_author_config.system_prompt,
                self.owner.definitions,
                self.owner.environment,
                "message-attachments-v1",
            ]
        )

    def system_message(self):
        # Use the operation snapshot verbatim, including an explicitly empty
        # prompt. The default is only for old settings missing this field.
        return {
            "role": "system",
            "message_type": "root_system",
            "runtime_phase": "header",
            "content": self.owner.cfg.card_author_config.system_prompt,
            "_cache_static": True,
        }

    def header(self):
        return [
            self.system_message(),
            *([environment_message(self.owner.environment)] if self.owner.environment else []),
        ]

    def __init__(self, saved, owner):
        self.owner = owner
        self.origin = response_origin(owner.cfg)
        self.refresh_tool_policy()
        history = owner.edits.messages(owner.wid)
        seed = {
            "documents": [c["document"] for c in owner.working["cards"]],
            "history": [m["content"] for m in history],
        }
        self.reason = None
        if not saved or saved.get("version") != 4:
            self.reason = "initial_context" if not saved else "context_version_changed"
        elif saved["origin"] != self.origin:
            self.reason = "provider_or_model_changed"
        elif saved["policy"] != self.policy:
            self.reason = "context_sources_changed"
        elif any(
            set(HANDLE.findall(m["content"])) & set(saved["view"]["reverse"])
            for m in history
            if m["ref"] not in saved["seen"]
        ):
            self.reason = "literal_handle_collision"
        self.view = ModelView(seed if self.reason else None)
        if saved and saved.get("version") in (2, 3, 4) and self.reason != "literal_handle_collision":
            self.view.forward = copy.deepcopy(saved["view"]["forward"])
            self.view.reverse = copy.deepcopy(saved["view"]["reverse"])
            self.view.reserved = set(saved["view"]["reserved"])
            self.view.seed(seed)
        self.frames = {}
        self.seen = []
        self.epoch = fingerprint([owner.wid, self.origin, self.policy, signature(owner.working), owner.rid])
        if not self.reason:
            owner.reads = copy.deepcopy(saved.get("read_versions", {}))
            self.view.forward = copy.deepcopy(saved["view"]["forward"])
            self.view.reverse = copy.deepcopy(saved["view"]["reverse"])
            self.view.reserved = set(saved["view"]["reserved"])
            self.view.seed(seed)
            self.messages = copy.deepcopy(saved["messages"])
            self.frames, self.seen = copy.deepcopy(saved["frames"]), list(saved["seen"])
            # Upgrade a still-current old comparison ledger without changing any
            # already-sent message or forcing a new model/cache window.
            overview = self.frames.get("overview")
            if (
                overview is not None
                and "documents" not in overview
                and saved["document"] == signature(owner.working)
            ):
                self.frames["overview"] = {
                    "overview": overview,
                    "documents": draft_versions(owner.working["cards"]),
                }
            self.epoch = saved["epoch"]
        else:
            self.messages = self.header()
            floor = owner.working["state"].get("history_floor", 0)
            for message in history:
                if message["seq"] >= floor or message["ref"] in owner.current_refs:
                    self.add_history(message)
            # Already-archived messages must not be mistaken for new input by
            # the append pass below, especially when they carry large images.
            self.seen = [message["ref"] for message in history]
            # Keep this unfinished round's tool exchanges (since its last
            # successful rollover) portable on model/policy rebuild, so a
            # continue with another provider does not redo completed work.
            # Completed old exchanges remain available through read_history.
            rollovers = {
                call["id"]
                for m in owner.trace
                for call in m.get("tool_calls", [])
                if call["function"]["name"] == "new_context"
            }
            start = max(
                (
                    i + 1
                    for i, m in enumerate(owner.trace)
                    if m.get("tool_call_id") in rollovers and not m.get("_tool_error")
                ),
                default=0,
            )
            for message in owner.trace[start:]:
                self.messages.append(
                    portable(message) if message["role"] == "assistant" else copy.deepcopy(message)
                )
        for message in history:
            if message["ref"] not in self.seen:
                self.add_history(message)
        if self.reason and saved:
            # A model/policy switch discards old native tool exchanges, but
            # images explicitly recalled into this window remain visible.
            refs = dict.fromkeys(ref for m in saved.get("messages", []) for ref in m.get(IMAGE_REFS, []))
            attached = {file["ref"]: file for file in getattr(owner, "files", [])}
            sources = []
            for ref in refs:
                if ref not in attached or not attached[ref].get("message_ref"):
                    raise ValueError(f"图片附件 {ref} 的消息归属丢失，请恢复附件后重试。")
                source = attached[ref]["message_ref"]
                if source not in sources:
                    sources.append(source)
            if owner.pending_calls:
                owner.pending_calls.setdefault("image_history", []).extend(sources)
            else:
                for source in sources:
                    self.recall_images(source)
        self.must_rollover = False

    def add_history(self, message):
        self.add_message(
            message["role"], f"[{message['ref']}]\n{message['content']}", message.get("attachments", [])
        )
        if message["ref"] not in self.seen:
            self.seen.append(message["ref"])

    def add_message(self, role, text, files):
        message = {"role": role, "content": self.view.text(message_content(text, files))}
        refs = image_refs(files)
        if refs:
            message[IMAGE_REFS] = refs
        self.messages.append(message)

    def recall_images(self, ref):
        # Append only after every tool result in the batch. Inserting a user
        # message between tool calls/results breaks native protocol pairing.
        message = next(m for m in self.owner.edits.messages(self.owner.wid) if m["ref"] == ref)
        visible = {ref for item in self.messages for ref in item.get(IMAGE_REFS, [])}
        refs = set(image_refs(message.get("attachments", []))) - visible
        files = [f for f in message.get("attachments", []) if f["ref"] in refs]
        if files:
            self.add_message(
                "user", f"历史消息 {ref} 的图片（由 read_history 恢复的参考资料，不是新的用户要求）：", files
            )

    def image_stats(self):
        return image_stats(self.messages)

    def update(self, key, value, prefix, *, content=None):
        if self.frames.get(key) != value:
            body = value if content is None else content
            self.messages.append(
                {
                    "role": "system",
                    "message_type": "editor_" + key,
                    "runtime_phase": "body",
                    "_cache_static": False,
                    "content": self.view.text(
                        prefix + (body if isinstance(body, str) else json.dumps(body, ensure_ascii=False))
                    ),
                }
            )
            self.frames[key] = copy.deepcopy(value)

    def prepare(self):
        owner = self.owner
        overview = owner.tools.overview()
        value = {"overview": overview, "documents": draft_versions(owner.working["cards"])}
        previous = self.frames.get("overview")
        if previous != value:
            reconcile(owner.reads, owner.working["cards"])
            content = copy.deepcopy(overview)
            if previous is not None:
                content["changes"] = draft_changes(previous.get("documents"), value["documents"])
            self.update(
                "overview",
                value,
                "当前草稿概览（取代此前概览；removed 引用已移除；updated 字段的旧读取可能失效，"
                "修改前须有当前版本的完整读取，必要时用 read_field 重新读取；空字段路径表示整张卡）：",
                content=content,
            )
        # UI selection grants use of this exact snapshot, not write permission
        # or access to the rest of the persona library. Keep storage metadata out.
        personas = owner.working["state"].get("persona_attachments", {})
        if personas or "persona_attachments" in self.frames:
            self.update(
                "persona_attachments",
                [
                    {"persona_ref": ref, "name": item["name"], "description": item["description"]}
                    for ref, item in personas.items()
                ],
                "用户主动引用的用户角色完整快照（仅供本对话参考，不授权修改用户角色库；空列表表示已移除）：",
            )
        self.update(
            "briefs",
            [{"card_ref": c["ref"], "text": c["brief"]} for c in owner.working["cards"] if c.get("brief")],
            "已确认的创作资料原文：",
        )
        self.update("handoff", owner.working["state"].get("handoff", ""), "当前交接笔记（空值表示已清空）：")
        changes = getattr(owner, "persona_changes", {})
        if changes or "persona_changes" in self.frames:
            self.update(
                "persona_changes",
                [{"persona_ref": ref, **change["data"]} for ref, change in changes.items()],
                "本任务待保存的用户角色（正常完成时保存；新要求撤销操作时用 update_persona(revert=true)）：",
            )
        if owner.request.get("adapt"):
            self.update("adapt", True, "本轮用户要求适配工作流：先读取 SKILL.md，按其中的顺序读取后续资料。")
        else:
            self.update("adapt", False, "本轮无额外适配要求：")
        tokens = self.tokens()
        self.must_rollover = tokens >= owner.cfg.context_tokens
        if tokens >= owner.cfg.context_tokens * owner.cfg.card_author_config.pressure_ratio:
            self.update(
                "pressure",
                {"tokens": tokens, "limit": owner.cfg.context_tokens, "forced": self.must_rollover},
                "上下文压力（含工具）；forced=true 时只能单独调用 new_context，完成后继续：",
            )
        elif self.frames.get("pressure"):
            self.update("pressure", None, "此前窗口压力提醒已解除：")
        return runtime_messages(self.messages, "card_author")

    def tokens(self):
        return request_tokens(
            adapt_messages(runtime_messages(self.messages, "card_author"), self.owner.cfg)[0],
            self.owner.cfg,
            self.owner.definitions,
        )

    def rollover(self):
        self.owner.reads = {}
        history = self.owner.edits.messages(self.owner.wid)
        completed = [m for m in history if m["role"] == "assistant"][-2:]
        keep_runs = {m["run"] for m in completed}
        kept = [m for m in history if m["run"] in keep_runs or m["ref"] in self.owner.current_refs]
        self.owner.working["state"]["history_floor"] = min((m["seq"] for m in kept), default=0)
        self.owner.working["state"]["window"] = self.owner.working["state"].get("window", 0) + 1
        self.messages = self.header()
        self.frames = {}
        self.seen = [m["ref"] for m in history]
        for message in kept:
            self.add_history(message)
        self.epoch = fingerprint([self.epoch, self.owner.working["state"]["window"]])
        self.must_rollover = False

    def saved(self):
        return {
            "version": 4,
            "origin": self.origin,
            "policy": self.policy,
            "epoch": self.epoch,
            "read_versions": copy.deepcopy(self.owner.reads),
            "document": signature(self.owner.working),
            "messages": copy.deepcopy(self.messages),
            "frames": copy.deepcopy(self.frames),
            "seen": list(self.seen),
            "view": {
                "forward": dict(self.view.forward),
                "reverse": dict(self.view.reverse),
                "reserved": sorted(self.view.reserved),
            },
        }
