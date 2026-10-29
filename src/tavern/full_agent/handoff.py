"""Full Agent handoff: an append-mostly chapter ledger plus a replaced current handoff.

Chapters cover contiguous closed H spans from H1, without gaps or overlap.
Runtime supplies the default endpoint; point citations are optional. A rollover must leave every
exiting message covered. Written chapters stay as they are unless revised or
merged. Only merged chapters are summarized again when the total limit requires
coarser history; old detail stays reachable through the cited spans.

Storage keeps immutable message identities and span endpoints. H and C numbers
are only the rendering shared by the model and user; forks remap identities.
"""

import copy
import uuid
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from tavern.core.history_refs import (
    message_positions,
    message_ref,
    parse_message_ref,
    parse_span_ref,
    span_ref,
)
from tavern.core.tokens import count_text

CURRENT_HEADING = "当前交接"


class HandoffError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


class _Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)


class Point(_Arguments):
    text: str = Field(min_length=1, max_length=2000, description="One concise statement.")
    refs: list[str] = Field(
        default_factory=list,
        max_length=4,
        description="Optional, at most 4 known supporting H references or closed spans. Omit when unnecessary or the information comes from instructions, character material or the current input. Do not guess references; chapter citations must stay inside the chapter.",
    )
    kind: Literal["fact", "inference"] = Field(
        default="fact", description="fact for established information; inference for your judgement."
    )


class ChapterAdd(_Arguments):
    op: Literal["add"] = Field(description="Append the next chapter right after the last one.")
    through: str | None = Field(
        default=None,
        description="Leave empty: the chapter then covers the messages leaving the window (new_context) or all committed history not yet in a chapter (write_handoff). Set a known H endpoint only to split chapters deliberately. The start always follows the previous chapter.",
    )
    title: str = Field(min_length=1, max_length=80, description="Short chapter title.")
    points: list[Point] = Field(min_length=1, max_length=20)


class ChapterRevise(_Arguments):
    op: Literal["revise"] = Field(description="Rewrite one chapter's title and points; its span stays.")
    chapter: str = Field(description="Chapter reference shown in <tavern_memory>, e.g. C3.")
    title: str = Field(min_length=1, max_length=80)
    points: list[Point] = Field(min_length=1, max_length=20)


class ChapterMerge(_Arguments):
    op: Literal["merge"] = Field(
        description="Replace adjacent chapters with one coarser chapter over their union."
    )
    chapters: list[str] = Field(
        min_length=2, max_length=10, description="Adjacent chapter references, e.g. ['C1', 'C2']."
    )
    title: str = Field(min_length=1, max_length=80)
    points: list[Point] = Field(min_length=1, max_length=20)


ChapterOp = Annotated[ChapterAdd | ChapterRevise | ChapterMerge, Field(discriminator="op")]


class HandoffWrite(_Arguments):
    chapters: list[ChapterOp] = Field(
        default_factory=list,
        max_length=10,
        description="Chapter operations in order; C references mean the numbering before this call. Omit to keep chapters.",
    )
    current: list[Point] = Field(
        min_length=1,
        max_length=30,
        description="Complete replacement current handoff: scene, relationships, constraints, unresolved issues and tone. Do not retell chapter events.",
    )


def _span(history, first, last, *, current_input_id: str | None = None):
    return {
        key: current_input_id
        if current_input_id is not None and position == len(history) + 1
        else history[position - 1]["id"]
        for key, position in (("start", first), ("end", last))
    }


def _positions(stored, positions):
    first, last = positions.get(stored["start"]), positions.get(stored["end"])
    return (first, last) if first is not None and last is not None else None


def chapter_spans(handoff, history):
    positions = message_positions(history)
    spans = []
    for index, chapter in enumerate(handoff["chapters"], 1):
        span = _positions(chapter, positions)
        if span is None:
            raise HandoffError("INVALID_REFERENCE", f"C{index} 的范围不在当前分支")
        spans.append(span)
    return spans


