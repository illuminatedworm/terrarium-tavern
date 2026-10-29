"""Conversation cache statistics from provider counters, never token estimates."""

from collections.abc import Iterable
from math import isfinite
from typing import TypedDict


class CacheUsage(TypedDict):
    prompt_tokens: int | float | None
    cached_tokens: int | float | None


def _counter(value):
    return value if type(value) in (int, float) and isfinite(value) and value >= 0 else None


def _first(*values):
    return next((value for value in values if value is not None), None)


def _cache_usage(usage: dict | None) -> CacheUsage:
    if not usage:
        return {"prompt_tokens": None, "cached_tokens": None}
    # The transport's numeric accounting can synthesize zeros. Raw usage is
    # authoritative, including an empty object or a missing counter in a batch.
    if "raw_usage" in usage:
        raw = usage["raw_usage"]
        if isinstance(raw, list):
            return aggregate_cache_usage(raw) or {"prompt_tokens": None, "cached_tokens": None}
        return _cache_usage(raw)
    details = usage.get("prompt_tokens_details") or usage.get("input_tokens_details") or {}
    cached = _counter(
        _first(
            details.get("cached_tokens"),
            usage.get("cache_read_input_tokens"),
            usage.get("cached_tokens"),
            usage.get("cachedContentTokenCount"),
        )
    )
    prompt = _counter(
        _first(usage.get("prompt_tokens"), usage.get("input_tokens"), usage.get("promptTokenCount"))
    )
    if "input_tokens" in usage and "cache_read_input_tokens" in usage:
        # Anthropic reports the three disjoint input buckets. A missing bucket
        # is unknown, not an implicit zero or a smaller denominator.
        created = _counter(usage.get("cache_creation_input_tokens"))
        prompt = prompt + cached + created if None not in (prompt, cached, created) else None
    if prompt is not None and cached is not None and cached > prompt:
        cached = None
    return {"prompt_tokens": prompt, "cached_tokens": cached}


def input_tokens(usage: dict | None, api=None):
    """A single provider sample, never a synthesized zero or a batch sum."""
    if not usage or isinstance(usage.get("raw_usage"), list):
        return None
    raw = usage.get("raw_usage", usage)
    if not isinstance(raw, dict):
        return None
    raw, _ = _input_observation(raw, api)
    if raw is None:
        return None
    if (
        api == "anthropic"
        or "input_tokens" in raw
        and any(k in raw for k in ("cache_read_input_tokens", "cache_creation_input_tokens"))
    ):
        buckets = [
            _counter(raw.get(k))
            for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
        ]
        return sum(buckets) if all(v is not None for v in buckets) else None
    return _counter(_first(raw.get("prompt_tokens"), raw.get("input_tokens"), raw.get("promptTokenCount")))


def _input_observation(raw, api):
    if api == "anthropic" and raw.get("iterations") is not None:
        # Server tools can sample repeatedly. Billing sums those iterations,
        # whereas the last main-model sample describes the observed window.
        # Compaction/advisor sub-inferences never stand in for that sample.
        iterations = raw["iterations"]
        last = (
            next(
                (
                    item
                    for item in reversed(iterations)
                    if isinstance(item, dict) and item.get("type") == "message"
                ),
                None,
            )
            if isinstance(iterations, list)
            else None
        )
        return last, "last_iteration"
    return raw, "request"


def _details(raw, key):
    value = raw.get(key)
    return value if isinstance(value, dict) else {}


def _modalities(raw, key):
    value = raw.get(key)
    if not isinstance(value, list):
        return None
    if any(
        not isinstance(item, dict)
        or not isinstance(item.get("modality"), str)
        or _counter(item.get("tokenCount")) is None
        for item in value
    ):
        return None
    return [{"modality": item["modality"], "tokens": item["tokenCount"]} for item in value]


