"""Branch-position H references shared by every story-history label.

``H{n}`` is the n-th message of the current branch chain, root first, so the
greeting is H1. A pending input or reply is numbered by the position it will
occupy once committed; accepting the turn never renumbers it. ``H41-H50`` is a
closed span and ``H52#3`` the third ``split_passages`` part of H52's text.

Only the rendering lives here. Storage keeps message IDs, fingerprints and
excerpts; callers decide which text is split and which parts were shown.

Positions remain stable while a branch only appends messages: existing positions
must not be inserted, deleted or reordered. Text edits preserve positions; reply
versions can occupy the same H, so H is a branch-local locator, never stored identity.
"""

import re
from collections.abc import Iterable, Mapping, Sequence

_MESSAGE_REF = re.compile(r"H([1-9]\d*)")


def message_ref(position: int) -> str:
    """1-based branch position → ``H{position}``."""
    return f"H{position}"


def span_ref(first: int, last: int) -> str:
    """Closed 1-based span; a single message renders as its own reference."""
    return message_ref(first) if first == last else f"{message_ref(first)}-{message_ref(last)}"


def passage_ref(position: int, part: int) -> str:
    """1-based passage ``part`` of the message at ``position`` → ``H{position}#{part}``."""
    return f"{message_ref(position)}#{part}"


def labeled(position: int, text: str) -> str:
    """Request text of a user message: its ``[H52]`` position line, then the text.

    Assistant prose stays unchanged, including native API replay. Exact assistant
    references are available from history tools; the model need not infer them.
    """
    return f"[{message_ref(position)}]\n{text}"


def parse_message_ref(ref: object) -> int | None:
    """``H52`` → 52. Passage refs, spans and any other value → None."""
    match = _MESSAGE_REF.fullmatch(ref) if isinstance(ref, str) else None
    return int(match[1]) if match else None


def parse_span_ref(ref: object) -> tuple[int, int] | None:
    """``H41-H50`` → (41, 50); ``H52`` → (52, 52). Reversed spans and other values → None."""
    if not isinstance(ref, str):
        return None
    first, _, last = ref.partition("-")
    first, last = parse_message_ref(first), parse_message_ref(last or first)
    return (first, last) if first is not None and last is not None and first <= last else None


def message_positions(history: Iterable[Mapping]) -> dict[str, int]:
    """Message ID → 1-based position on the branch chain ``history`` starts at."""
    return {message["id"]: position for position, message in enumerate(history, 1)}


def message_at(history: Sequence[Mapping], ref: object) -> Mapping | None:
    """The message ``ref`` names on ``history``, or None when it is not there."""
    position = parse_message_ref(ref)
    return history[position - 1] if position is not None and position <= len(history) else None
