"""Semantic setting edits: no document paths, scripts or bulk card reads."""

import copy
import re
from html import unescape
from html.parser import HTMLParser
from typing import Annotated, Literal, get_args
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from tavern.character.card_workflow import validate_document
from tavern.character.cards import is_initvar, lore_entries, lore_title
from tavern.full_agent.protocol import ToolError

SettingField = Literal[
    "name",
    "description",
    "personality",
    "scenario",
    "mes_example",
    "system_prompt",
    "post_history_instructions",
]
CHARACTER_FIELDS = get_args(SettingField)


class ContentArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class CharacterRead(ContentArguments):
    ref: SettingField = Field(
        description="One allowed character setting field. No whole card, worldbook, extensions, opening or paths."
    )


class LoreRead(ContentArguments):
    ref: str = Field(
        pattern=r"^L[1-9]\d*$",
        description="Exact L handle for one ordinary narrative worldbook entry. No IDs, indices or wildcard.",
    )


class EntryValues(ContentArguments):
    title: str = Field(min_length=1, description="Complete entry title.")
    content: str = Field(
        min_length=1,
        description="Complete narrative setting text; no executable scripts or variable initialization/rules.",
    )
    keys: list[str] = Field(default_factory=list, description="Primary trigger keywords.")
    secondary_keys: list[str] = Field(default_factory=list, description="Secondary trigger keywords.")
    enabled: bool = Field(default=True, description="Whether this narrative entry is enabled.")

    @model_validator(mode="after")
    def meaningful(self):
        if not self.title.strip() or not self.content.strip():
            raise ValueError("条目标题和正文不能为空白")
        return self


class EntryChanges(ContentArguments):
    title: str | None = Field(default=None, min_length=1, description="New complete title; omit to preserve.")
    content: str | None = Field(
        default=None, min_length=1, description="New complete narrative text; omit to preserve."
    )
    keys: list[str] | None = Field(default=None, description="New primary keywords; omit to preserve.")
    secondary_keys: list[str] | None = Field(
        default=None, description="New secondary keywords; omit to preserve."
    )
    enabled: bool | None = Field(default=None, description="New enabled state; omit to preserve.")

    @model_validator(mode="after")
    def nonempty(self):
        values = self.model_dump(exclude_unset=True)
        if not values or any(value is None for value in values.values()):
            raise ValueError("changes 必须提供至少一个非 null 的设定字段")
        if any(not value.strip() for key, value in values.items() if key in {"title", "content"}):
            raise ValueError("条目标题和正文不能为空白")
        return self


class SetCharacter(ContentArguments):
    op: Literal["set_character"] = Field(description="Set one allowed character setting.")
    field: SettingField = Field(description="Allowed character setting field to update.")
    value: str = Field(
        description="Complete updated text; empty clears an optional setting. Name must remain nonempty."
    )


class AddLore(ContentArguments):
    op: Literal["add_lore"] = Field(
        description="Append one ordinary narrative setting entry; no prior bulk read needed."
    )
    entry: EntryValues = Field(
        description="New entry's narrative settings. Runtime supplies its identity and preserves the worldbook."
    )


class UpdateLore(ContentArguments):
    op: Literal["update_lore"] = Field(description="Update one ordinary narrative entry.")
    ref: str = Field(pattern=r"^L[1-9]\d*$", description="L handle returned for this entry.")
    changes: EntryChanges = Field(
        description="Only changed narrative properties; omitted properties and runtime configuration remain intact."
    )


class RemoveLore(ContentArguments):
    op: Literal["remove_lore"] = Field(description="Delete one ordinary narrative entry.")
    ref: str = Field(pattern=r"^L[1-9]\d*$", description="L handle of the ordinary entry to remove.")


ContentOperation = Annotated[SetCharacter | AddLore | UpdateLore | RemoveLore, Field(discriminator="op")]


class ContentRevision(ContentArguments):
    operations: list[ContentOperation] = Field(
        min_length=1,
        max_length=100,
        description="Atomic setting actions: set_character, add_lore, update_lore, remove_lore. No JSON Patch paths, IDs, card roots or extensions.",
    )
    comment: str = Field(
        min_length=1, max_length=1000, description="Brief user-facing comment about this operation."
    )


CODE = re.compile(
    r"<(?:script|initvar|JSONPatch|UpdateVariable)\b|"
    r"\b_\.(?:set|add|insert|delete|move)\s*\(|"
    r"\{\{\s*(?:setvar|addvar|incvar|decvar|setglobalvar|addglobalvar)::|"
    r"```\s*(?:javascript|typescript|js|ts)\b",
    re.I,
)
SCRIPT_KEYS = {"scripts", "script", "regex_scripts", "tavern_helper", "tavernhelper_scripts", "javascript"}


class ExecutionHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.executable = False

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "iframe", "object", "embed"}:
            self.executable = True
        for key, value in attrs:
            clean = re.sub(r"[\s\x00-\x20]+", "", unescape(value or "")).lower()
            if (
                key.startswith("on")
                or key == "srcdoc"
                or clean.startswith(
                    ("javascript:", "vbscript:", "data:text/html", "data:application/javascript")
                )
            ):
                self.executable = True

    handle_startendtag = handle_starttag


def runtime_code(value):
    if isinstance(value, dict):
        return any(str(key).lower() in SCRIPT_KEYS or runtime_code(item) for key, item in value.items())
    if isinstance(value, list):
        return any(runtime_code(item) for item in value)
    if not isinstance(value, str):
        return False
    text = unescape(value)
    if CODE.search(text):
        return True
    parser = ExecutionHTML()
    parser.feed(text)
    return parser.executable


def card_data(document):
    return document.get("data", document) if document.get("spec") else document


def entry_rows(document):
    entries = (card_data(document).get("character_book") or {}).get("entries", [])
    slots = list(entries.items()) if isinstance(entries, dict) else list(enumerate(entries))
    effective = lore_entries(document)
    identities = [entry["id"] for entry in effective]
    if len(identities) != len(set(identities)):
        raise ToolError("INVALID_REFERENCE", "世界书条目身份重复，无法安全定位；请先由用户修正")
    return [(slot, raw, entry) for (slot, raw), entry in zip(slots, effective, strict=True)]


def lore_references(document, state):
    refs = state.setdefault("lore_references", {})
    known = set(refs.values())
    number = max((int(ref[1:]) for ref in refs if re.fullmatch(r"L[1-9]\d*", ref)), default=0)
    for _, _, entry in entry_rows(document):
        if entry.get("terrarium", {}).get("audience", "writer") == "writer" and entry["id"] not in known:
            number += 1
            refs[f"L{number}"] = entry["id"]
            known.add(entry["id"])
    return refs


def editable_entry(raw, entry):
    return (
        entry.get("terrarium", {}).get("audience", "writer") == "writer"
        and not is_initvar(entry)
        and not runtime_code(raw)
    )


def entry_view(entry):
    return {
        "title": lore_title(entry) or "世界书",
        "content": entry.get("content", ""),
        "keys": entry.get("keys", []),
        "secondary_keys": entry.get("secondary_keys", []),
        "enabled": bool(entry.get("enabled", True)),
    }