def observed_usage(usage: dict | None, api: str, response: dict | None = None):
    """Provider-reported facts, without guessing a post-response context size.

    Output fields describe the entire response even when input_scope selects
    its last main-model iteration. They are never added to the window meter.
    Missing fields and explicit zero remain distinct, including totals.
    """
    response = response or {}
    raw = (usage or {}).get("raw_usage", usage) or {}
    raw = raw if isinstance(raw, dict) else {}
    incoming, scope = _input_observation(raw, api)
    incoming = incoming or {}
    details = _details(incoming, "input_tokens_details") or _details(incoming, "prompt_tokens_details")
    output_details = _details(raw, "output_tokens_details") or _details(raw, "completion_tokens_details")
    result = {
        "tokens": input_tokens({"raw_usage": raw}, api),
        "input_scope": scope,
        "output_tokens": _counter(
            raw.get("candidatesTokenCount")
            if api == "gemini"
            else raw.get("completion_tokens")
            if api == "chat_completions"
            else raw.get("output_tokens")
        ),
        "thinking_tokens": _counter(
            raw.get("thoughtsTokenCount")
            if api == "gemini"
            else output_details.get("thinking_tokens")
            if api == "anthropic"
            else output_details.get("reasoning_tokens")
        ),
        "output_includes_thinking": api != "gemini",
        "reported_total_tokens": _counter(
            raw.get("totalTokenCount") if api == "gemini" else raw.get("total_tokens")
        ),
        "cached_input_tokens": _counter(
            _first(
                incoming.get("cachedContentTokenCount"),
                incoming.get("cache_read_input_tokens"),
                details.get("cached_tokens"),
            )
        ),
        "cache_write_input_tokens": _counter(
            _first(incoming.get("cache_creation_input_tokens"), details.get("cache_write_tokens"))
        ),
        "input_modalities": _modalities(raw, "promptTokensDetails"),
        "output_modalities": _modalities(raw, "candidatesTokensDetails"),
        "candidate_tokens": None,
        "tool_prompt_tokens": _counter(raw.get("toolUsePromptTokenCount")),
        "reasoning_context": None,
        "context_edits": None,
        "input_transformations": None,
        "compacted": any(
            item.get("type") == "compaction" for item in response.get("output", []) if isinstance(item, dict)
        ),
        "model": response.get("model", response.get("modelVersion")),
    }
    if api == "gemini":
        candidates = response.get("candidates") or []
        if candidates:
            result["candidate_tokens"] = _counter(candidates[0].get("tokenCount"))
    if api == "responses":
        context = _details(response, "reasoning").get("context")
        result["reasoning_context"] = context if isinstance(context, str) else None
    if api == "anthropic":
        edits = _details(response, "context_management").get("applied_edits")
        if isinstance(edits, list) and all(
            isinstance(item, dict) and isinstance(item.get("type"), str) for item in edits
        ):
            result["context_edits"] = [
                {
                    "type": item["type"],
                    **{
                        key: item[key]
                        for key in ("cleared_input_tokens", "cleared_thinking_turns", "cleared_tool_uses")
                        if _counter(item.get(key)) is not None
                    },
                }
                for item in edits
            ]
        transformations = response.get("input_transformations")
        if isinstance(transformations, list) and all(
            isinstance(item, dict) and isinstance(item.get("type"), str) for item in transformations
        ):
            counts = {}
            for item in transformations:
                counts[item["type"]] = counts.get(item["type"], 0) + 1
            result["input_transformations"] = counts
    return result


def aggregate_cache_usage(usages: Iterable[dict | None]) -> CacheUsage | None:
    """Sum counters before division; any missing sample keeps its total unknown."""
    totals: CacheUsage | None = None
    for usage in usages:
        sample = _cache_usage(usage)
        if totals is None:
            totals = sample
        else:
            for key in ("prompt_tokens", "cached_tokens"):
                totals[key] = (
                    totals[key] + sample[key] if totals[key] is not None and sample[key] is not None else None
                )
    return totals
