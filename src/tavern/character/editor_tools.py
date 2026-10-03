"""One typed catalog for Card Editor execution, provider schemas and previews."""
import copy
import json
from pathlib import Path
from typing import Any, Literal

import jsonpointer
from pydantic import BaseModel, ConfigDict, Field, model_validator

from tavern.character.card_workflow import greeting_hash, validate_document, validation_report
from tavern.character.cards import diagnose, normalize
from tavern.character.editor_config import FIELD_PURPOSES, blank_card
from tavern.character.editor_personas import PERSONA_SPECS, PersonaTools
from tavern.core.model_view import hidden_field
from tavern.core.tokens import count_text
from tavern.storage.store import dump, uid


class Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Card(Arguments):
    card_ref: str = Field(pattern=r"^C[1-9]\d*$", description="Exact attached card handle, e.g. C1. Always identifies the current draft of this card.")


class Overview(Arguments):
    card_ref: str | None = Field(default=None, description="C handle to inspect in detail; omit to list all cards attached to this conversation.")


class Read(Card):
    version: Literal["current", "round_start", "saved"] = Field(default="current", description="current is the editable draft; round_start reads the last completed version before this task; saved reads the library baseline snapshot. Useful when steering reverses a partial edit.")
    path: str = Field(default="", description="JSON Pointer from the complete document root. Empty reads the entire card; wrapped cards use /data/description. Escape ~ as ~0 and / as ~1 in keys.")


class Search(Card):
    query: str = Field(default="", max_length=1000, description="Literal words to match in worldbook titles, keys and content; empty lists all entries. Results are navigation previews, not full source.")


class Operation(Arguments):
    op: Literal["add", "remove", "replace", "move", "copy", "test"] = Field(description="JSON Patch operation.")
    path: str = Field(description="Destination JSON Pointer relative to the whole card. Array indices are zero-based; /- appends.")
    value: Any = Field(default=None, description="Complete value for add, replace or test; required even when null.")
    source: str | None = Field(default=None, alias="from", description="Source JSON Pointer, required for move/copy.")

    @model_validator(mode="after")
    def complete(self):
        if self.op in {"add", "replace", "test"} and "value" not in self.model_fields_set:
            raise ValueError("add/replace/test 必须提供 value，可为 null")
        if self.op in {"move", "copy"} and self.source is None:
            raise ValueError("move/copy 必须提供 from")
        return self


class Patch(Card):
    operations: list[Operation] = Field(min_length=1, max_length=100, description="1–100 JSON Patch operations applied atomically. Read current affected fields first; preserve unrelated fields and extensions.")
    reason: str = Field(min_length=1, max_length=1000, description="User-requested purpose of the change.")


class Create(Arguments):
    name: str = Field(min_length=1, description="Name of the new card or explicitly requested variant.")
    source_ref: str | None = Field(default=None, description="Optional attached C handle to copy as a new variant. To edit an existing card, use patch_draft on its existing handle instead.")
    proposal_ref: str | None = Field(default=None, description="H reference of the full assistant proposal approved by the user. Runtime preserves the original text.")
    confirmation_ref: str = Field(description="H reference of the actual user confirmation, direct creation request, or mechanical copy request.")
    direct_instruction: bool = Field(default=False, description="True only when the user explicitly asked to create directly without prior proposal, or requested a mechanical copy.")


class Configure(Card):
    target_mode: Literal["half_agent", "full_agent"] = Field(description="Optional user-selected compatibility target, needed only when specific script capabilities affect the design. Ordinary authoring and MVU rules work in both modes without this selection.")
    confirmation_ref: str = Field(description="H reference of the user's runtime choice; original text is retained with the card.")


class Revert(Card):
    reason: str = Field(min_length=1, max_length=1000, description="User-requested reason to undo this task's changes to this card.")


class Opening(Card):
    action: Literal["update", "add", "remove", "move"] = Field(default="update", description="Opening operation. Initialization migration happens only if initialization is explicitly supplied.")
    index: int = Field(ge=0, description="Zero-based opening index; 0 is first_mes, followed by alternate_greetings. add inserts before index (or appends at count).")
    narrative: str | None = Field(default=None, description="Complete opening text for add/update. For initialization-only updates omit this to preserve the existing narrative. Retain macros and inline data unless migration was requested.")
    initialization: str | None = Field(default=None, description="Explicit standalone InitVar/MVU data when the user requested migration; omission preserves current initialization and does not create metadata.")
    to_index: int | None = Field(default=None, ge=0, description="Destination index for move, in the resulting opening list.")


