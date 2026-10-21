"""Typed tools over a turn draft; storage identities never become model arguments."""

import json
import re
from typing import Annotated, Any, Literal

import jsonpatch
import jsonpointer
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from tavern.core.history_refs import message_at, message_ref, parse_span_ref
from tavern.core.model_view import state_for_model
from tavern.core.tokens import count_text
from tavern.full_agent.argument_errors import ToolArgumentError
from tavern.full_agent.config import FullAgentConfig
from tavern.full_agent.content import CharacterRead, ContentEditor, ContentRevision, LoreRead, entry_view
from tavern.full_agent.handoff import (
    HandoffError,
    HandoffWrite,
    apply_write,
    chapter_index,
    check_limits,
    require_coverage,
)
from tavern.full_agent.protocol import ToolError, TurnError, success
from tavern.full_agent.read_guards import DraftReads, state_scope
from tavern.full_agent.state import note_view, rollover, rollover_start
from tavern.scripting.runtime import RuntimeRollbackError
from tavern.tracker.state import InvalidStatePatch, InvalidStatePath, ProtectedStateField, stage


class Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Query(Arguments):
    query: str = Field(min_length=1, max_length=1000, description="Search text or retrieval clues.")


class RetrievalQuery(Arguments):
    query: str = Field(
        max_length=1000,
        description="Replace next-turn retrieval clues; empty string clears this turn's clues.",
    )


class Search(Query):
    limit: int = Field(default=5, ge=1, le=10, description="Maximum number of matches.")


class Read(Arguments):
    ref: str = Field(
        min_length=1, max_length=80, description="Reference, note key, or character field to read."
    )


HISTORY_SPAN = 8


class HistoryRead(Read):
    ref: str = Field(
        min_length=1,
        max_length=80,
        description=f"H reference or closed span from search_history, retained context or handoff, e.g. H1 or H52-H58; at most {HISTORY_SPAN} messages per span, all in the current branch.",
    )


class NoteRead(Arguments):
    key: str = Field(
        min_length=1,
        max_length=80,
        description="Exact existing note key, e.g. relationships/guide; accepts active or archived notes, not H/L references.",
    )


class Plot(Arguments):
    content: str = Field(
        min_length=1,
        max_length=64000,
        description="Complete replacement author-level stage outline; future possibilities, not established story facts. Must contain non-whitespace text.",
    )


class NewContext(Arguments):
    handoff: HandoffWrite | None = Field(
        default=None,
        description="Optional update; omit or pass null to reuse the current handoff unchanged. Only messages actually leaving after keep_recent expands to whole exchanges need chapter coverage. When updating, provide chapter operations and the complete current handoff.",
    )
    keep_recent: int = Field(
        default=2,
        ge=0,
        le=12,
        description="Recent dialogue messages to retain (0-12; default 2), normally up to 6 user/assistant exchanges. Expanded to complete exchanges; current user input is retained separately.",
    )


class Note(Arguments):
    key: str = Field(
        min_length=1,
        max_length=80,
        pattern=r"^[\w\-/ .\u4e00-\u9fff]+$",
        description="Meaningful, stable note key.",
    )
    content: str = Field(
        min_length=1,
        max_length=64000,
        description="Complete updated note text; must contain non-whitespace text.",
    )
    kind: Literal["fact", "hypothesis", "plan"] = Field(
        description="Whether the note records evidence-backed facts, a hypothesis, or characters' in-story intentions. Author-level future outlines belong to plot, not kind=plan."
    )
    sources: list[str] = Field(
        default_factory=list,
        max_length=20,
        description="Valid H references in this branch, including the current user input; required for facts. No prior tool read or search is required.",
    )


class NoteKey(Arguments):
    key: str = Field(min_length=1, max_length=80, description="Existing note key.")


