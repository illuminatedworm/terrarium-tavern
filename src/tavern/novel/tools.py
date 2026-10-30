"""Separate tool surfaces: the investigator reads; the main agent only coordinates."""

from pydantic import BaseModel, ConfigDict, Field


class Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Start(Arguments):
    question: str = Field(
        min_length=1,
        max_length=8000,
        description="One specific, self-contained question about the novel: who or what (with names and aliases as "
        "written in the novel), which part of the book if known, and what kind of answer you need.",
    )
    background: str = Field(
        default="",
        max_length=16000,
        description="Only the story background the investigator needs, such as where this story stands relative to "
        "the novel and how it differs. Do not paste the whole context or cite H/L/R references and notes; the "
        "investigator cannot see them.",
    )


class Reference(Arguments):
    ref: str = Field(
        pattern=r"^R[1-9][0-9]*$",
        description="Investigator reference from start or list, e.g. R1.",
    )


class Message(Reference):
    message: str = Field(
        min_length=1,
        max_length=16000,
        description="Follow-up question, or an answer to the investigator's question; delivered before its next model request.",
    )


class Wait(Reference):
    timeout_seconds: int = Field(
        default=30,
        ge=1,
        le=60,
        description="Seconds to wait, inclusive range 1–60, default 30. Does not cancel the investigator on timeout.",
    )


class Page(Arguments):
    start: int = Field(
        default=0,
        ge=0,
        description="Zero-based Unicode character offset, inclusive. Default 0; at EOF returns empty, beyond EOF is rejected.",
    )


class Read(Page):
    length: int = Field(
        default=8000,
        ge=1,
        le=20000,
        description="Number of Unicode characters, inclusive range 1–20000, default 8000; stops at EOF. End offset is exclusive.",
    )


class Search(Page):
    query: str = Field(
        min_length=1,
        max_length=200,
        description="Literal case-insensitive search text; not a regular expression. 1–200 characters.",
    )
    limit: int = Field(
        default=10,
        ge=1,
        le=30,
        description="Maximum returned matches, inclusive range 1–30, default 10; use next_start for remaining matches.",
    )
    end: int | None = Field(
        default=None,
        ge=0,
        description="Exclusive character offset where the search stops; default end of file. Use with start to "
        "search within one chapter.",
    )


class Outline(Page):
    limit: int = Field(
        default=50,
        ge=1,
        le=100,
        description="Maximum inferred headings, inclusive range 1–100, default 50; use next_start to continue.",
    )


class Ask(Arguments):
    question: str = Field(
        min_length=1,
        max_length=4000,
        description="The clarification you need, and how the answer would change your conclusion.",
    )


MAIN_SPECS = {
    "start_novel_research": (
        Start,
        "Delegate a question about the user's novel to a new read-only investigator. Returns an R reference "
        "immediately; the investigator runs in the background and can continue across turns. It cannot see your "
        "context, so write a self-contained question and pass only the background it needs. Limits: 3 investigators "
        "per branch until destroyed, 5 starts per story turn; destroying or regenerating does not return a start.",
    ),
    "list_novel_research": (
        Arguments,
        "List investigators that are not destroyed: status, remaining requests and seconds, whether a report or "
        "question is waiting to be delivered, and any failure reason. Also returns free slots and starts left this turn.",
    ),
    "message_novel_research": (
        Message,
        "Send a follow-up question, or an answer to an investigator's question. It continues in its existing context "
        "with its remaining quota, restarting if paused or failed; quota is never refilled, and an investigator with "
        "no quota left cannot take messages.",
    ),
    "wait_novel_research": (
        Wait,
        "Wait until an investigator reports, asks a question or stops running. Returns its status only; a report or "
        "question appears as a separate message right after this result. Timing out does not cancel the "
        "investigator: wait again, or continue without it.",
    ),
    "resume_novel_research": (
        Reference,
        "Restart a paused or failed investigator in its existing context with its remaining quota. Quota is never "
        "refilled; an investigator with no quota left cannot be resumed.",
    ),
    "destroy_novel_research": (
        Reference,
        "Cancel an investigator and free its slot; delivered reports and history are kept. Use once its report and "
        "any needed follow-ups are done; keep it only for a concrete follow-up. Refused while a report or question "
        "from it is still waiting to be delivered.",
    ),
}

WORKER_SPECS = {
    "novel_outline": (
        Outline,
        "List chapter headings in N1, detected by heading format. Detection may miss headings; search and range "
        "reading work without them.",
    ),
    "search_novel": (
        Search,
        "Find literal matches in N1 between start and end, with short surrounding excerpts and the total match count "
        "in that range. Excerpts only locate text, and no match does not prove absence; read the surrounding range "
        "to verify.",
    ),
    "read_novel": (
        Read,
        "Read N1 by character range. The result gives the exact range returned and whether more text follows.",
    ),
    "ask_delegator": (
        Ask,
        "Ask the delegator a necessary question and pause until it answers. It may not reply for a long time; if you "
        "can answer under a stated assumption, report instead. Call alone.",
    ),
}


def definitions(specs):
    return [
        {
            "type": "function",
            "function": {"name": name, "description": description, "parameters": model.model_json_schema()},
        }
        for name, (model, description) in specs.items()
    ]