class Skill(Arguments):
    resource: Literal["card-guide.md", "runtime-guide.md", "half-agent.md", "full-agent.md", "SKILL.md", "references/workflow.md"] = Field(default="card-guide.md", description="Built-in documentation resource. SKILL.md and references/workflow.md share one adaptation contract for both modes. runtime-guide.md covers MVU and script compatibility; mode-specific references only explain execution differences.")


class Handoff(Arguments):
    content: str = Field(min_length=1, description="Complete replacement handoff note: preserve decisions, pending work and useful C/H references. Does not replace the separately retained approved proposal.")


class HistorySearch(Arguments):
    query: str = Field(min_length=1, max_length=1000, description="Literal words to locate prior authoring messages; * lists recent messages.")
    limit: int = Field(default=5, ge=1, le=20, description="Maximum navigation matches, each with an H reference and a clearly labeled preview.")


class HistoryRead(Arguments):
    ref: str = Field(pattern=r"^H[1-9]\d*$", description="Exact H reference from history search or current request. Reads full original message and associated tool exchanges.")


class Probe(Card):
    index: int = Field(default=0, ge=0, description="Opening to initialize in a temporary local sandbox; 0 selects first_mes.")
    samples: list[str] = Field(default_factory=list, max_length=10, description="Optional complete text examples on which to observe input/output regex transformations. No model calls are made.")


SPECS = {
    "overview": (Overview, "Inspect attached cards or the structure of one card. Returns current C handles, data root, field purposes/types/sizes, and worldbook paths. No library search."),
    "read_field": (Read, "Read a complete current field, entry, extension, script or whole card. A read makes that subtree available for edits; source is never paginated or truncated."),
    "search_entries": (Search, "Locate worldbook entries by literal words. Previews are only navigation; read_field returns full entries before editing."),
    "create_draft": (Create, "Create a new unsaved V2 card or explicitly requested variant after a proposal is approved, or an explicit direct/mechanical-copy request. This does not save to the library. Returns a new C handle and full initial document."),
    "configure_card": (Configure, "Record an optional user-confirmed compatibility target when requested or when specific script capabilities affect the design. Not required for ordinary authoring or MVU rules. Does not migrate content by itself."),
    "patch_draft": (Patch, "Apply a batch to the specified current draft; a reference is promoted without changing its C handle. Read affected values first. Paths start at the document root (/data/... for wrapped cards). New lore entries omit IDs; hidden fields are preserved by Runtime. A failed batch changes nothing."),
    "revert_draft": (Revert, "Undo this task's uncommitted changes to one card after the user changes direction. Runtime restores the complete round-start card; a card newly created in this task is removed. Other cards and the library are unchanged. Use this instead of mechanically reconstructing the old document."),
    "set_opening": (Opening, "Add, edit, remove or reorder an opening with its existing initialization association. Read the affected opening first. Runtime maintains stable IDs and hashes. Supplying initialization explicitly opts into standalone migration; never migrate on ordinary prose edits."),
    "validate_draft": (Card, "Check structure, authoring completeness and static compatibility separately. Reports warnings and untested runtime behavior; a valid document is not proof of script or narrative correctness."),
    "test_draft": (Probe, "Run local deterministic checks on one opening: initialization, script startup and regex transformations. Uses isolated temporary state and only embedded/cached resources; reports failed/blocked/untested checks. Never calls a model or writes a story."),
    "read_skill": (Skill, "Read complete bundled card-authoring or runtime adaptation documentation. Only listed resources are accessible."),
    "write_handoff": (Handoff, "Replace the handoff note, preserving useful earlier decisions and unresolved work. Approved proposal originals are retained independently. A failed update does not replace the old note."),
    "new_context": (Handoff, "Save the complete handoff note and switch context windows. Call alone. Retains approved proposals, card directory, current user inputs and two recent user/final-answer exchanges; old tools leave together and remain readable from history. Continue the task after switching; the call allowance does not reset."),
    "search_history": (HistorySearch, "Search this authoring conversation only; returns H handles and labeled previews. Includes user requests and final replies, not gameplay."),
    "read_history": (HistoryRead, "Read a full authoring message and its durable tool exchanges. Use it to verify approved constraints or recover results that left the active context."),
    **PERSONA_SPECS,
}


