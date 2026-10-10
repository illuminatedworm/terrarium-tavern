"""Typed tools over a turn draft; storage identities never become model arguments."""
import json
import re
from typing import Annotated, Any, Literal

import jsonpatch
import jsonpointer
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from tavern.core.model_view import state_for_model
from tavern.core.tokens import count_text
from tavern.full_agent.argument_errors import ToolArgumentError
from tavern.full_agent.config import FullAgentConfig
from tavern.full_agent.content import CharacterRead, ContentEditor, ContentRevision, LoreRead, entry_view
from tavern.full_agent.protocol import ToolError, TurnError, success
from tavern.full_agent.read_guards import DraftReads
from tavern.full_agent.state import note_view, rollover, source_hash
from tavern.scripting.runtime import RuntimeRollbackError
from tavern.tracker.state import InvalidStatePatch, InvalidStatePath, ProtectedStateField, stage


class Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Query(Arguments):
    query: str = Field(min_length=1, max_length=1000, description="Search text or retrieval clues.")


class RetrievalQuery(Arguments):
    query: str = Field(max_length=1000, description="Replace next-turn retrieval clues; empty string clears this turn's clues.")


class Search(Query):
    limit: int = Field(default=5, ge=1, le=10, description="Maximum number of matches.")


class Read(Arguments):
    ref: str = Field(min_length=1, max_length=80, description="Reference, note key, or character field to read.")


class HistoryRead(Read):
    ref: str = Field(min_length=1, max_length=80, description="Exact H reference from search_history or retained context, e.g. H1; must exist in the current branch.")


class NoteRead(Arguments):
    key: str = Field(min_length=1, max_length=80, description="Exact existing note key, e.g. relationships/guide; accepts active or archived notes, not H/L references.")


class Handoff(Arguments):
    content: str = Field(min_length=1, max_length=64000, description="Complete updated handoff text; must contain non-whitespace text.")


class NewContext(Arguments):
    handoff: str = Field(min_length=1, max_length=64000, description="Complete updated handoff text for the new window; must contain non-whitespace text.")
    keep_recent: int = Field(default=2, ge=0, le=12, description="Recent dialogue messages to retain (0-12; default 2), normally up to 6 user/assistant exchanges. Expanded to complete exchanges; current user input is retained separately.")


class Note(Arguments):
    key: str = Field(min_length=1, max_length=80, pattern=r"^[\w\-/ .\u4e00-\u9fff]+$", description="Meaningful, stable note key.")
    content: str = Field(min_length=1, max_length=64000, description="Complete updated note text; must contain non-whitespace text.")
    kind: Literal["fact", "hypothesis", "plan"] = Field(description="Whether the note records evidence-backed facts, a hypothesis, or a plan.")
    sources: list[str] = Field(default_factory=list, max_length=20, description="Valid H references in this branch, including the current user input; required for facts. No prior tool read or search is required.")


class NoteKey(Arguments):
    key: str = Field(min_length=1, max_length=80, description="Existing note key.")


PATCH_GUIDANCE = ("JSON Patch objects. Each requires op and path. "
        "op is add, remove, replace, move, copy, or test. add/replace/test require value (any JSON value, including null); "
        "move/copy require from (source JSON Pointer); remove needs only op/path. "
        "path/from use slash-separated JSON Pointers, not dot paths: escape ~ as ~0 and / as ~1 within keys. "
        "Array indices are zero-based; add may use a final /- to append. "
        "An empty pointer targets the root. Follow this tool's root rules; existing targets are required for "
        "remove/replace/test and from, and add requires an existing parent.")


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
    operations: list[StateOperation] = Field(max_length=100, description=PATCH_GUIDANCE + " Empty array means no changes.")
    reason: str = Field(min_length=1, max_length=1000, description="Story evidence and purpose of this change.")


class StateRead(Arguments):
    path: str = Field(default="", max_length=200, description="JSON Pointer relative to the selected scope, e.g. /hp or /items/0/name. Escape ~ as ~0 and / as ~1 in keys. Empty reads the complete scope; missing paths return null.")
    scope: Literal["variables", "story"] = Field(default="variables", description="variables reads stat_data, matching patch_state paths. story reads projected story state. Only a complete variables read qualifies for a nonempty patch.")


RESULT_FIELDS = {
    "recall_lore": {"include_ref": False, "include_title": False},
    "search_history": {"include_preview": True},
    "read_history": {"include_metadata": True},
    "search_notes": {"include_preview": True},
    "read_note": {"include_metadata": True},
    "read_state": {"include_metadata": True},
    "read_character": {"include_metadata": True},
}
READ_DEPENDENCIES = {"patch_state": "read_state"}


