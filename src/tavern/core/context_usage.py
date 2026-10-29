"""Latest provider observations, independent of billing totals and story commits."""

import json
import time
from typing import Literal, TypedDict

from tavern.core.cache_policy import fingerprint
from tavern.core.tokens import counting_payload
from tavern.core.usage import observed_usage

OBSERVATION_VERSION = 3


class RequestMeasure(TypedDict):
    api: str
    identity: str
    blocks: list[str]
    output_limit: int | None


class ContextUsage(TypedDict):
    tokens: int | float | None
    source: Literal["provider", "unavailable"]
    coverage: Literal["exact", "appended", "changed", "unavailable"]
    added_blocks: int | None
    pending: bool
    observed_at: float | None
    unavailable_reason: str | None
    input_scope: Literal["request", "last_iteration"] | None
    output_tokens: int | float | None
    thinking_tokens: int | float | None
    output_includes_thinking: bool
    reported_total_tokens: int | float | None
    cached_input_tokens: int | float | None
    cache_write_input_tokens: int | float | None
    input_modalities: list[dict] | None
    output_modalities: list[dict] | None
    candidate_tokens: int | float | None
    tool_prompt_tokens: int | float | None
    reasoning_context: str | None
    context_edits: list[dict] | None
    input_transformations: dict[str, int] | None
    compacted: bool
    model: str | None
    overflow: bool
    last_attempt: (
        Literal[
            "pending", "completed", "request_failed", "request_cancelled", "interrupted", "context_overflow"
        ]
        | None
    )


def context_overflow(error):
    """Only explicit provider context-limit rejection authorizes smaller batches."""
    if getattr(error, "code", None) == "context_length_exceeded":
        return True
    text = str(error).lower()
    # OpenAI-compatible, Anthropic ("prompt is too long") and Gemini ("input
    # token count ... exceeds the maximum number of tokens") rejection texts.
    # Not "too many tokens": Bedrock throttling says "Too many tokens, please
    # wait", and a rate limit must never retire history or halve a batch.
    return any(
        word in text
        for word in (
            "context_length",
            "context window",
            "maximum context",
            "prompt is too long",
            "exceeds the maximum number of tokens",
        )
    )


def describe(messages, settings, tools=None, *, instructions="") -> RequestMeasure:
    from tavern.scripting.request_context import response_origin

    definitions = [tool.to_api_format() if hasattr(tool, "to_api_format") else tool for tool in tools or []]
    return {
        "api": settings.api_mode,
        # Output reservations affect rejection, not measured input occupancy.
        # The subscription route does not send a configurable output limit.
        "output_limit": None if settings.auth_mode == "chatgpt_subscription" else settings.reply_tokens,
        "identity": fingerprint(
            [
                OBSERVATION_VERSION,
                response_origin(settings),
                settings._context_generation,
                definitions,
                settings.reasoning_effort,
                instructions,
            ]
        ),
        # Matching only proves what was observed; it never estimates additions.
        "blocks": [fingerprint(counting_payload(message, settings)) for message in messages],
    }


def resolve(current: RequestMeasure, sample: dict | None = None) -> ContextUsage:
    result: ContextUsage = {
        **observed_usage(None, current["api"]),
        "source": "unavailable",
        "coverage": "unavailable",
        "added_blocks": None,
        "pending": False,
        "observed_at": None,
        "input_scope": None,
        "unavailable_reason": "no_snapshot",
        "overflow": False,
        "last_attempt": None,
    }
    if not sample or sample.get("version") != OBSERVATION_VERSION:
        return result
    result["pending"] = sample["pending"]
    result["last_attempt"] = sample["last_attempt"]
    record = sample["previous"] if sample["pending"] else sample
    if not record:
        return result
    if record["request"]["identity"] != current["identity"]:
        result["unavailable_reason"] = "source_changed"
        return result
    observation = record["observation"]
    blocks = record["request"]["blocks"]
    intact = current["blocks"][: len(blocks)] == blocks
    if observation is None:
        result["unavailable_reason"] = record["unavailable_reason"]
        # A rejection may concern input plus the reserved output. Reducing
        # that reservation can make the same input fit; successful input
        # measurements remain independent of this extra rejection check.
        result["overflow"] = (
            intact
            and record["unavailable_reason"] == "context_overflow"
            and record["request"]["output_limit"] == current["output_limit"]
        )
        return result
    same = intact and len(current["blocks"]) == len(blocks)
    result.update(
        **observation,
        source="provider" if observation["tokens"] is not None else "unavailable",
        coverage="exact" if same else "appended" if intact else "changed",
        added_blocks=len(current["blocks"]) - len(blocks) if intact else None,
        unavailable_reason=None if observation["tokens"] is not None else "missing_usage",
    )
    return result


def pressure_tokens(usage: ContextUsage):
    """Only an intact, completed observation may drive the next window action."""
    return usage["tokens"] if not usage["pending"] and usage["coverage"] in ("exact", "appended") else None


def exceeds(usage: ContextUsage, limit, *, inclusive=False):
    # Every caller's limit is within the model window that rejected the request.
    if usage["overflow"] and not usage["pending"]:
        return True
    tokens = pressure_tokens(usage)
    return tokens is not None and (tokens >= limit if inclusive else tokens > limit)