def _points(points, history, within, *, current_input_id: str | None = None):
    last_source = len(history) + (current_input_id is not None)
    result = []
    for point in points:
        refs = []
        for ref in dict.fromkeys(point.refs):
            span = parse_span_ref(ref)
            if span is None or span[1] > last_source:
                raise HandoffError(
                    "INVALID_REFERENCE",
                    f"{ref} 不是当前可引用原文 {span_ref(1, last_source)} 内的引用或闭区间",
                )
            if within and not (within[0] <= span[0] and span[1] <= within[1]):
                raise HandoffError("INVALID_REFERENCE", f"{ref} 不在所属章节 {span_ref(*within)} 内")
            refs.append(_span(history, *span, current_input_id=current_input_id))
        result.append({"text": point.text.strip(), "kind": point.kind, "refs": refs})
    return result


def apply_write(handoff, write, history, *, archive_through=None, current_input_id: str | None = None):
    """Apply semantic edits; Runtime supplies the ordinary chapter endpoint."""
    result = copy.deepcopy(handoff)
    chapters = result["chapters"]
    chapter_spans(result, history)
    # C references mean the numbering the caller saw before this call.
    refs = {f"C{index}": chapter["id"] for index, chapter in enumerate(chapters, 1)}

    def located(ref):
        if ref not in refs:
            raise HandoffError("INVALID_REFERENCE", f"{ref} 不是本次请求中的章节编号")
        position = next((i for i, c in enumerate(chapters) if c["id"] == refs[ref]), None)
        if position is None:
            raise HandoffError("INVALID_REFERENCE", f"{ref} 已在本次调用中被合并")
        return position

    for op in write.chapters:
        spans = chapter_spans(result, history)
        if op.op == "add":
            last = spans[-1][1] if spans else 0
            through = (
                parse_message_ref(op.through)
                if op.through is not None
                else len(history)
                if archive_through is None
                else archive_through
            )
            if through is None or through > len(history):
                raise HandoffError("INVALID_REFERENCE", f"{op.through} 不是当前分支有效的 H 引用")
            if through <= last and op.through is not None:
                raise HandoffError(
                    "INVALID_ARGUMENTS", f"add 的 through 必须晚于最后一章的终点 {message_ref(last)}"
                )
            if through <= last:
                # Runtime chose the endpoint: nothing new is leaving or unarchived.
                covered = f"章节已覆盖到 {message_ref(last)}" if last else "当前没有需要归档的原文"
                raise HandoffError(
                    "INVALID_ARGUMENTS", f"{covered}，本次没有可新增章节的范围；请删去这个 add"
                )
            within = (last + 1, through)
            chapters.append(
                {
                    "id": uuid.uuid4().hex,
                    "title": op.title.strip(),
                    **_span(history, *within),
                    "points": _points(op.points, history, within),
                }
            )
        elif op.op == "revise":
            index = located(op.chapter)
            chapters[index].update(
                title=op.title.strip(),
                points=_points(op.points, history, spans[index]),
            )
        else:
            indexes = [located(ref) for ref in dict.fromkeys(op.chapters)]
            if len(indexes) < 2 or sorted(indexes) != list(range(min(indexes), max(indexes) + 1)):
                raise HandoffError("INVALID_ARGUMENTS", "merge 只能合并两个以上相邻章节")
            first, last = min(indexes), max(indexes)
            within = (spans[first][0], spans[last][1])
            merged = {
                "id": uuid.uuid4().hex,
                "title": op.title.strip(),
                **_span(history, *within),
                "points": _points(op.points, history, within),
            }
            chapters[first : last + 1] = [merged]
    # Only current may cite the pending input's reserved identity. Chapters
    # still cover committed history, so the input cannot be archived early.
    result["current"] = _points(write.current, history, None, current_input_id=current_input_id)
    return result


def _render_point(point, positions):
    spans = [span_ref(positions[ref["start"]], positions[ref["end"]]) for ref in point["refs"]]
    prefix = "（推断）" if point["kind"] == "inference" else ""
    citation = f" [{', '.join(spans)}]" if spans else ""
    return f"- {prefix}{point['text']}{citation}"


def render_chapter(index, chapter, history, *, positions=None):
    positions = positions or message_positions(history)
    span = span_ref(positions[chapter["start"]], positions[chapter["end"]])
    lines = [f"## C{index} {chapter['title']} [{span}]"]
    lines.extend(_render_point(point, positions) for point in chapter["points"])
    return "\n".join(lines)


