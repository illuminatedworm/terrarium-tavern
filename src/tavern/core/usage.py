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
    cached = _counter(_first(details.get("cached_tokens"), usage.get("cache_read_input_tokens"),
                             usage.get("cached_tokens"), usage.get("cachedContentTokenCount")))
    prompt = _counter(_first(usage.get("prompt_tokens"), usage.get("input_tokens"), usage.get("promptTokenCount")))
    if "input_tokens" in usage and "cache_read_input_tokens" in usage:
        # Anthropic reports the three disjoint input buckets. A missing bucket
        # is unknown, not an implicit zero or a smaller denominator.
        created = _counter(usage.get("cache_creation_input_tokens"))
        prompt = prompt + cached + created if None not in (prompt, cached, created) else None
    if prompt is not None and cached is not None and cached > prompt:
        cached = None
    return {"prompt_tokens": prompt, "cached_tokens": cached}


def aggregate_cache_usage(usages: Iterable[dict | None]) -> CacheUsage | None:
    """Sum counters before division; any missing sample keeps its total unknown."""
    totals: CacheUsage | None = None
    for usage in usages:
        sample = _cache_usage(usage)
        if totals is None:
            totals = sample
        else:
            for key in ("prompt_tokens", "cached_tokens"):
                totals[key] = (totals[key] + sample[key]
                               if totals[key] is not None and sample[key] is not None else None)
    return totals