def result_field(session, tool, field):
    return session.get("tool_result_options", {}).get(tool, {}).get(field, RESULT_FIELDS[tool][field])


SPECS = {
    "find_lore_for_edit": (Search, "Locate ordinary narrative worldbook entries for editing; use recall_lore for narrative recall. "
        "Search literal title, keywords or text, including disabled ordinary entries; query=* lists up to limit results. "
        "Returns L handles, titles and navigation previews. Read one target with read_lore_entry before updating/removing it."),
    "read_lore_entry": (LoreRead, "Read one complete ordinary narrative worldbook entry by L handle: title, content, keys, secondary_keys, enabled. "
        "Establishes a current read record for editing. Protected scripts, variable rules and initialization are unavailable."),
    "recall_lore": (Query, "Recall complete worldbook sources for narrative writing; use find_lore_for_edit to locate editing targets. "
        "Use a concise, self-contained query in the worldbook's language. Entry conditions, cooldowns, relevance thresholds and limits may yield no matches. "
        "Returns complete selected entries in <lorebook>; already injected text is omitted. "
        "When enabled, results include titles and L handles. These handles can be passed to read_lore_entry, subject to target protection; recall alone does not establish an edit read record."),
    "search_history": (Search, "Search dialogue in this branch by literal text terms; returns H references and optional previews. "
        "Use short text fragments; query=* lists recent records. Use read_history for complete sources. Old tool exchanges are not searchable."),
    "read_history": (HistoryRead, "Read complete original dialogue by a valid H reference, including the current user input. H references are navigation handles, not story prose."),
    "write_handoff": (Handoff, "Replace the complete handoff in the current window. The handoff is a summary of all current context. Build on the latest runtime memory update or successful write, whichever is later. "
        "Preserve valid scene context, relationships, user constraints, important experiences, unresolved issues, tone and useful references. Mark plans as pending; verify stale sources."),
    "new_context": (NewContext, "Replace the complete handoff and switch windows. The handoff is a summary of all current context. Preserve valid context, constraints, unresolved issues and references needed to continue. "
        "Call alone: a mixed batch is rejected without executing any tools. keep_recent counts dialogue messages, expanded to complete exchanges; the current input stays separately. "
        "Old tool exchanges leave the window. Continue this turn after switching; the model-call budget does not reset."),
    "search_notes": (Search, "Search active notes in this branch by literal terms in content or keys; query=* lists notes. "
        "Returns navigation previews when enabled. Note contents are not automatically injected; use read_note for complete text."),
    "read_note": (NoteRead, "Read a complete active or archived note by key, with optional kind, H sources and stale status. "
        "Stale means sources changed or became unavailable; verify them before relying on the note."),
    "write_note": (Note, "Create or fully replace a note by key; preserve still-valid information. No prior note or history read is required. "
        "kind=fact requires supporting H sources valid in this branch; hypothesis marks uncertainty and plan marks intent. "
        "Attribute character claims, preserve uncertainty and respect revoked information. Replacing an archived key reactivates it."),
    "archive_note": (NoteKey, "Archive a note so normal searches exclude it; it remains readable by key. "
        "This does not delete it or older history. write_note with the same key reactivates it."),
    "read_state": (StateRead, "Read state using JSON Pointer. Default scope=variables reads stat_data with the same relative paths as patch_state, e.g. /hp. "
        "Empty path reads the complete scope; scope=story reads projected story state. Internal ledgers remain hidden. "
        'Before a nonempty patch, read all variables with path="", scope=variables; partial or story-scope reads do not qualify.'),
    "patch_state": (StatePatch, "Must succeed exactly once this turn when variable updates are authorized; retry after validation errors, which do not count as success. "
        'Nonempty operations require a current complete variables read with read_state(path="", scope="variables"). '
        "Apply MVU-validated JSON Patch relative to stat_data, e.g. /hp, not /stat_data/hp. "
        "Follow the latest <mvu_rules>, explain story evidence in reason and align with the final narrative. "
        "Submit operations=[] to confirm no changes; this needs no read, changes no variables and consumes the one successful submission. Use this tool instead of MVU blocks in prose."),
    "revise_content": (ContentRevision, "Edit narrative settings with set_character, add_lore, update_lore or remove_lore; use these semantic actions, not JSON Patch or storage IDs. "
        "Existing fields require a current read_character record; existing entries require a current read_lore_entry record. add_lore needs no prior read. "
        "Success returns complete final contents of changed targets and refreshes their read records for further edits in this window; deletion invalidates the record. "
        "Omitted properties remain intact. Script-bearing targets and execution settings are protected; failed batches change nothing."),
    "read_character": (CharacterRead, "Read one complete setting: name, description, personality, scenario, mes_example, system_prompt or post_history_instructions. "
        "Missing fields return null. Establishes a current read record for set_character; script-bearing fields are protected."),
    "set_retrieval_query": (RetrievalQuery, "Replace this turn's clues for next-turn automatic worldbook retrieval; the last successful call wins. "
        'query="" clears these clues without erasing clues saved by earlier turns. Describe relevant scenes, entities or unresolved issues, distinguishing anticipated events from facts. Clues stay outside narrative prose.'),
}


