"""Append-only editor requests with explicit model-controlled context rollover."""

import copy
import json

from tavern.character.editor_attachments import message_content
from tavern.character.editor_cache import FINAL_REF_PREFIX, follows_replies, reply_breakpoint
from tavern.character.editor_documents import DocumentDirectory
from tavern.character.editor_draft_state import draft_changes, draft_versions
from tavern.character.editor_environment import environment_message
from tavern.character.editor_guides import SKILL_ENTRIES, guide_text, scoped_text
from tavern.character.editor_images import IMAGE_REFS, image_refs, image_stats
from tavern.character.editor_reads import reconcile
from tavern.core.cache_policy import fingerprint, shares_editor_cloud_prefix
from tavern.core.context_usage import context_usage, pressure_tokens
from tavern.core.message_adapter import adapt_messages, runtime_messages
from tavern.core.model_view import HANDLE, ModelView
from tavern.scripting.native_response import portable
from tavern.scripting.request_context import response_origin


def signature(working):
    return fingerprint(
        {
            "cards": [{k: c.get(k) for k in ("ref", "document", "brief")} for c in working["cards"]],
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
        messages = [self.system_message()]
        if self.owner.environment:
            messages.append(
                environment_message(
                    self.owner.environment, header=not shares_editor_cloud_prefix(self.owner.cfg)
                )
            )
        return messages

    def __init__(self, saved, owner):
        self.owner = owner
        self.directory = DocumentDirectory(owner, saved.get("directory") if saved else None)
        self.version_cache = {}
        self.origin = response_origin(owner.cfg)
        self.refresh_tool_policy()
        history = owner.edits.messages(owner.wid)
        self.reply_refs = [message["ref"] for message in history if message["role"] == "assistant"]
        seed = {
            "documents": [c["document"] for c in owner.working["cards"]],
            "history": [m["content"] for m in history],
        }
        self.reason = None
        if not saved or saved.get("version") != 5:
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
        if saved and saved.get("version") in (2, 3, 4, 5) and self.reason != "literal_handle_collision":
            self.view.forward = copy.deepcopy(saved["view"]["forward"])
            self.view.forward.pop("post_history_instructions", None)
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
                    "documents": self.versions(owner.working["cards"]),
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
        content = scoped_text(message["content"], message.get("target_labels", []), message.get("skill"))
        self.add_message(message["role"], f"[{message['ref']}]\n{content}", message.get("attachments", []))
        if message["ref"] not in self.seen:
            self.seen.append(message["ref"])
        if (
            message["role"] == "assistant"
            and follows_replies(self.owner.cfg)
            and self.owner.cache_follow["mode"] != "fixed"
        ):
            self.final_reply(message["ref"])

    def final_reply(self, ref):
        self.update("final_ref", ref, FINAL_REF_PREFIX)

    def add_message(self, role, text, files):
        message = {"role": role, "content": self.view.text(message_content(text, files))}
        refs = image_refs(files)
        if refs:
            message[IMAGE_REFS] = refs
        if role == "user" and len(self.messages) == 2:
            environment = self.messages[1]
            if (
                environment.get("message_type") == "editor_environment"
                and environment["runtime_phase"] == "body"
            ):
                # End the initial developer group before the conversation's
                # timestamp, so OpenAI can implicitly reuse the shared rules.
                self.messages.insert(1, message)
                return
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

    def update(self, key, value, prefix, *, content=None, trusted=False):
        if self.frames.get(key) != value:
            body = value if content is None else content
            text = prefix + (body if isinstance(body, str) else json.dumps(body, ensure_ascii=False))
            self.messages.append(
                {
                    "role": "system",
                    "message_type": "editor_" + key,
                    "runtime_phase": "body",
                    "_cache_static": False,
                    "content": self.view.protocol_text(text) if trusted else self.view.text(text),
                }
            )
            self.frames[key] = copy.deepcopy(value)

    def versions(self, cards):
        refs = {c["ref"] for c in cards}
        self.directory.prune(refs)
        for ref in self.version_cache.keys() - refs:
            del self.version_cache[ref]
        versions = {}
        for card in cards:
            ref = card["ref"]
            cached = self.version_cache.get(ref)
            if not cached or cached[0] is not card["document"]:
                self.directory.index(card)
                record = draft_versions([card])[ref]
                record["nodes"] = {k: [v[1], v[3]] for k, v in self.directory.state["cards"][ref].items()}
                cached = self.version_cache[ref] = card["document"], record
            versions[ref] = cached[1]
        return versions

    def prepare(self):
        owner = self.owner
        overview = owner.tools.overview()
        cards = owner.working["cards"]
        versions = self.versions(cards)
        value = {"overview": overview, "documents": versions}
        previous = self.frames.get("overview")
        if previous != value:
            reconcile(owner.reads, owner.working["cards"])
            content = copy.deepcopy(overview)
            if previous is not None:
                changes = draft_changes(previous.get("documents"), versions)
                prior_rows = {c["ref"]: c for c in previous.get("overview", {}).get("cards", [])}
                edited = {row["card_ref"] for row in changes["updated"]}
                content = {
                    "cards": [
                        c for c in overview["cards"] if c["ref"] in edited or c != prior_rows.get(c["ref"])
                    ],
                    "changes": changes,
                    "active_refs": list(versions),
                }
            self.update(
                "overview",
                value,
                "当前草稿概览（首次列出全部卡片，后续只追加变化的卡；active_refs 为当前引用。removed 已移除，updated 可能需要重读。"
                "目录与摘录只供定位；用 list_documents、search_documents 找文档，read_documents 读取原文）：",
                content=content,
            )
        # UI selection grants use of this exact snapshot, not write permission
        # or access to the rest of the persona library. Keep storage metadata out.
        personas = owner.working["state"].get("persona_attachments", {})
        if personas or "persona_attachments" in self.frames:
            self.update(
                "persona_attachments",
                {ref: item["version"] for ref, item in personas.items()},
                "当前用户角色引用（取代此前列表；按需用 read_persona_reference 读取完整快照。"
                "重新引用后需重读；空列表表示无引用；引用不授权修改用户角色库）：",
                content=[{"persona_ref": ref, "name": item["name"]} for ref, item in personas.items()],
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
        skill = owner.request.get("skill")
        if skill:
            title, resource = SKILL_ENTRIES[skill]
            self.update(
                "adapt",
                skill,
                f"本条消息选用{title}。内置指引 {resource} 全文：\n",
                content=guide_text(resource),
                trusted=True,
            )
        elif owner.request.get("adapt"):
            self.update("adapt", True, "本轮用户要求适配工作流：先读取 SKILL.md，按其中的顺序读取后续资料。")
        else:
            self.update("adapt", False, "本轮无额外适配要求：")
        measured = len(self.messages)
        self.last_usage = self.usage()
        tokens = pressure_tokens(self.last_usage)
        previous = self.frames.get("pressure") or {}
        self.must_rollover = (
            previous.get("forced", False)
            and previous.get("limit") == owner.cfg.context_tokens
            or tokens is not None
            and tokens >= owner.cfg.context_tokens
        )
        if (
            self.must_rollover
            or tokens is not None
            and tokens >= owner.cfg.context_tokens * owner.cfg.card_author_config.pressure_ratio
        ):
            self.update(
                "pressure",
                {"tokens": tokens, "limit": owner.cfg.context_tokens, "forced": self.must_rollover},
                "窗口依据上次实测输入触发；tokens=null 表示当前无可用实测，新增内容尚未计量。forced=true 时只能单独调用 new_context，完成后继续：",
            )
        elif self.frames.get("pressure"):
            self.update("pressure", None, "此前窗口压力提醒已解除：")
        if len(self.messages) != measured:
            # A pressure frame changes the request about to be sent; the stats
            # event reports that request rather than the one measured above.
            self.last_usage = self.usage()
        messages = runtime_messages(self.messages, "card_author")
        self.cache_breakpoint = reply_breakpoint(owner.cfg, owner.cache_follow, self.reply_refs, messages)
        index = self.cache_breakpoint.get("index")
        if index is not None:
            messages[index].update(
                _cache_breakpoint=True,
                _cache_message_id=self.cache_breakpoint["reply_ref"],
                _cache_source="editor_final_ref",
            )
        return messages

    def usage(self):
        self.owner.cfg._context_generation = self.epoch
        return context_usage(
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
            "version": 5,
            "directory": copy.deepcopy(self.directory.state),
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
