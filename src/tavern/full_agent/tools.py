"""Typed tools over a turn draft; storage identities never become model arguments."""

import json
import re
from typing import Annotated, Any, Literal

import jsonpatch
import jsonpointer
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from tavern.core.history_refs import SOURCE_REFERENCE_LIMIT, message_at, message_ref, parse_span_ref
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
        description="Complete updated author-level stage outline, built on the current one; future possibilities, not established story facts. Must contain non-whitespace text.",
    )


class NewContext(Arguments):
    handoff: HandoffWrite | None = Field(
        default=None,
        description="Omit or pass null to keep the current handoff. When given, it has write_handoff's format and current is the complete replacement.",
    )
    keep_recent: int = Field(
        default=2,
        ge=0,
        le=12,
        description="Recent dialogue messages to keep (0-12, default 2). Rounded up to whole user/assistant exchanges; the current input is always kept.",
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
        description="Complete updated note text, built on the current note when the key exists; must contain non-whitespace text.",
    )
    kind: Literal["fact", "hypothesis", "plan"] = Field(
        description="fact: backed by H sources. hypothesis: uncertain. plan: a character's in-story intention, not an author outline."
    )
    sources: list[str] = Field(
        default_factory=list,
        max_length=SOURCE_REFERENCE_LIMIT,
        description="H references from this branch, including the current user input. Required for kind=fact.",
    )


class NoteKey(Arguments):
    key: str = Field(min_length=1, max_length=80, description="Existing note key.")