def tool_definitions(request, session, config=None, *, settings=None, catalog=False, document=None):
    config = config or FullAgentConfig()
    names = set(SPECS)
    if session.get("context_strategy") == "openai_compact":
        names.difference_update({"new_context", "write_handoff"})
    if not catalog:
        names.difference_update(session.get("disabled_tools", []))
        for write, read in READ_DEPENDENCIES.items():
            if read not in names:
                names.discard(write)
    if not catalog and not request.allow_content_edit:
        names.discard("revise_content")
    limits = {
        "write_handoff": f" content limit: {config.handoff_tokens} tokens.",
        "new_context": f" handoff limit: {config.handoff_tokens} tokens.",
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
        limits["recall_lore"] = mechanism
    return [{"type": "function", "function": {"name": name, "description": description + limits.get(name, ""),
             "parameters": model.model_json_schema()}} for name, (model, description) in SPECS.items() if name in names]


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
        return [{"ref": f"H{i+1}", "role": m["role"], "text": m["raw"]}
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
            item = {k: v for k, v in row.items() if k != "text"}
            if include_preview:
                item["preview"] = text[at:at+400]
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
            rows = [{"ref": row["ref"], "title": row["title"],
                     "text": row["title"] + " " + " ".join(row["keys"] + row["secondary_keys"]) + " " + row["content"]}
                    for row in self.content.rows()]
            result = self.search(rows, args.query, args.limit)
            result["notice"] = "preview 仅用于定位；用 read_lore_entry 完整读取一个目标后才能修改或删除。"
            return owner.model_view.expose(result)
        if name == "read_lore_entry":
            _, raw, entry = self.content.resolve(args.ref)
            result = owner.model_view.expose({"ref": args.ref, **entry_view(entry)})
            self.reads.read_entry(args.ref, raw)
            return result
        if name == "recall_lore":
            return await owner.retrieve(args.query, source="agent",
                                        include_ref=result_field(owner.session, name, "include_ref"),
                                        include_title=result_field(owner.session, name, "include_title"))
        if name == "search_history":
            return self.search(self.archive(), args.query, args.limit,
                               include_preview=result_field(owner.session, name, "include_preview"))
        if name == "read_history":
            row = next((r for r in self.archive() if r["ref"] == args.ref), None)
            if row is None:
                raise ToolError("INVALID_REFERENCE", "当前分支没有此历史引用")
            metadata = ({"ref": args.ref, "role": row["role"]}
                        if result_field(owner.session, name, "include_metadata") else {})
            return self.read(row["text"], **metadata)
        if name in ("write_handoff", "new_context"):
            handoff = args.handoff if name == "new_context" else args.content
            self.cap_text(handoff, owner.settings.full_agent_config.handoff_tokens, "handoff")
            if name == "new_context":
                rollover(state, owner.history, handoff, args.keep_recent)
                owner.rolled = True
            else:
                state["handoff"] = {"text": handoff, "revision": state["handoff"]["revision"] + 1, "stale": False}
            state.pop("edited_sources", None)
            return {"saved_to_draft": True, "window": state["window"]["number"],
                    "notice": "正文完成后与本回合统一提交。"}
        if name == "search_notes":
            rows = [{**note_view(n, owner.source_history), "text": n["content"]} for n in state["notes"].values()
                    if n["status"] == "active"]
            for row in rows:
                row.pop("content")
                row.pop("revision")
            return self.search(rows, args.query, args.limit,
                               include_preview=result_field(owner.session, name, "include_preview"))
        if name == "read_note":
            note = state["notes"].get(args.key)
            if note is None:
                raise ToolError("INVALID_REFERENCE", "笔记 key 不存在")
            meta = note_view(note, owner.source_history)
            meta.pop("revision")
            return self.read(meta.pop("content"),
                             **(meta if result_field(owner.session, name, "include_metadata") else {}))
        if name == "write_note":
            self.cap_text(args.content, owner.settings.full_agent_config.note_tokens, "笔记")
            old = state["notes"].get(args.key, {})
            if args.kind == "fact" and not args.sources:
                raise ToolError("INVALID_ARGUMENTS", "fact 必须提供当前分支有效的 H 原文依据；尚未确立的内容请用 hypothesis/plan")
            sources = []
            history = {f"H{i+1}": message for i, message in enumerate(owner.source_history)}
            for ref in dict.fromkeys(args.sources):
                source = history.get(ref)
                if source is None:
                    raise ToolError("INVALID_REFERENCE", "sources 包含当前分支无效的历史引用；请使用有效的 H 引用")
                sources.append({"message_id": source["id"], "hash": source_hash(source)})
            state["notes"][args.key] = {"key": args.key, "content": args.content, "kind": args.kind,
                                       "status": "active", "revision": old.get("revision", 0) + 1, "sources": sources}
            return {"key": args.key, "saved_to_draft": True}
        if name == "archive_note":
            if args.key not in state["notes"]:
                raise ToolError("INVALID_REFERENCE", "笔记 key 不存在")
            state["notes"][args.key]["status"] = "archived"
            state["notes"][args.key]["revision"] += 1
            return {"key": args.key, "archived": True}
        if name == "read_state":
            try:
                path = owner.model_view.pointer(args.path, restore=True)
            except jsonpointer.JsonPointerException as error:
                raise ToolError("INVALID_PATH", str(error)) from None
            projected = state_for_model(owner.snapshot["state"])
            root = projected.get("stat_data", {}) if args.scope == "variables" else projected
            value = jsonpointer.resolve_pointer(root, path, default=None)
            result = self.read(encoded(owner.model_view.expose(value)),
                               **({"path": args.path, "scope": args.scope}
                                  if result_field(owner.session, name, "include_metadata") else {}))
            if path == "" and args.scope == "variables":
                self.reads.read_state()
            return result
        if name == "patch_state":
            if args.operations:
                self.reads.require_state()
                try:
                    operations = owner.model_view.operations([op.model_dump(by_alias=True) for op in args.operations])
                    # Share only the pure variable projection validator, not Tracker
                    # orchestration. It preserves hidden parent metadata and maps
                    # visible array indices before any MVU callback can execute.
                    operations, _ = stage(owner.snapshot["state"].get("stat_data", {}), operations)
                except ProtectedStateField as error:
                    raise ToolError("PROTECTED_TARGET", str(error),
                                    next_action="仅修改读取得到的业务变量，内部字段由程序维护。") from None
                except (jsonpointer.JsonPointerException, jsonpatch.JsonPatchConflict, InvalidStatePath) as error:
                    raise ToolError("INVALID_PATH", str(error)) from None
                except (jsonpatch.InvalidJsonPatch, KeyError, TypeError) as error:
                    raise ToolError("INVALID_ARGUMENTS", str(error)) from None
                except jsonpatch.JsonPatchTestFailed:
                    # The library exception embeds complete compared values,
                    # including metadata the model was deliberately not shown.
                    raise ToolError("PATCH_VALIDATION_FAILED", "补丁的 test 条件未满足；请核对当前变量和预期值。") from None
                except (jsonpatch.JsonPatchException, InvalidStatePatch) as error:
                    raise ToolError("PATCH_VALIDATION_FAILED", str(error)) from None
                before = await owner.script.call("snapshot")
                try:
                    result = await owner.script.call("process", "", operations, "")
                except RuntimeRollbackError as error:
                    raise TurnError("SCRIPT_RESTORE_FAILED", "变量工具失败且运行时恢复失败，停止回合；草稿未提交") from error
                except Exception as error:
                    try:
                        await owner.script.call("update", before)
                    except Exception as restore_error:
                        raise TurnError("SCRIPT_RESTORE_FAILED", "变量工具失败且运行时恢复失败，停止回合；草稿未提交") from restore_error
                    # Script exceptions can contain snapshots of private runtime
                    # fields. Keep the diagnostic out of the model tool result.
                    raise ToolError("PATCH_VALIDATION_FAILED", "MVU 或脚本校验失败；本次变量补丁已回滚。") from error
                owner.snapshot["state"], owner.snapshot["scopes"] = result["state"], result["scopes"]
            owner.patch_state_done = True
            return {"saved_to_draft": True, "patch_state_completed": True,
                    "notice": "变量已通过 MVU 校验。" if args.operations else "已确认本轮变量无变化；未执行变量补丁或脚本回调。"}
        if name == "read_character":
            value = self.content.character(args.ref)
            result = owner.model_view.expose({"text": value,
                **({"ref": args.ref, "exists": value is not None} if result_field(owner.session, name, "include_metadata") else {})})
            self.reads.read_character(args.ref)
            return result
        if name == "revise_content":
            return self.content.revise(args)
        if name == "set_retrieval_query":
            owner.retrieval_query = args.query
            return {"saved_to_draft": True}
        raise ToolError("UNKNOWN_TOOL", "未知工具")
