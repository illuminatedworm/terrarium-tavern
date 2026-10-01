"""Typed tools over a turn draft; storage identities never become model arguments."""
import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from tavern.character.cards import normalize
from tavern.conversation.context import get_path
from tavern.core.model_view import state_for_model
from tavern.core.tokens import count_text
from tavern.full_agent.config import FullAgentConfig
from tavern.full_agent.state import note_view, rollover, source_hash


class Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Query(Arguments):
    query: str = Field(min_length=1, max_length=1000, description="Search text or retrieval clues.")


class Search(Query):
    limit: int = Field(default=5, ge=1, le=10, description="Maximum number of matches.")


class Read(Arguments):
    ref: str = Field(min_length=1, max_length=80, description="Reference, note key, or character field to read.")


class Checkpoint(Arguments):
    content: str = Field(min_length=1, max_length=64000, description="Complete updated checkpoint text.")


class NewContext(Checkpoint):
    keep_recent: int = Field(default=2, ge=0, le=12, description="Recent dialogue messages to retain, expanded to complete exchanges.")


class Note(Arguments):
    key: str = Field(min_length=1, max_length=80, pattern=r"^[\w\-/ .\u4e00-\u9fff]+$", description="Meaningful, stable note key.")
    content: str = Field(min_length=1, max_length=64000, description="Complete updated note text.")
    kind: Literal["fact", "hypothesis", "plan"] = Field(description="Whether the note records evidence-backed facts, a hypothesis, or a plan.")
    sources: list[str] = Field(default_factory=list, max_length=20, description="H references searched or read in this turn; required for facts.")


class NoteKey(Arguments):
    key: str = Field(min_length=1, max_length=80, description="Existing note key.")


class Patch(Arguments):
    operations: list[dict] = Field(min_length=1, max_length=100, description="JSON Patch operations using JSON Pointer paths.")
    reason: str = Field(min_length=1, max_length=1000, description="Story evidence and purpose of this change.")


class StateRead(Arguments):
    path: str = Field(default="", max_length=200, description="Dot-separated state path; empty reads the entire story state.")


CHECKPOINT_GUIDANCE = (
    "content replaces the entire checkpoint. Build on the latest <tavern_memory> update or the most recent "
    "successful write_checkpoint call, whichever is later; failed proposals are not current memory. Preserve valid scene context, "
    "relationships, user constraints, important experiences, unresolved issues, tone, and useful H references or note keys. "
    "Mark plans as pending and verify stale sources. Draft changes commit with the turn; review mode requires approval."
)
REFERENCE_GUIDANCE = "H/L references are navigation handles, not story prose."
RESULT_FIELDS = {
    "search_lore": {"include_ref": False, "include_title": False},
    "search_history": {"include_preview": True},
    "read_history": {"include_metadata": True},
    "search_notes": {"include_preview": True},
    "read_note": {"include_metadata": True},
    "read_state": {"include_metadata": True},
    "read_character": {"include_metadata": True},
}


def result_field(session, tool, field):
    return session.get("tool_result_options", {}).get(tool, {}).get(field, RESULT_FIELDS[tool][field])


SPECS = {
    "search_lore": (Query, "Search the lorebook. "
        "Use a concise, self-contained query in the lorebook's language to describe what you're looking for. "
        "Entry conditions, cooldowns, relevance thresholds, and limits may filter matches. "
        "Returns complete selected entries in <lorebook>; auto-injected entries already in context are omitted. "
        "Optional result fields may include L references and titles."),
    "search_history": (Search, "Search dialogue in this branch by literal text terms; returns H references and optional previews. "
        "Use short text fragments; query=* lists recent records. Use read_history to verify facts and note sources."),
    "read_history": (Read, "Read original dialogue by H reference, including the current user input. " + REFERENCE_GUIDANCE),
    "write_checkpoint": (Checkpoint, "Update the checkpoint in the current window. " + CHECKPOINT_GUIDANCE),
    "new_context": (NewContext, "Save a checkpoint and switch windows. " + CHECKPOINT_GUIDANCE
        + " Call this tool alone. The current user input stays; keep_recent counts dialogue messages, "
        "expanded to complete exchanges. Old tool exchanges leave the window, so include needed results or references "
        "in the checkpoint. Forced rollover accepts only this tool. Continue the turn after switching; "
        "the model-call budget does not reset."),
    "search_notes": (Search, "Search active notes in this branch by literal text terms in content or keys. "
        "Use short text fragments; query=* lists notes. Notes are not automatically injected. "
        "Use read_note for full text and optional source metadata."),
    "read_note": (Read, "Read an active or archived note by key, with optional kind, H sources, and stale status. "
        "Stale means sources changed or became unavailable; verify them before relying on the note."),
    "write_note": (Note, "Create or fully replace a note by key. Before replacing any existing or archived note, "
        "read its complete current version with read_note in this turn and window; preserve still-valid information. "
        "kind=fact requires supporting H sources searched or read this turn. hypothesis marks uncertainty; plan marks intent. "
        "Attribute character claims and preserve uncertainty where evidence is insufficient. Respect revoked information. "
        "Replacing an archived key reactivates it. Draft changes commit with the turn; review mode requires approval."),
    "archive_note": (NoteKey, "Archive a note so normal searches exclude it; it remains readable by key. "
        "History and older branches remain. Draft changes commit with the turn; review mode requires approval."),
    "read_state": (StateRead, "Read story state, excluding runtime ledgers. An empty path reads all state; "
        "a dot-separated path reads one field. Check values and structure before updating."),
    "patch_state": (Patch, "Call exactly once only when this turn's system message requires an MVU update. "
        "Apply MVU-validated JSON Patch relative to stat_data; omit the /stat_data prefix from paths. "
        "Read current values first; give story evidence in reason and align changes with the final narrative. "
        "Use this tool instead of MVU blocks in prose. Draft changes commit with the turn; review mode requires approval."),
    "revise_content": (Patch, "Apply JSON Patch to this branch's character/lorebook draft, not the library card. "
        "Read fields with read_character first; follow turn authorization and give a reason. "
        "Draft changes commit with the turn; review mode requires approval."),
    "read_character": (Read, "Read a complete top-level character field, such as description, personality, "
        "scenario, character_book, or extensions, for inspection or editing."),
    "set_retrieval_query": (Query, "Save concise next-turn automatic lorebook retrieval clues about the scene, "
        "relevant entities, or unresolved issues. Distinguish anticipated events from facts. "
        "Clues commit with the final narrative and stay outside story prose."),
}