PATCH_GUIDANCE = (
    "JSON Patch operations, each with op and path. op is add, remove, replace, move, copy or test. "
    "add/replace/test need value (any JSON, including null); move/copy need from; remove needs only path. "
    "Paths are slash-separated JSON Pointers, not dot paths: escape ~ as ~0 and / as ~1 inside keys. "
    "Array indices start at 0; add with a final /- appends. An empty path targets the root. "
    "remove, replace, test and from need an existing target; add needs an existing parent."
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
        "Find worldbook entries to edit. For background while writing, use recall_lore instead.\n"
        "Matches literal text in titles, keywords and content, including disabled entries; query=* lists entries. "
        "Returns L handles with short previews for locating targets; use read_lore_entry to see an entry in full. "
        "Script and variable-rule entries are not listed.",
    ),
    "read_lore_entry": (
        LoreRead,
        "Read one worldbook entry in full by L handle: title, content, keys, secondary_keys and enabled. "
        "Script, variable-rule and initialization entries are protected and cannot be read.",
    ),
    "recall_lore": (
        Query,
        "Look up worldbook background for the story. To find entries to edit, use find_lore_for_edit.\n"
        "Write a short, self-contained query in the worldbook's language. Returns complete matching entries in <lorebook>. "
        "Entries already in this context are left out, and entry conditions, cooldowns or relevance limits may leave no matches. "
        "When titles and L handles are shown, recalled entries can serve as the current text for later edits.",
    ),
    "search_history": (
        Search,
        "Search this branch's dialogue by literal text. Use short fragments; query=* lists recent messages. "
        "Returns H references with optional previews; use read_history for the full text. Earlier tool calls are not searchable.",
    ),
    "read_history": (
        HistoryRead,
        "Read original dialogue in full by H reference (H12) or closed span (H12-H18), including the current user input. "
        "A span returns every message in order.",
    ),
    "write_handoff": (
        HandoffWrite,
        "Update the handoff without switching windows. Start from the latest <tavern_memory> or your later successful write, "
        "whichever is newer, and re-check points marked as having changed sources.\n"
        "- chapters are an append-only record of what will still matter later: irreversible events, relationship turns, "
        "promises and debts, secrets and who knows them, recurring people, items and places. Skip one-off actions, scenery "
        "and dialogue texture; the original text stays readable with read_history.\n"
        "- add: usually give only a title and points. Leave through empty and the chapter covers all committed history "
        "not yet in a chapter; set through only to split chapters deliberately.\n"
        "- Do not rewrite finished chapters. Use revise only to fix an error; merge the oldest adjacent chapters when "
        "nearing the total limit.\n"
        "- current fully replaces the current handoff: the scene, relationships, constraints, unresolved issues and tone. "
        "Do not retell chapter events.\n"
        "- refs are optional. Cite a known H reference only when precise lookup will help later; never invent one.\n"
        "- Do not copy whole notes or the plot outline into the handoff.",
    ),
    "new_context": (
        NewContext,
        "Switch to a fresh context window. Older messages and earlier tool calls leave the window; afterwards you "
        "continue the same turn, and the call budget does not reset.\n"
        "- When to switch: the runtime measures context usage for you, so do not estimate it yourself. Without a "
        "context notice, do not switch. A budget reminder means plan the switch, ideally at a natural break; a "
        "forced-rollover notice means switch now.\n"
        "- keep_recent sets how many recent messages stay. It is rounded up to whole user/assistant exchanges, and the "
        "current input always stays.\n"
        "- Every message that leaves must already be covered by a chapter. If it is, call with just keep_recent. "
        "Otherwise pass handoff with add operations for the missing part, leaving through empty. If all history stays, "
        "an empty handoff is fine.\n"
        "- Omitting handoff keeps the current one unchanged. When you pass it, chapters keep lasting events, relationship "
        "changes, promises, secrets and recurring people, items and places; current holds the scene, relationships, "
        "constraints, unresolved issues and tone. Do not copy whole notes or the plot outline.\n"
        "Call alone: a batch containing new_context is rejected and none of its tools run.",
    ),
    "search_notes": (
        Search,
        "Search active notes by literal text in keys or content; query=* lists notes. Returns keys with optional previews. "
        "Note contents are not automatically injected; use read_note for the full text.",
    ),
    "read_note": (
        NoteRead,
        "Read a note in full by key, including archived notes. Metadata may include kind and H sources.",
    ),
    "write_note": (
        Note,
        "Create a note or fully replace one by key. Updates are cumulative: start from the note's current text, keep "
        "information that is still valid, and record how things happened and changed, not just the current state. "
        "Attribute claims to whoever made them, keep uncertainty visible, and "
        "treat retracted information as no longer valid.\n"
        "kind: fact needs H sources from this branch; hypothesis marks uncertainty; plan records a character's own "
        "intentions inside the story. Author-level outlines belong in write_plot.\n"
        "Writing to an archived key reactivates it.",
    ),
    "write_plot": (
        Plot,
        "Create or fully replace this branch's author-level stage outline. The current outline is in <tavern_plot>; "
        "no read is needed.\n"
        "Updates are cumulative: revise the current outline instead of starting over. Keep stages, foreshadowing and "
        "conditions that still hold, change what progress has affected, and condense finished stages without dropping "
        "unresolved foreshadowing.\n"
        "Keep near stages concrete and distant stages broad: possible events, turning points, foreshadowing and the "
        "conditions that trigger them. These are revisable possibilities, not established facts, things characters "
        "know, or actions decided for the user.",
    ),
    "archive_note": (
        NoteKey,
        "Archive a note that is no longer relevant. It drops out of normal searches but stays readable by key; "
        "nothing is deleted. Writing to the same key reactivates it.",
    ),
    "read_state": (
        StateRead,
        "Read runtime variables by JSON Pointer.\n"
        "- scope=mvu (default): the MVU variables (stat_data), with the same paths as patch_state, e.g. /hp.\n"
        "- scope=full: all visible runtime state, including stat_data.\n"
        "- scope=other: everything except stat_data.\n"
        'An empty path reads the whole scope. To refresh all MVU variables before patching, use path="" and scope="mvu".',
    ),
    "patch_state": (
        StatePatch,
        "Update MVU variables with JSON Patch. When variable updates are authorized, submit at least one successful "
        "patch this turn; validation failures may be corrected and retried and do not count as success. "
        "If nothing changed, submit operations=[]; this needs no read and changes nothing.\n"
        "Paths are relative to stat_data: /hp, not /stat_data/hp. Follow the latest <mvu_rules>, keep values consistent "
        "with the final story text, and cite the story evidence in comment. Use this tool instead of writing MVU blocks "
        "in the story text.",
    ),
    "revise_content": (
        ContentRevision,
        "Edit character settings and worldbook entries with semantic actions: set_character, add_lore, update_lore "
        "and remove_lore. Use these actions, not JSON Patch or storage IDs.\n"
        "Omitted properties stay unchanged, and a failed batch changes nothing. Success returns the final contents of "
        "each changed target, which count as current for later edits. Script-bearing targets and execution settings "
        "are protected.",
    ),
    "read_character": (
        CharacterRead,
        "Read one character setting in full: name, description, personality, scenario, mes_example, system_prompt "
        "or post_history_instructions. Missing fields return null; script-bearing fields are protected.",
    ),
    "set_retrieval_query": (
        RetrievalQuery,
        "Set clues for next turn's automatic worldbook lookup: the scenes, people, places or unresolved issues likely "
        "to matter, with anticipated events marked as anticipated. The last successful call this turn wins; "
        'query="" clears this turn\'s clues without touching earlier turns. Clues never appear in the story.',
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
        "write_note": f" Content limit: {config.note_tokens} tokens.",
        "write_plot": f" Content limit: {config.plot_tokens} tokens.",
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
            # Chapters and rollover use committed history; current may also
            # cite this turn's user input, but not the assistant draft.
            history = owner.history
            archive_through = (
                rollover_start(state, history, args.keep_recent) if name == "new_context" else len(history)
            )
            write = args.handoff if name == "new_context" else args
            handoff = state["handoff"]
            try:
                if write is not None:
                    handoff = apply_write(
                        handoff,
                        write,
                        history,
                        archive_through=archive_through,
                        current_input_id=owner.user_message["id"],
                    )
                    check_limits(
                        handoff,
                        history,
                        owner.settings.full_agent_config,
                        owner.settings,
                        current_input_id=owner.user_message["id"],
                    )
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
                {
                    **note_view(
                        n,
                        owner.source_history,
                        pending_reply_id=owner.output_id if owner.story is None else None,
                    ),
                    "text": n["content"],
                }
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
            meta = note_view(
                note,
                owner.source_history,
                pending_reply_id=owner.output_id if owner.story is None else None,
            )
            meta.pop("revision")
            notice = meta.pop("notice", None)
            self.reads.read_note(args.key)
            return self.read(
                meta.pop("content"),
                **(meta if result_field(owner.session, name, "include_metadata") else {}),
                # A withdrawn source affects interpretation even when optional
                # note metadata is hidden.
                **({"notice": notice} if notice else {}),
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
                    result = await owner.script.call("process", "", operations, "", False)
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
                if result.get("loreModified"):
                    # Include callback changes in the same tool checkpoint as
                    # the variable patch, so a later resume never replays them.
                    owner.apply_script_lore(before["lore"], result)
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
