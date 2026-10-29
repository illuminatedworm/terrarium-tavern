"""Model-facing contracts; execution and authority belong to TrackerTask."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_core import PydanticCustomError

from tavern.core.history_refs import SOURCE_REFERENCE_LIMIT
from tavern.memories.history_recall import HISTORY_TOOL


class Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ReadCharacter(Arguments):
    field: str = Field(
        default="",
        description="Dot-separated path relative to character data, e.g. description or character_book.entries, with no data prefix. Empty reads everything.",
    )


class ReadState(Arguments):
    path: str = Field(
        default="",
        description="Dot-separated path relative to stat_data, with no stat_data prefix. Empty reads all variables.",
    )


class Search(Arguments):
    query: str = Field(
        min_length=1,
        max_length=1000,
        description="Short keywords in the source language; * lists available records.",
    )


class Patch(Arguments):
    operations: list[dict] = Field(
        max_length=100,
        description="JSON Patch operations, each with op and path. add/replace/test need value; move/copy need from. Paths are slash-separated JSON Pointers: escape ~ as ~0 and / as ~1 inside keys. Array indices start at 0; a final /- appends. Empty array means no changes.",
    )
    comment: str = Field(
        min_length=1, max_length=2000, description="Brief user-facing comment about this operation."
    )


class MemoryRecord(Arguments):
    content: str | None = None
    category: Literal["current", "episode", "open", "uncertain"] | None = None
    kind: Literal["fact", "claim", "inference"] | None = None
    people: list[str] | None = None
    known_to: list[str] | None = None
    story_time: str | None = None
    status: Literal["active", "completed"] | None = None
    callback: bool | None = None
    sources: list[str] = Field(
        min_length=1,
        max_length=SOURCE_REFERENCE_LIMIT,
        description="Passage refs such as H52#3 shown for this batch, in its input or recall results; at least one inside batch_range. Message refs such as H52 are not sources.",
    )
    setting_refs: list[str] = Field(
        default_factory=list,
        max_length=SOURCE_REFERENCE_LIMIT,
        description="Optional E refs for provided setting evidence.",
    )


class MemoryOperation(Arguments):
    op: Literal["add", "revise", "complete", "merge", "remove"]
    id: str | None = Field(
        default=None,
        pattern=r"^[RN][1-9]\d*$",
        description="R ref of an existing record. For add, omit it or give an N ref.",
    )
    record: MemoryRecord
    merge_ids: list[str] = Field(default_factory=list, description="Other existing R refs to merge into id.")

    @model_validator(mode="after")
    def identity(self):
        if self.op == "add" and self.id is not None and not self.id.startswith("N"):
            raise PydanticCustomError("invalid_memory_identity", "新增记忆只允许省略 id 或使用 N 编号")
        if self.op != "add" and (self.id is None or not self.id.startswith("R")):
            raise PydanticCustomError("invalid_memory_identity", "修改记忆必须使用已有 R 编号")
        return self


class Question(Arguments):
    topic: Literal["ownership", "identity", "knowledge", "injury", "relationship", "completion"]
    query: str = Field(min_length=1)
    record_ids: list[str] = Field(
        min_length=1, description="Uncertain record R refs, or N1/N2 by order of adds in this call."
    )


class WriteMemory(Arguments):
    operations: list[MemoryOperation] = Field(max_length=100)
    questions: list[Question] = Field(default_factory=list, max_length=8)


MEMORY_CONTRACT = (
    "Submit the memory changes for the current batch. Call once per batch; if nothing changed, submit "
    "operations=[]. Records you leave out stay unchanged.\n"
    "- op: add (needs content and category), revise, complete, merge or remove. On revise, omitted content "
    "fields keep their old values.\n"
    "- category: current = the present situation; episode = experiences and changes; open = pending "
    "commitments; uncertain = unresolved conclusions.\n"
    "- kind: fact by default; use claim for what a character says and inference for your own deductions.\n"
    "- Leave unknown details out: people and known_to default to [], story_time to 未知. Never guess who "
    "knows what.\n"
    "- sources: every change cites passage refs such as H52#3, at least one inside batch_range. Background "
    "and recalled passages can support a change but cannot be its only source. Message refs such as H52 are "
    "not sources.\n"
    "- E refs are setting or state evidence, not proof that an event happened; historical=false marks a "
    "current-version fallback.\n"
    "- Do not change manual, deleted or source_stale records, including as merge sources, and do not restore "
    "what the user deleted.\n"
    "- questions: only for key doubts about active, non-manual uncertain records. Keep the uncertainty "
    "explicit meanwhile.\n"
    "- callback=true marks an episode as texture, such as a representative line or action: at most three, "
    "300 tokens in total. Never ask for review about texture.\n"
    "The tool result brings the next batch or a review when there is one. In a review, only revise the "
    "supplied uncertain records; resolving one requires supplied E evidence, and no new questions are allowed."
)

SPECS = {
    "read_character": (
        ReadCharacter,
        "Read this conversation's character data, including its worldbook.",
    ),
    "search_lore": (
        Search,
        "Search enabled worldbook entries with the story's recall rules and return complete matches; * lists "
        "enabled entries. Read-only: it does not change what the Writer sees. No match does not mean the "
        "setting does not exist.",
    ),
    "read_state": (
        ReadState,
        "Read the MVU variables (stat_data), including a patch you already submitted this turn.",
    ),
    "search_memory": (
        Search,
        "Search this branch's active memories. Returns complete records with kind, manual protection and "
        "source-stale status; verify records whose source text changed or went missing.",
    ),
    "write_memory": (WriteMemory, MEMORY_CONTRACT),
    "revise_content": (
        Patch,
        "Edit this conversation's character card and worldbook (not the library card) with JSON Patch. Read "
        "the affected fields first. Paths are relative to character data, e.g. /description or "
        "/character_book/entries, with no /data prefix. Leave unrelated fields as they are.\n"
        "Record durable changes here, such as important experiences and relationships; do not copy "
        "short-term memories.",
    ),
    "patch_state": (
        Patch,
        "Record this turn's actual variable changes as one JSON Patch array, relative to stat_data (no "
        "/stat_data prefix). If nothing changed, submit []. Only one successful submission is accepted, so "
        "include every change at once. History is context, not events to apply again.",
    ),
}


def definitions(*, memory, variables, content):
    names = {"read_character", "search_lore", "read_state", "search_memory"}
    if memory:
        names.add("write_memory")
    if variables:
        names.add("patch_state")
    if content:
        names.add("revise_content")
    return [
        HISTORY_TOOL,
        *[
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": description,
                    "parameters": model.model_json_schema(),
                },
            }
            for name, (model, description) in SPECS.items()
            if name in names
        ],
    ]