PATCH_GUIDANCE = (
    "JSON Patch objects. Each requires op and path. "
    "op is add, remove, replace, move, copy, or test. add/replace/test require value (any JSON value, including null); "
    "move/copy require from (source JSON Pointer); remove needs only op/path. "
    "path/from use slash-separated JSON Pointers, not dot paths: escape ~ as ~0 and / as ~1 within keys. "
    "Array indices are zero-based; add may use a final /- to append. "
    "An empty pointer targets the root. Follow this tool's root rules; existing targets are required for "
    "remove/replace/test and from, and add requires an existing parent."
)


class ValueOperation(Arguments):
    op: Literal["add", "replace", "test"] = Field(description="Operation that supplies a JSON value.")
    path: str = Field(description="Target JSON Pointer relative to stat_data.")
    value: Any = Field(description="JSON value, including null.")


class FromOperation(Arguments):
    op: Literal["move", "copy"] = Field(description="Operation that reads another location.")
    path: str = Field(description="Target JSON Pointer relative to stat_data.")
    from_: str = Field(alias="from", description="Source JSON Pointer relative to stat_data.")


class RemoveOperation(Arguments):
    op: Literal["remove"] = Field(description="Remove an existing value.")
    path: str = Field(description="Target JSON Pointer relative to stat_data.")


StateOperation = Annotated[ValueOperation | FromOperation | RemoveOperation, Field(discriminator="op")]


class StatePatch(Arguments):
    operations: list[StateOperation] = Field(
        max_length=100, description=PATCH_GUIDANCE + " Empty array means no changes."
    )
    comment: str = Field(
        min_length=1, max_length=1000, description="Brief user-facing comment about this operation."
    )


class StateRead(Arguments):
    path: str = Field(
        default="",
        max_length=200,
        description="JSON Pointer relative to the selected scope, e.g. /hp or /items/0/name. Escape ~ as ~0 and / as ~1 in keys. Empty reads the complete scope; missing paths return null.",
    )
    scope: Literal["mvu", "full", "other"] = Field(
        default="mvu",
        description="mvu reads stat_data, matching patch_state paths. full reads all model-visible runtime state, including stat_data. other reads that state with the top-level stat_data field excluded. Internal bookkeeping is hidden in every scope.",
    )

    @field_validator("scope", mode="before")
    @classmethod
    def legacy_scope(cls, value):
        return state_scope(value)