def tool_definitions(request, session, config=None, *, settings=None, catalog=False):
    config = config or FullAgentConfig()
    names = set(SPECS)
    if not catalog:
        names.difference_update(session.get("disabled_tools", []))
    if not catalog and session["policy_effective"] == "directed" and not request.allow_content_edit:
        names.discard("revise_content")
    limits = {
        "write_checkpoint": f" content limit: {config.checkpoint_tokens} tokens.",
        "new_context": f" content limit: {config.checkpoint_tokens} tokens.",
        "write_note": f" content limit: {config.note_tokens} tokens.",
    }
    if settings is not None:
        mechanisms = {
            "keyword": "keyword",
            "bm25": "BM25",
            "embedding": "semantic",
        }
        channels = list(dict.fromkeys(settings.retrieval.channels))
        labels = [mechanisms[c] for c in channels]
        channel_names = labels[0] if len(labels) == 1 else ", ".join(labels[:-1]) + " and " + labels[-1]
        mechanism = " Uses " + channel_names + " recall."
        if settings.local_models.reranker_enabled:
            mechanism += " Reranking orders and filters candidates by relevance."
        limits["search_lore"] = mechanism
    return [{"type": "function", "function": {"name": name, "description": description + limits.get(name, ""),
             "parameters": model.model_json_schema()}} for name, (model, description) in SPECS.items() if name in names]


def encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class AgentTools:
    def __init__(self, owner):
        self.owner = owner
        self.read_sources = {}
        self.note_reads = {}

    def note_read_key(self, note):
        return (self.owner.state["window"]["id"], note["key"], note["revision"])

    def require_note_read(self, note):
        if self.note_read_key(note) not in self.note_reads:
            raise ValueError("更新已有笔记前必须用 read_note 完整读取当前版本，再基于旧内容累积修订")

    def cap_text(self, content, maximum, label):
        if not content.strip() or count_text(content, self.owner.settings) > maximum:
            raise ValueError(f"{label}不能为空，且须在 {maximum} tokens 内；请自行精简")

    def read(self, text, *, wrap=str, **metadata):
        return {**metadata, "text": wrap(text)}

    def archive(self):
        return [{"ref": f"H{i+1}", "role": m["role"], "text": m["raw"], "source": m}
                for i, m in enumerate(self.owner.source_history)]

    def search(self, rows, query, limit, *, include_preview=True):
        terms = set(re.findall(r"\w+", query.casefold()))
        def score(row):
            text = (row.get("key", "") + " " + row["text"]).casefold()
            return sum(term in text for term in terms) + 2 * (query.casefold() in text)
        ranked = sorted(((score(row), i, row) for i, row in enumerate(rows)), key=lambda x: (-x[0], -x[1]))
        results = []
        for rank, _, row in ranked:
            if len(results) >= limit or (not rank and query != "*"):
                break
            text = row["text"]
            at = max(0, min((text.casefold().find(t) for t in terms if t in text.casefold()), default=0) - 80)
            item = {k: v for k, v in row.items() if k not in ("source", "text")}
            if include_preview:
                item["preview"] = text[at:at+400]
            results.append(item)
            if row.get("source"):
                self.read_sources[row["ref"]] = row["source"]
        return {"matches": results, **({"notice": "使用 read 工具读取完整原文。"} if results else {})}

    async def execute(self, name, arguments):
        owner = self.owner
        allowed = {t["function"]["name"] for t in owner.definitions}
        if name not in allowed:
            raise ValueError("当前模式或授权不允许此工具")
        if name == "patch_state":
            if not owner.request.update_variables:
                raise ValueError("本轮未要求更新 MVU 变量，不允许调用 patch_state")
            if owner.patch_state_done:
                raise ValueError("本轮已完成一次 patch_state，不允许重复更新")
        args = SPECS[name][0].model_validate_json(arguments)
        state = owner.state
        if name == "search_lore":
            return await owner.retrieve(args.query, source="agent",
                                        include_ref=result_field(owner.session, name, "include_ref"),
                                        include_title=result_field(owner.session, name, "include_title"))
        if name == "search_history":
            return self.search(self.archive(), args.query, args.limit,
                               include_preview=result_field(owner.session, name, "include_preview"))
        if name == "read_history":
            row = next((r for r in self.archive() if r["ref"] == args.ref), None)
            if row is None:
                raise ValueError("当前分支没有此历史引用")
            metadata = ({"ref": args.ref, "role": row["role"]}
                        if result_field(owner.session, name, "include_metadata") else {})
            result = self.read(row["text"], **metadata)
            if row.get("source"):
                self.read_sources[args.ref] = row["source"]
            return result
        if name in ("write_checkpoint", "new_context"):
            self.cap_text(args.content, owner.settings.full_agent_config.checkpoint_tokens, "checkpoint")
            if name == "new_context":
                rollover(state, owner.history, args.content, args.keep_recent)
                owner.rolled = True
            else:
                state["checkpoint"] = {"text": args.content, "revision": state["checkpoint"]["revision"] + 1, "stale": False}
            state.pop("edited_sources", None)
            return {"saved_to_draft": True, "window": state["window"]["number"],
                    "notice": "正文完成后与本回合统一提交。"}
        if name == "search_notes":
            rows = [{**note_view(n, owner.source_history), "text": n["content"]} for n in state["notes"].values()
                    if n["status"] == "active"]
            for row in rows:
                row.pop("content")
            return self.search(rows, args.query, args.limit,
                               include_preview=result_field(owner.session, name, "include_preview"))
        if name == "read_note":
            note = state["notes"].get(args.ref)
            if note is None:
                raise ValueError("笔记 key 不存在")
            meta = note_view(note, owner.source_history)
            result = self.read(meta.pop("content"),
                               **(meta if result_field(owner.session, name, "include_metadata") else {}))
            self.note_reads[self.note_read_key(note)] = True
            return result
        if name == "write_note":
            self.cap_text(args.content, owner.settings.full_agent_config.note_tokens, "笔记")
            old = state["notes"].get(args.key, {})
            if old:
                self.require_note_read(old)
            if args.kind == "fact" and not args.sources:
                raise ValueError("fact 必须提供已读取的 H 原文依据；尚未确立的内容请用 hypothesis/plan")
            sources = []
            for ref in dict.fromkeys(args.sources):
                source = self.read_sources.get(ref)
                if source is None:
                    raise ValueError("sources 只能引用本回合已搜索或读取的 H 原文；先调用历史工具")
                sources.append({"message_id": source["id"], "hash": source_hash(source)})
            state["notes"][args.key] = {"key": args.key, "content": args.content, "kind": args.kind,
                                       "status": "active", "revision": old.get("revision", 0) + 1, "sources": sources}
            return {"key": args.key, "saved_to_draft": True}
        if name == "archive_note":
            if args.key not in state["notes"]:
                raise ValueError("笔记 key 不存在")
            state["notes"][args.key]["status"] = "archived"
            state["notes"][args.key]["revision"] += 1
            return {"key": args.key, "archived": True}
        if name == "read_state":
            result = get_path(state_for_model(owner.snapshot["state"]), owner.model_view.restore(args.path))
            return self.read(encoded(owner.model_view.expose(result)),
                             **({"path": args.path} if result_field(owner.session, name, "include_metadata") else {}))
        if name == "patch_state":
            result = await owner.script.call("process", "", owner.model_view.operations(args.operations), "")
            owner.snapshot["state"], owner.snapshot["scopes"] = result["state"], result["scopes"]
            owner.patch_state_done = True
            return {"saved_to_draft": True, "notice": "变量已通过 MVU 校验。"}
        if name == "read_character":
            data = owner.model_view.expose(normalize(owner.document))
            if args.ref not in data:
                raise ValueError("角色字段不存在")
            value = data[args.ref]
            return self.read(value if isinstance(value, str) else encoded(value),
                             **({"ref": args.ref} if result_field(owner.session, name, "include_metadata") else {}))
        if name == "revise_content":
            from tavern.character.card_workflow import validate_document
            revised = owner.model_view.patch(owner.document, args.operations)
            validate_document(revised)
            owner.document = revised
            owner.snapshot["content"] = {"document": revised, "reason": args.reason, "turn": owner.tid}
            return {"saved_to_draft": True}
        if name == "set_retrieval_query":
            owner.retrieval_query = args.query
            return {"saved_to_draft": True}
        raise ValueError("未知工具")