class ContentEditor:
    def __init__(self, owner, reads):
        self.owner, self.reads = owner, reads

    def character(self, field):
        value = card_data(self.owner.document).get(field)
        if runtime_code(value):
            raise ToolError(
                "PROTECTED_TARGET", "该设定字段含执行脚本或变量指令，不能通过设定编辑工具读取或修改"
            )
        return value

    def resolve(self, ref, *, document=None, refs=None):
        document = self.owner.document if document is None else document
        refs = lore_references(document, self.owner.state) if refs is None else refs
        identity = refs.get(ref)
        row = next((row for row in entry_rows(document) if row[2]["id"] == identity), None)
        if row is None:
            raise ToolError("INVALID_REFERENCE", "世界书引用无效或条目已删除；请重新定位目标条目")
        if not editable_entry(row[1], row[2]):
            raise ToolError(
                "PROTECTED_TARGET", "该条目属于变量规则、初始化或脚本，不能通过设定编辑工具读取或修改"
            )
        return row

    def rows(self):
        refs = {
            identity: ref for ref, identity in lore_references(self.owner.document, self.owner.state).items()
        }
        return [
            {"ref": refs[entry["id"]], **entry_view(entry)}
            for _, raw, entry in entry_rows(self.owner.document)
            if editable_entry(raw, entry)
        ]

    def revise(self, arguments):
        owner = self.owner
        operations = owner.model_view.restore(
            [op.model_dump(exclude_unset=True) for op in arguments.operations]
        )
        comment = owner.model_view.restore(arguments.comment)
        edits_lore = any(operation["op"] != "set_character" for operation in operations)
        refs = copy.deepcopy(owner.state.get("lore_references", {}))
        if edits_lore:
            lore_references(owner.document, {"lore_references": refs})
        fields, changed, affected_refs = set(), set(), set()
        for operation in operations:
            if operation["op"] == "set_character":
                self.character(operation["field"])
                self.reads.require_character(operation["field"])
                if runtime_code(operation["value"]):
                    raise ToolError("PROTECTED_TARGET", "人物设定不能引入执行脚本或变量指令")
                fields.add(operation["field"])
            elif operation["op"] in ("update_lore", "remove_lore"):
                _, raw, entry = self.resolve(operation["ref"], refs=refs)
                self.reads.require_entry(operation["ref"], raw)
                affected_refs.add(operation["ref"])
                changed.add(entry["id"])
        revised = copy.deepcopy(owner.document)
        # Fix legacy positional identities before a deletion can shift the array.
        # Their effective IDs remain identical to existing references/injections.
        if edits_lore:
            for _, raw, entry in entry_rows(revised):
                if "id" not in raw and "id" not in raw.get("extensions", {}):
                    raw["id"] = entry["id"]
        added = []
        for operation in operations:
            op = operation["op"]
            if op == "set_character":
                card_data(revised)[operation["field"]] = operation["value"]
                continue
            book = card_data(revised).setdefault("character_book", {})
            entries = book.setdefault("entries", [])
            if op == "add_lore":
                values = {"keys": [], "secondary_keys": [], "enabled": True, **operation["entry"]}
                raw = {"id": uuid4().hex, "comment": values.pop("title"), **values}
                if not editable_entry(raw, raw):
                    raise ToolError(
                        "PROTECTED_TARGET", "只能新增普通叙事设定，不能引入初始化、变量规则或脚本"
                    )
                if isinstance(entries, dict):
                    entries[raw["id"]] = raw
                else:
                    entries.append(raw)
                added.append(raw["id"])
                continue
            slot, raw, entry = self.resolve(operation["ref"], document=revised, refs=refs)
            if op == "remove_lore":
                del entries[slot]
                continue
            for field, value in operation["changes"].items():
                # Keep the effective storage location of legacy aliases/overrides.
                key = {"title": "comment"}.get(field, field)
                alias = {
                    "comment": "name",
                    "keys": "key",
                    "secondary_keys": "keysecondary",
                    "enabled": "disable",
                }.get(key)
                extension = raw.get("extensions", {})
                if key in extension:
                    extension[key] = value
                elif key in raw:
                    raw[key] = value
                elif alias and alias in extension:
                    extension[alias] = not value if key == "enabled" else value
                elif alias and alias in raw:
                    raw[alias] = not value if key == "enabled" else value
                else:
                    raw[key] = value
            current = next(row for row in entry_rows(revised) if row[2]["id"] == entry["id"])
            if not editable_entry(current[1], current[2]):
                raise ToolError("PROTECTED_TARGET", "普通条目不能改成初始化、变量规则或执行脚本")
        validate_document(revised)
        current_refs = lore_references(revised, {"lore_references": refs}) if edits_lore else refs
        reverse = {identity: ref for ref, identity in current_refs.items()}
        final_entries = (
            [
                (raw, entry)
                for _, raw, entry in entry_rows(revised)
                if entry["id"] in changed or entry["id"] in added
            ]
            if edits_lore
            else []
        )
        present = {reverse[entry["id"]] for _, entry in final_entries}
        removed_refs = sorted(affected_refs - present)
        # Return the complete final view once per target, then register that exact
        # content as observed. No intermediate batch version grants a read record.
        result = owner.model_view.expose(
            {
                "saved_to_draft": True,
                "added_refs": [reverse[identity] for identity in added],
                "characters": [
                    {"ref": field, "text": card_data(revised).get(field)} for field in sorted(fields)
                ],
                "entries": [{"ref": reverse[entry["id"]], **entry_view(entry)} for _, entry in final_entries],
                "removed_refs": removed_refs,
            }
        )
        owner.document = revised
        # The shared content snapshot keeps its storage field across agent modes.
        owner.snapshot["content"] = {"document": revised, "reason": comment, "turn": owner.tid}
        self.reads.invalidate_content((), removed_refs)
        for field in fields:
            self.reads.read_character(field)
        for raw, entry in final_entries:
            self.reads.read_entry(reverse[entry["id"]], raw)
        if edits_lore:
            owner.state["lore_references"] = current_refs
        if changed:
            owner.snapshot["lore_injections"] = [
                record for record in owner.snapshot.get("lore_injections", []) if record["id"] not in changed
            ]
        for identity in changed:
            owner.snapshot.get("lore", {}).pop(identity, None)
        if hasattr(owner, "exiting"):
            owner.exiting.difference_update(changed)
        return result