class ContextMeter:
    def __init__(self, store, kind, owner):
        self.store, self.owner = store, f"{kind}:{owner}"

    def __deepcopy__(self, memo):
        return self

    def usage(self, current, lane):
        with self.store.connection() as db:
            row = db.execute(
                "SELECT payload FROM context_token_samples WHERE owner=? AND lane=?", (self.owner, lane)
            ).fetchone()
        return resolve(current, json.loads(row[0]) if row else None)

    def begin(self, current, lane):
        """Start an observation; also return what usage() saw just before it."""
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT sequence,payload FROM context_token_samples WHERE owner=? AND lane=?",
                (self.owner, lane),
            ).fetchone()
            sequence = row["sequence"] + 1 if row else 1
            previous = json.loads(row["payload"]) if row else None
            if previous and previous.get("version") != OBSERVATION_VERSION:
                previous = None
            before = resolve(current, previous)
            if previous and previous["pending"]:
                previous = previous["previous"]
            payload = {
                "version": OBSERVATION_VERSION,
                "request": current,
                "observation": None,
                "pending": True,
                "previous": previous,
                "unavailable_reason": None,
                "last_attempt": "pending",
            }
            # Keep only one completed predecessor while in flight, never a chain.
            db.execute(
                "INSERT INTO context_token_samples(owner,lane,sequence,payload) VALUES (?,?,?,?) "
                "ON CONFLICT(owner,lane) DO UPDATE SET sequence=excluded.sequence,payload=excluded.payload",
                (self.owner, lane, sequence, json.dumps(payload)),
            )
        return sequence, before

    def _save(self, current, lane, sequence, observation, reason=None, *, restore=False):
        payload = {
            "version": OBSERVATION_VERSION,
            "request": current,
            "observation": observation,
            "pending": False,
            "previous": None,
            "unavailable_reason": reason,
            "last_attempt": reason or "completed",
        }
        # Retain the last completed evidence without making it look like the
        # latest attempt succeeded. The UI receives both facts from this row.
        value = (
            "json_set(COALESCE(json_extract(payload,'$.previous'),?), '$.last_attempt', ?)"
            if restore
            else "?"
        )
        values = (json.dumps(payload), reason) if restore else (json.dumps(payload),)
        with self.store.connection() as db:
            db.execute(
                f"UPDATE context_token_samples SET payload={value} WHERE owner=? AND lane=? AND sequence=?",
                (*values, self.owner, lane, sequence),
            )

    def finish(self, current, lane, sequence, usage, response=None):
        observation = {
            **observed_usage(usage, current["api"], response),
            "observed_at": time.time(),
        }
        self._save(current, lane, sequence, observation)
        return observation

    def fail(self, current, lane, sequence, *, overflow=False, cancelled=False):
        if overflow:
            # A definite rejection is stronger evidence than any earlier sample.
            self._save(current, lane, sequence, None, "context_overflow")
        else:
            # A cancelled or failed attempt proves nothing about size; the last
            # completed record (a sample or an overflow) still drives the window.
            self._save(
                current,
                lane,
                sequence,
                None,
                "request_cancelled" if cancelled else "request_failed",
                restore=True,
            )


def recover_samples(db):
    # Restart interrupts the observation, not the preceding completed record,
    # which is restored as after any other failure. Advancing the sequence
    # also rejects any late callback from that attempt.
    db.execute(
        "UPDATE context_token_samples SET sequence=sequence+1, "
        "payload=json_set(COALESCE(json_extract(payload,'$.previous'),"
        "json_set(payload,'$.pending',json('false'),'$.unavailable_reason','interrupted')),"
        "'$.last_attempt','interrupted') "
        "WHERE json_extract(payload,'$.pending')=1"
    )


def retire_estimated_pressure(db):
    """Schema 16 invalidates derived vetoes as well as calibration samples.

    Keep accepted work and the original trace intact. The next compiler either
    replaces the old runtime notice or appends its explicit pressure-clear frame.
    """
    for table, path in (
        ("full_agent_progress", "$.must_rollover"),
        ("card_author_progress", "$.pending_calls.forced"),
        ("card_author_progress", "$.context.frames.pressure.forced"),
        ("card_edit_contexts", "$.frames.pressure.forced"),
    ):
        db.execute(
            f"UPDATE {table} SET payload=json_set(payload,?,json('false')) WHERE json_extract(payload,?)=1",
            (path, path),
        )


def bind_meter(settings, store, kind, owner):
    settings._context_meter = ContextMeter(store, kind, owner)
    return settings


def meter_lane(settings, purpose="narrative"):
    lane = settings._adapter_lane
    return lane if purpose in ("narrative", "full_agent", "card_author", "advisor") else lane + ":" + purpose


def context_usage(messages, settings, tools=None, *, purpose="narrative") -> ContextUsage:
    current = describe(messages, settings, tools)
    meter = settings._context_meter
    return meter.usage(current, meter_lane(settings, purpose)) if meter else resolve(current)


def context_tokens(messages, settings, tools=None, *, purpose="narrative"):
    return pressure_tokens(context_usage(messages, settings, tools, purpose=purpose))


def apply_usage(context, settings, tools=None):
    context["context_usage"] = context_usage(
        context["messages"], settings, tools if tools is not None else context.get("tools")
    )
    return context