def render_chapters(handoff, history):
    positions = message_positions(history)
    return "\n\n".join(
        render_chapter(i, c, history, positions=positions) for i, c in enumerate(handoff["chapters"], 1)
    )


def render_written_current(points):
    """The current-handoff rendering a successful write produces, from its tool arguments.

    Non-canonical spellings only cost one resend.
    """
    lines = [f"## {CURRENT_HEADING}"]
    for point in points:
        spans = [parse_span_ref(ref) for ref in dict.fromkeys(point.get("refs", []))]
        refs = ", ".join(span_ref(*span) for span in spans if span)
        prefix = "（推断）" if point.get("kind") == "inference" else ""
        citation = f" [{refs}]" if refs else ""
        lines.append(f"- {prefix}{point['text'].strip()}{citation}")
    return "\n".join(lines)


def render_current(handoff, history, *, current_input_id: str | None = None):
    if not handoff["current"]:
        return ""
    positions = message_positions(history)
    if current_input_id is not None:
        positions[current_input_id] = len(history) + 1
    return "\n".join([f"## {CURRENT_HEADING}", *(_render_point(p, positions) for p in handoff["current"])])


def render_text(handoff, history):
    """One-way projection shared by model context and the user's read-only view."""
    return "\n\n".join(
        part
        for part in (
            render_chapters(handoff, history),
            render_current(handoff, history),
        )
        if part
    )


def chapter_index(handoff, history):
    spans = chapter_spans(handoff, history)
    return [
        {"ref": f"C{i}", "title": c["title"], "range": span_ref(*span)}
        for i, (c, span) in enumerate(zip(handoff["chapters"], spans, strict=True), 1)
    ]


def chapter_token_counts(handoff, history, settings):
    positions = message_positions(history)
    return [
        count_text(render_chapter(index, chapter, history, positions=positions), settings)
        for index, chapter in enumerate(handoff["chapters"], 1)
    ]


def check_limits(handoff, history, config, settings, *, current_input_id: str | None = None):
    counts = chapter_token_counts(handoff, history, settings)
    for index, tokens in enumerate(counts, 1):
        if tokens > config.chapter_tokens:
            raise HandoffError(
                "CONTENT_LIMIT",
                f"C{index} 约 {tokens} tokens，超过单章上限 {config.chapter_tokens}；请精简要点",
            )
    total = sum(counts)
    if total > config.chapters_total_tokens:
        hint = "合并最旧的相邻章节，如 C1、C2" if len(handoff["chapters"]) > 1 else "精简章节"
        raise HandoffError(
            "CONTENT_LIMIT",
            f"章节总量约 {total} tokens，超过上限 {config.chapters_total_tokens}；请用 merge {hint}",
        )
    current = count_text(render_current(handoff, history, current_input_id=current_input_id), settings)
    if not handoff["current"] or current > config.handoff_tokens:
        raise HandoffError("CONTENT_LIMIT", f"当前交接不能为空，且须在 {config.handoff_tokens} tokens 内")


def require_coverage(handoff, history, start):
    """A rollover keeping messages from 0-based ``start`` must leave H1..H{start} covered."""
    spans = chapter_spans(handoff, history)
    covered = spans[-1][1] if spans else 0
    if covered < start:
        missing = span_ref(covered + 1, start)
        raise HandoffError(
            "HANDOFF_COVERAGE",
            f"章节尚未覆盖 {missing}，无法移出这些原文；请先让 Agent 整理交接，或增加保留的最近剧情消息。",
        )


def remap_handoff(handoff, ids):
    for item in [
        *handoff["chapters"],
        *(r for c in handoff["chapters"] for p in c["points"] for r in p["refs"]),
    ]:
        item["start"], item["end"] = ids.get(item["start"], item["start"]), ids.get(item["end"], item["end"])
    for ref in (r for p in handoff["current"] for r in p["refs"]):
        ref["start"], ref["end"] = ids.get(ref["start"], ref["start"]), ids.get(ref["end"], ref["end"])