def tool_definitions():
    return [{"type": "function", "function": {"name": name, "description": description, "parameters": model.model_json_schema()}}
            for name, (model, description) in SPECS.items()]


def report(document):
    validation = validation_report(document)
    data = normalize(document)
    return {"valid": validation["valid"], "errors": validation["errors"], "warnings": validation["warnings"],
            "completeness": [f"{FIELD_PURPOSES[k]}尚未填写" for k in ("description", "first_mes", "mes_example") if not data.get(k, "").strip()],
            "compatibility": diagnose(document), "runtime_tested": False}


class EditorTools(PersonaTools):
    def __init__(self, owner):
        self.owner = owner

    @property
    def view(self):
        return self.owner.context.view

    def card(self, ref):
        return self.owner.edits.item(self.owner.working, ref)

    def overview(self, ref=None):
        items = [self.card(ref)] if ref else self.owner.working["cards"]
        result = []
        for item in items:
            document = item["document"]
            data = normalize(document)
            prefix = "/data" if document.get("spec") else ""
            row = {"ref": item["ref"], "name": data.get("name"), "kind": item["kind"], "target_mode": item.get("target_mode"),
                   "root": prefix, "format": document.get("spec", "legacy"), "fields": [
                       {"path": self.view.pointer(prefix + "/" + key.replace("~", "~0").replace("/", "~1")),
                        "type": type(value).__name__, "purpose": FIELD_PURPOSES.get(key, "保留的扩展字段"),
                        "chars": len(value) if isinstance(value, str) else len(dump(self.view.expose(value)))}
                       for key, value in data.items() if not hidden_field(key)]}
            result.append(row)
        return self.view.expose({"cards": result})

    def source_message(self, ref, role=None):
        message = next((m for m in self.owner.edits.messages(self.owner.wid) if m["ref"] == ref), None)
        if message is None or role and message["role"] != role:
            raise ValueError("需要本对话中正确角色的有效 H 引用")
        return message

    def require_read(self, ref, path):
        if self.owner.legacy:
            return
        if not any(path == read or path.startswith(read + "/") or read == "" for read in self.owner.reads.get(ref, [])):
            raise ValueError(f"修改前请先完整读取 {ref} 的 {path or '根对象'}")

    async def execute(self, name, raw):
        if name not in SPECS:
            raise ValueError("工具不可用")
        args = json.loads(raw) if isinstance(raw, str) else copy.deepcopy(raw)
        model = SPECS[name][0]
        if self.owner.legacy and issubclass(model, Card) and "card_ref" not in args and len(self.owner.working["cards"]) == 1:
            args["card_ref"] = self.owner.working["cards"][0]["ref"]
        parsed = model.model_validate(args)
        result = await (self.overview_call(parsed) if name == "overview" else getattr(self, name)(parsed))
        return self.view.expose(result)

    async def overview_call(self, args):
        return self.overview(args.card_ref)

    async def read_field(self, args):
        pointer = self.view.pointer(args.path, restore=True)
        if any(hidden_field(p) for p in jsonpointer.JsonPointer(pointer).parts):
            return {"text": "该字段由程序维护，无需读取或回写。"}
        item = self.card(args.card_ref)
        document = (self.owner.edits.item(self.owner.base, args.card_ref)["document"] if args.version == "round_start"
                    else item["base_document"] if args.version == "saved" else item["document"])
        value = self.view.expose(jsonpointer.resolve_pointer(document, pointer))
        if args.version == "current":
            self.owner.reads.setdefault(args.card_ref, []).append(pointer)
        return {"card_ref": args.card_ref, "path": args.path, "version": args.version, "text": value if isinstance(value, str) else dump(value)}

    async def search_entries(self, args):
        document = self.card(args.card_ref)["document"]
        entries = (normalize(document).get("character_book") or {}).get("entries", [])
        rows = entries.items() if isinstance(entries, dict) else enumerate(entries)
        prefix = "/data" if document.get("spec") else ""
        terms = args.query.casefold().split()
        matches = [{"path": self.view.pointer(prefix + "/character_book/entries/" + str(key).replace("~", "~0").replace("/", "~1")),
                    "title": entry.get("comment", entry.get("name", "")), "preview": self.view.text(entry.get("content", ""))[:180]}
                   for key, entry in rows if all(term in dump(entry).casefold() for term in terms)]
        return {"entries": matches, "total": len(matches), "preview_only": True}

    async def create_draft(self, args):
        confirmation = self.source_message(args.confirmation_ref, "user")
        proposal = self.source_message(args.proposal_ref, "assistant") if args.proposal_ref else None
        if not proposal and not args.direct_instruction:
            raise ValueError("先提出制卡方案，用户确认后提供方案和确认的 H 引用；明确直接制作或机械复制才能使用 direct_instruction")
        if proposal and proposal["seq"] >= confirmation["seq"]:
            raise ValueError("确认必须晚于方案")
        source = self.card(args.source_ref) if args.source_ref else None
        document = copy.deepcopy(source["document"]) if source else blank_card(args.name)
        (document["data"] if document.get("spec") else document)["name"] = self.view.restore(args.name)
        ref = self.owner.edits.reserve_ref(self.owner.wid)
        item = {"ref": ref, "card": None, "kind": "draft", "source": copy.deepcopy(source["source"]) if source else {},
                "base_revision": None, "base_document": blank_card(args.name), "document": document,
                "target_mode": source.get("target_mode") if source else None,
                "brief": (f"方案原文（{proposal['ref']}）：\n{proposal['content']}\n\n" if proposal else "")
                         + f"用户要求/确认原文（{confirmation['ref']}）：\n{confirmation['content']}"}
        self.owner.working["cards"].append(item)
        self.owner.reads[ref] = [""]
        return {"card_ref": ref, "document": self.view.expose(document), "saved_to_library": False}

    async def configure_card(self, args):
        message = self.source_message(args.confirmation_ref, "user")
        item = self.card(args.card_ref)
        item["target_mode"] = args.target_mode
        item["brief"] = item.get("brief", "") + f"\n目标模式 {args.target_mode} 的用户依据（{message['ref']}）：\n{message['content']}"
        return {"card_ref": args.card_ref, "target_mode": args.target_mode}

    async def revert_draft(self, args):
        current = self.card(args.card_ref)
        old = next((c for c in self.owner.base["cards"] if c["ref"] == args.card_ref), None)
        if old:
            self.owner.working["cards"][self.owner.working["cards"].index(current)] = copy.deepcopy(old)
        else:
            self.owner.working["cards"].remove(current)
        self.owner.reads.pop(args.card_ref, None)
        return {"card_ref": args.card_ref, "restored_round_start": bool(old), "removed_new_draft": old is None}

    async def patch_draft(self, args):
        item = self.card(args.card_ref)
        operations = [op.model_dump(by_alias=True, exclude_unset=True) for op in args.operations]
        for op in operations:
            pointer = self.view.pointer(op["path"], restore=True)
            self.require_read(args.card_ref, pointer.rpartition("/")[0] if op["op"] == "add" else pointer)
            if "from" in op:
                self.require_read(args.card_ref, self.view.pointer(op["from"], restore=True))
        changed = self.view.patch(item["document"], operations)
        validate_document(changed)
        item.update(document=changed, kind="draft")
        self.owner.event("draft_change", {"card_ref": args.card_ref, "reason": args.reason})
        return {"card_ref": args.card_ref, "accepted": True, **report(changed)}

    async def set_opening(self, args):
        item = self.card(args.card_ref)
        changed = copy.deepcopy(item["document"])
        data = changed["data"] if changed.get("spec") else changed
        prefix = "/data" if changed.get("spec") else ""
        self.require_read(args.card_ref, prefix + ("/first_mes" if args.index == 0 else "/alternate_greetings"))
        greetings = [data.get("first_mes", ""), *data.get("alternate_greetings", [])]
        meta = data.get("extensions", {}).get("terrarium", {}).get("workflow")
        if args.initialization is not None and meta is None:
            meta = {"version": 1, "greetings": [{"id": uid(), "initialization": "", "greeting_hash": greeting_hash(t)} for t in greetings], "unresolved": []}
            data.setdefault("extensions", {}).setdefault("terrarium", {})["workflow"] = meta
        if args.action != "add" and not 0 <= args.index < len(greetings):
            raise ValueError("开场编号不存在，index 从 0 开始")
        if args.action in {"add", "update"}:
            if args.narrative is None and (args.action == "add" or args.initialization is None):
                raise ValueError("新增需要 narrative；修改需要 narrative 或 initialization")
            narrative = self.view.restore(args.narrative) if args.narrative is not None else greetings[args.index]
            if args.action == "add":
                if args.index > len(greetings):
                    raise ValueError("插入位置超过开场数量")
                greetings.insert(args.index, narrative)
                if meta:
                    meta["greetings"].insert(args.index, {"id": uid(), "initialization": "", "greeting_hash": greeting_hash(narrative)})
            else:
                greetings[args.index] = narrative
            if meta:
                record = meta["greetings"][args.index]
                record["greeting_hash"] = greeting_hash(narrative)
                if args.initialization is not None:
                    record["initialization"] = self.view.restore(args.initialization)
        elif args.action == "remove":
            if len(greetings) == 1:
                raise ValueError("必须保留一个默认开场；可把正文设为空")
            greetings.pop(args.index)
            if meta:
                meta["greetings"].pop(args.index)
        else:
            if args.to_index is None or args.to_index >= len(greetings):
                raise ValueError("需要有效的目标开场编号")
            greetings.insert(args.to_index, greetings.pop(args.index))
            if meta:
                meta["greetings"].insert(args.to_index, meta["greetings"].pop(args.index))
        data["first_mes"], data["alternate_greetings"] = greetings[0], greetings[1:]
        validate_document(changed)
        item.update(document=changed, kind="draft")
        return {"card_ref": args.card_ref, **report(changed)}

    async def validate_draft(self, args):
        item = self.card(args.card_ref)
        return {"card_ref": args.card_ref, **report(item["document"])}

    async def test_draft(self, args):
        from tavern.character.editor_sandbox import test_card
        return await test_card(self.owner.store, self.card(args.card_ref), args.index, args.samples)

    async def read_skill(self, args):
        root = Path(__file__).parents[1] / "skills"
        folder = "adapt-card-to-tavern" if args.resource in {"SKILL.md", "references/workflow.md"} else "card-authoring"
        return {"resource": args.resource, "text": (root / folder / args.resource).read_text(encoding="utf-8")}

    async def write_handoff(self, args):
        if not args.content.strip() or count_text(args.content, self.owner.cfg) > self.owner.cfg.card_editor_config.handoff_tokens:
            raise ValueError("handoff 必须非空且不超过配置 token 上限")
        self.owner.working["state"]["handoff"] = self.view.restore(args.content)
        return {"accepted": True, "content": args.content}

    async def new_context(self, args):
        await self.write_handoff(args)
        self.owner.rolled = True
        return {"accepted": True, "message": "交接笔记已保存，继续本轮任务"}

    async def search_history(self, args):
        terms = args.query.casefold().split()
        messages = [m for m in self.owner.edits.messages(self.owner.wid) if args.query == "*" or all(t in m["content"].casefold() for t in terms)]
        return {"matches": [{"ref": m["ref"], "role": m["role"], "preview": self.view.text(m["content"])[:240]} for m in messages[-args.limit:]], "preview_only": True}

    async def read_history(self, args):
        message = self.source_message(args.ref)
        with self.owner.store.connection() as db:
            trace = db.execute("SELECT payload FROM card_editor_traces WHERE run=?", (message["run"],)).fetchone()
        # Native reasoning and protocol IDs are not business history. Tool pairing
        # is represented by names/arguments/results, with opaque identities projected.
        exchanges = []
        if trace:
            calls = {}
            for row in json.loads(trace[0]):
                for call in row.get("tool_calls", []):
                    calls[call["id"]] = call["function"]
                if row.get("role") == "tool" and row.get("tool_call_id") in calls:
                    call = calls[row["tool_call_id"]]
                    exchanges.append({"tool": call["name"], "arguments": call["arguments"], "result": row["content"]})
        return {"ref": args.ref, "role": message["role"], "text": message["content"], "tool_exchanges": exchanges}