RESULT_FIELDS = {
    "recall_lore": {"include_ref": False, "include_title": False},
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
    "find_lore_for_edit": (
        Search,
        "Locate ordinary narrative worldbook entries for editing; use recall_lore for narrative recall. "
        "Search literal title, keywords or text, including disabled ordinary entries; query=* lists up to limit results. "
        "Returns L handles, titles and navigation previews. Previews help locate targets; use read_lore_entry when you need to inspect or refresh a target's complete contents.",
    ),
    "read_lore_entry": (
        LoreRead,
        "Inspect or refresh one complete ordinary narrative worldbook entry by L handle: title, content, keys, secondary_keys, enabled. "
        "Protected scripts, variable rules and initialization are unavailable.",
    ),
    "recall_lore": (
        Query,
        "Recall complete worldbook sources for narrative writing; use find_lore_for_edit to locate editing targets. "
        "Use a concise, self-contained query in the worldbook's language. Entry conditions, cooldowns, relevance thresholds and limits may yield no matches. "
        "Returns complete selected entries in <lorebook>; text already injected or recalled in this context is omitted. "
        "Recalled entries share automatic injection's presence and cooldown rules. "
        "When enabled, results include titles and L handles. Reuse recalled content for later edits when you know it is still current; target protection still applies.",
    ),
    "search_history": (
        Search,
        "Search dialogue in this branch by literal text terms; returns H references and optional previews. "
        "Use short text fragments; query=* lists recent records. Use read_history for complete sources. Old tool exchanges are not searchable.",
    ),
    "read_history": (
        HistoryRead,
        "Read complete original dialogue by a valid H reference or closed span, including the current user input; a span returns its messages complete and in branch order. "
        "H references are navigation handles, not story prose.",
    ),
    "write_handoff": (
        HandoffWrite,
        "Update the handoff in the current window without switching: optional chapter operations plus the complete current handoff. "
        "Build on the latest <tavern_memory> or successful write, whichever is later; verify points marked as changed sources. "
        "Chapters are an append-only ledger of what still matters later (irreversible events, relationship turns, promises and debts, secrets and who knows them, recurring people, items and places). Normally omit through: Runtime appends a chapter over the unarchived committed history. Choose a known endpoint only for intentional chapter splits. Drop one-off actions, scenery and dialogue texture; chapter ranges remain readable with read_history. Point refs are optional: cite known history only when useful for precise follow-up, never invent a citation for instructions or character material. Do not rewrite written chapters; revise only to correct one, merge the oldest adjacent chapters when the total limit is near. current fully replaces the current handoff: scene, relationships, constraints, unresolved issues and tone, without retelling chapter events. Do not copy complete notes or the future plot outline.",
    ),
    "new_context": (
        NewContext,
        "Switch windows, optionally updating the handoff. Omit handoff or pass null to reuse the latest saved or successfully written handoff unchanged. "
        "Choose keep_recent first: chapter coverage is required only for messages actually leaving after expanding the retained tail to complete exchanges. Retained messages and the current input need no new chapter; when all history stays, an empty handoff is allowed. "
        "If updating, add any missing chapters and give the complete current handoff. Normally omit through on add: Runtime assigns the remaining exiting range. Choose a known endpoint only for intentional chapter splits. Keep only lasting events, relationship changes, promises, secrets and recurring people, items and places; chapter ranges remain readable with read_history. Point refs are optional, including in current; use only known references when precise follow-up helps. Do not rewrite written chapters; revise only to correct one, merge the oldest adjacent chapters when the total limit is near. current fully replaces the current handoff: scene, relationships, constraints, unresolved issues and tone, without retelling chapter events. Do not copy complete notes or the future plot outline. "
        "Call alone: a mixed batch is rejected without executing any tools. keep_recent counts dialogue messages, expanded to complete exchanges; the current input stays separately. "
        "Old tool exchanges leave the window. Continue this turn after switching; the model-call budget does not reset.",
    ),
    "search_notes": (
        Search,
        "Search active notes in this branch by literal terms in content or keys; query=* lists notes. "
        "Returns navigation previews when enabled. Note contents are not automatically injected; use read_note for complete text.",
    ),
    "read_note": (
        NoteRead,
        "Read a complete active or archived note by key, with optional kind and H sources.",
    ),
    "write_note": (
        Note,
        "Create or fully replace a note by key; preserve still-valid information. Build on the current contents you already know, including text you wrote in earlier tool calls. Use read_note when you do not know the note's current contents or are unsure whether your version is still current. New keys need no read. "
        "kind=fact requires supporting H sources valid in this branch; hypothesis marks uncertainty and plan marks characters' in-story intentions, not an author-level future outline. "
        "Preserve valid experiences and changes, not just a current-state snapshot. Attribute character claims, preserve uncertainty and respect revoked information. Replacing an archived key reactivates it.",
    ),
    "write_plot": (
        Plot,
        "Create or fully replace this branch's rolling author-level stage outline. Available only when plot planning is enabled. "
        "The current complete outline is supplied automatically; no prior read, source references or version argument is required. "
        "When empty, save an initial outline before the final story reply. Otherwise revise when stages finish, turning points occur, the player diverges or premises change; no per-turn rewrite is required. "
        "Keep near stages concrete and distant stages broad, with possible events, turns, foreshadowing and conditions. These are revisable possibilities, not established facts, character knowledge or predetermined player actions. "
        "Player edits remain an editable draft. Success saves to the turn draft; failure preserves the old outline.",
    ),
    "archive_note": (
        NoteKey,
        "Archive a note so normal searches exclude it; it remains readable by key. "
        "Use what you already know about the note to decide whether to archive it. Use read_note if you do not know its current contents or need to check them. This does not delete the note or older history. write_note with the same key reactivates it.",
    ),
    "read_state": (
        StateRead,
        "Inspect or refresh runtime state using JSON Pointer relative to the selected scope. Default scope=mvu reads stat_data with the same relative paths as patch_state, e.g. /hp. "
        "scope=full reads all model-visible runtime state, including stat_data; scope=other reads that state excluding the top-level stat_data field. Empty path reads the complete selected scope. Internal ledgers remain hidden in every scope. "
        'To refresh all MVU variables for patching, use path="", scope="mvu".',
    ),
    "patch_state": (
        StatePatch,
        "When variable updates are authorized, submit exactly one successful patch this turn; validation failures may be corrected and retried and do not count as success. "
        'Work from the current MVU values you already know, including your earlier tool calls and their successful results. If you do not know the current values or are unsure whether they are still current, use read_state(path="", scope="mvu") to refresh them. '
        "Apply MVU-validated JSON Patch relative to stat_data, e.g. /hp, not /stat_data/hp. "
        "Follow the latest <mvu_rules> and align with the final narrative. Include relevant story evidence in a brief user-facing comment. "
        "Submit operations=[] to confirm no changes; this needs no read, changes no variables and consumes the one successful submission. Use this tool instead of MVU blocks in prose.",
    ),
    "revise_content": (
        ContentRevision,
        "Edit narrative settings with set_character, add_lore, update_lore or remove_lore; use these semantic actions, not JSON Patch or storage IDs. "
        "Work from the current contents you already know, including supplied settings, tool results and text you wrote in earlier tool calls. Use read_character or read_lore_entry when you do not know the target's current contents or are unsure whether your version is still current. Adding a new entry needs no read. "
        "Success returns complete final contents of changed targets, which you can reuse for later edits. "
        "Omitted properties remain intact. Script-bearing targets and execution settings are protected; failed batches change nothing.",
    ),
    "read_character": (
        CharacterRead,
        "Inspect or refresh one complete setting: name, description, personality, scenario, mes_example, system_prompt or post_history_instructions. "
        "Missing fields return null. Script-bearing fields are protected.",
    ),
    "set_retrieval_query": (
        RetrievalQuery,
        "Replace this turn's clues for next-turn automatic worldbook retrieval; the last successful call wins. "
        'query="" clears these clues without erasing clues saved by earlier turns. Describe relevant scenes, entities or unresolved issues, distinguishing anticipated events from facts. Clues stay outside narrative prose.',
    ),
}


def tool_definitions(request, session, config=None, *, settings=None, catalog=False, document=None):
    config = config or FullAgentConfig()
    names = set(SPECS)
    if session.get("context_strategy") == "openai_compact":
        names.difference_update({"new_context", "write_handoff"})
    if not catalog:
        names.difference_update(session.get("disabled_tools", []))
        # This capability has one player-facing switch, not two conflicting ones.
        if session.get("plot_enabled", False):
            names.add("write_plot")
        else:
            names.discard("write_plot")
    if not catalog and not request.allow_content_edit:
        names.discard("revise_content")
    limits = {
        "write_handoff": f" Limits: current {config.handoff_tokens} tokens, each chapter {config.chapter_tokens}, all chapters {config.chapters_total_tokens}.",
        "new_context": f" Limits: current {config.handoff_tokens} tokens, each chapter {config.chapter_tokens}, all chapters {config.chapters_total_tokens}.",
        "write_note": f" content limit: {config.note_tokens} tokens.",
        "write_plot": f" content limit: {config.plot_tokens} tokens.",
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
        limits["recall_lore"] = mechanism
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": description + limits.get(name, ""),
                "parameters": model.model_json_schema(),
            },
        }
        for name, (model, description) in SPECS.items()
        if name in names
    ]


def encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class AgentTools:
    def __init__(self, owner):
        self.owner = owner
        self.reads = DraftReads(owner)
        self.content = ContentEditor(owner, self.reads)

    def cap_text(self, content, maximum, label):
        if not content.strip() or count_text(content, self.owner.settings) > maximum:
            raise ToolError("CONTENT_LIMIT", f"{label}不能为空，且须在 {maximum} tokens 内；请自行精简")

    def read(self, text, *, wrap=str, **metadata):
        return {**metadata, "text": wrap(text)}

    def archive(self):
        return [
            {"ref": message_ref(position), "role": m["role"], "text": m["raw"]}
            for position, m in enumerate(self.owner.source_history, 1)
        ]

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
            item = {k: v for k, v in row.items() if k != "text"}
            if include_preview:
                item["preview"] = text[at : at + 400]
            results.append(item)
        return {"matches": results, **({"notice": "使用 read 工具读取完整原文。"} if results else {})}

    async def execute(self, name, arguments):
        # One boundary covers every successful result, including free-form source text.
        return success(self.owner.model_view.expose(await self._execute(name, arguments)))

    async def _execute(self, name, arguments):
        owner = self.owner
        if name not in SPECS:
            raise ToolError("UNKNOWN_TOOL", f"工具不存在：{name}；请使用本轮提供的工具名称")
        allowed = {t["function"]["name"] for t in owner.definitions}
        if name not in allowed:
            raise ToolError("PERMISSION_DENIED", "当前模式或授权不允许此工具")
        if name == "patch_state":
            if not owner.request.update_variables:
                raise ToolError("PERMISSION_DENIED", "本轮未要求更新 MVU 变量，不允许调用 patch_state")
            if owner.patch_state_done:
                raise ToolError("STATE_ALREADY_SUBMITTED", "本轮已完成一次 patch_state，不允许重复更新")
        model = SPECS[name][0]
        try:
            args = model.model_validate_json(arguments)
        except ValidationError as error:
            raise ToolArgumentError(name, model, error) from None
        state = owner.state
        if name == "find_lore_for_edit":
            rows = [
                {
                    "ref": row["ref"],
                    "title": row["title"],
                    "text": row["title"]
                    + " "
                    + " ".join(row["keys"] + row["secondary_keys"])
                    + " "
                    + row["content"],
                }
                for row in self.content.rows()
            ]
            result = self.search(rows, args.query, args.limit)
            result["notice"] = "preview 仅用于定位；需要了解或核对目标完整内容时，使用 read_lore_entry。"
            return owner.model_view.expose(result)
        if name == "read_lore_entry":
            _, raw, entry = self.content.resolve(args.ref)
            result = owner.model_view.expose({"ref": args.ref, **entry_view(entry)})
            self.reads.read_entry(args.ref, raw)
            return result
        if name == "recall_lore":
            return await owner.retrieve(
                args.query,
                source="agent",
                include_ref=result_field(owner.session, name, "include_ref"),
                include_title=result_field(owner.session, name, "include_title"),
            )
        if name == "search_history":
            return self.search(
                self.archive(),
                args.query,
                args.limit,
                include_preview=result_field(owner.session, name, "include_preview"),
            )
        if name == "read_history":
            rows = self.archive()
            span = parse_span_ref(args.ref)
            if span is None or span[1] > len(rows):
                raise ToolError("INVALID_REFERENCE", "当前分支没有此历史引用或区间")
            if span[1] - span[0] >= HISTORY_SPAN:
                raise ToolError(
                    "INVALID_ARGUMENTS",
                    f"一次最多读取 {HISTORY_SPAN} 条原文；请拆成多个不超过 {HISTORY_SPAN} 条的区间",
                )
            include = result_field(owner.session, name, "include_metadata")
            # Every message of a span is complete and in branch order; the
            # user's metadata switch applies to each, as to a single read.
            selected = [
                self.read(row["text"], **({"ref": row["ref"], "role": row["role"]} if include else {}))
                for row in rows[span[0] - 1 : span[1]]
            ]
            return {"messages": selected} if span[0] < span[1] else selected[0]
        if name in ("write_handoff", "new_context"):
            # Handoff cites committed originals; the current input is citable after this turn.
            history = owner.history
            archive_through = (
                rollover_start(state, history, args.keep_recent) if name == "new_context" else len(history)
            )
            write = args.handoff if name == "new_context" else args
            handoff = state["handoff"]
            try:
                if write is not None:
                    handoff = apply_write(handoff, write, history, archive_through=archive_through)
                    check_limits(handoff, history, owner.settings.full_agent_config, owner.settings)
                if name == "new_context":
                    require_coverage(handoff, history, archive_through)
            except HandoffError as error:
                raise ToolError(error.code, str(error)) from error
            if name == "new_context":
                rollover(state, history, handoff if write is not None else None, args.keep_recent)
                owner.rolled = True
            else:
                state["handoff"] = {**handoff, "revision": state["handoff"]["revision"] + 1}
            return {
                "saved_to_draft": True,
                "window": state["window"]["number"],
                # Current C numbering, so a later call in this window can revise or merge.
                "chapters": chapter_index(handoff, history),
                "notice": "正文完成后与本回合统一提交。",
            }
        if name == "write_plot":
            self.cap_text(args.content, owner.settings.full_agent_config.plot_tokens, "剧情规划")
            state["plot"] = {"text": args.content, "revision": state.get("plot", {}).get("revision", 0) + 1}
            return {"saved_to_draft": True}
        if name == "search_notes":
            rows = [
                {**note_view(n, owner.source_history), "text": n["content"]}
                for n in state["notes"].values()
                if n["status"] == "active"
            ]
            for row in rows:
                row.pop("content")
                row.pop("revision")
            return self.search(
                rows,
                args.query,
                args.limit,
                include_preview=result_field(owner.session, name, "include_preview"),
            )
        if name == "read_note":
            note = state["notes"].get(args.key)
            if note is None:
                raise ToolError("INVALID_REFERENCE", "笔记 key 不存在")
            meta = note_view(note, owner.source_history)
            meta.pop("revision")
            self.reads.read_note(args.key)
            return self.read(
                meta.pop("content"), **(meta if result_field(owner.session, name, "include_metadata") else {})
            )
        if name == "write_note":
            self.cap_text(args.content, owner.settings.full_agent_config.note_tokens, "笔记")
            self.reads.require_note(args.key)
            old = state["notes"].get(args.key, {})
            if args.kind == "fact" and not args.sources:
                raise ToolError(
                    "INVALID_ARGUMENTS",
                    "fact 必须提供当前分支有效的 H 原文依据；尚未确立的内容请用 hypothesis/plan",
                )
            sources = []
            for ref in dict.fromkeys(args.sources):
                source = message_at(owner.source_history, ref)
                if source is None:
                    raise ToolError(
                        "INVALID_REFERENCE", "sources 包含当前分支无效的历史引用；请使用有效的 H 引用"
                    )
                sources.append({"message_id": source["id"]})
            state["notes"][args.key] = {
                "key": args.key,
                "content": args.content,
                "kind": args.kind,
                "status": "active",
                "revision": old.get("revision", 0) + 1,
                "sources": sources,
            }
            # The full replacement is already present in the model's tool call.
            self.reads.read_note(args.key)
            return {"key": args.key, "saved_to_draft": True}
        if name == "archive_note":
            if args.key not in state["notes"]:
                raise ToolError("INVALID_REFERENCE", "笔记 key 不存在")
            self.reads.require_note(args.key)
            state["notes"][args.key]["status"] = "archived"
            state["notes"][args.key]["revision"] += 1
            return {"key": args.key, "archived": True}
        if name == "read_state":
            try:
                path = owner.model_view.pointer(args.path, restore=True)
            except jsonpointer.JsonPointerException as error:
                raise ToolError("INVALID_PATH", str(error)) from None
            projected = state_for_model(owner.snapshot["state"])
            if args.scope == "mvu":
                root = projected.get("stat_data", {})
            elif args.scope == "other":
                root = {key: value for key, value in projected.items() if key != "stat_data"}
            else:
                root = projected
            value = jsonpointer.resolve_pointer(root, path, default=None)
            result = self.read(
                encoded(owner.model_view.expose(value)),
                **(
                    {"path": args.path, "scope": args.scope}
                    if result_field(owner.session, name, "include_metadata")
                    else {}
                ),
            )
            if path == "" and args.scope == "mvu" or args.scope == "full" and path in ("", "/stat_data"):
                self.reads.read_state()
            return result
        if name == "patch_state":
            if args.operations:
                self.reads.require_state()
                try:
                    operations = owner.model_view.operations(
                        [op.model_dump(by_alias=True) for op in args.operations]
                    )
                    # Share only the pure variable projection validator, not Tracker
                    # orchestration. It preserves hidden parent metadata and maps
                    # visible array indices before any MVU callback can execute.
                    operations, _ = stage(owner.snapshot["state"].get("stat_data", {}), operations)
                except ProtectedStateField as error:
                    raise ToolError(
                        "PROTECTED_TARGET",
                        str(error),
                        next_action="仅修改读取得到的业务变量，内部字段由程序维护。",
                    ) from None
                except (
                    jsonpointer.JsonPointerException,
                    jsonpatch.JsonPatchConflict,
                    InvalidStatePath,
                ) as error:
                    raise ToolError("INVALID_PATH", str(error)) from None
                except (jsonpatch.InvalidJsonPatch, KeyError, TypeError) as error:
                    raise ToolError("INVALID_ARGUMENTS", str(error)) from None
                except jsonpatch.JsonPatchTestFailed:
                    # The library exception embeds complete compared values,
                    # including metadata the model was deliberately not shown.
                    raise ToolError(
                        "PATCH_VALIDATION_FAILED", "补丁的 test 条件未满足；请核对当前变量和预期值。"
                    ) from None
                except (jsonpatch.JsonPatchException, InvalidStatePatch) as error:
                    raise ToolError("PATCH_VALIDATION_FAILED", str(error)) from None
                before = await owner.script.call("snapshot")
                try:
                    result = await owner.script.call("process", "", operations, "")
                except RuntimeRollbackError as error:
                    raise TurnError(
                        "SCRIPT_RESTORE_FAILED", "变量工具失败且运行时恢复失败，停止回合；草稿未提交"
                    ) from error
                except Exception as error:
                    try:
                        await owner.script.call("update", before)
                    except Exception as restore_error:
                        raise TurnError(
                            "SCRIPT_RESTORE_FAILED", "变量工具失败且运行时恢复失败，停止回合；草稿未提交"
                        ) from restore_error
                    # Script exceptions can contain snapshots of private runtime
                    # fields. Keep the diagnostic out of the model tool result.
                    raise ToolError(
                        "PATCH_VALIDATION_FAILED", "MVU 或脚本校验失败；本次变量补丁已回滚。"
                    ) from error
                owner.snapshot["state"], owner.snapshot["scopes"] = result["state"], result["scopes"]
            owner.patch_state_done = True
            return {
                "saved_to_draft": True,
                "patch_state_completed": True,
                "notice": "变量已通过 MVU 校验。"
                if args.operations
                else "已确认本轮变量无变化；未执行变量补丁或脚本回调。",
            }
        if name == "read_character":
            value = self.content.character(args.ref)
            result = owner.model_view.expose(
                {
                    "text": value,
                    **(
                        {"ref": args.ref, "exists": value is not None}
                        if result_field(owner.session, name, "include_metadata")
                        else {}
                    ),
                }
            )
            self.reads.read_character(args.ref)
            return result
        if name == "revise_content":
            return self.content.revise(args)
        if name == "set_retrieval_query":
            owner.retrieval_query = args.query
            return {"saved_to_draft": True}
        raise ToolError("UNKNOWN_TOOL", "未知工具")
