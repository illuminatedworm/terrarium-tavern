"""Preview-only local tokenizer reference for comparing relative block sizes.

Nothing here feeds pressure, rollover, compaction, warnings or batching, and
nothing is written into ``context_usage`` or a stored turn preview. Requests
compile without it; only an explicit context preview asks for this figure.
"""

from tavern.core.tokens import (
    BUSINESS_FIELDS,
    OPAQUE_FIELDS,
    REPLY_PRIMING,
    counting_info,
    counting_payload,
    payload_tokens,
    request_tokens,
)

IMAGE_TYPES = {"image_url", "input_image", "image"}
ENCRYPTED_TYPES = {"compaction", "redacted_thinking", "reasoning.encrypted"}


def _excluded(value, found):
    """Collect what the local count leaves out; business arguments are text."""
    if isinstance(value, list):
        for item in value:
            _excluded(item, found)
    elif isinstance(value, dict):
        if value.get("type") in IMAGE_TYPES or "inlineData" in value:
            found.add("image")
        elif value.get("type") in ENCRYPTED_TYPES:
            found.add("encrypted")
        for key, item in value.items():
            if key in OPAQUE_FIELDS:
                found.add("encrypted")
            elif key not in BUSINESS_FIELDS:
                _excluded(item, found)
    return found


def annotate_rows(rows, settings):
    """Copy rows with their local count; opaque runtime state stays unknown."""
    result = []
    for row in rows:
        payload = counting_payload(row, settings)
        excluded = sorted(_excluded(payload, set()))
        result.append(
            {
                **row,
                "local_tokens": None if row.get("opaque") else payload_tokens(payload, settings),
                "local_excluded": ["encrypted"] if row.get("opaque") else excluded,
            }
        )
    return result


def estimate(messages, rows, settings, tools=None):
    info = counting_info(settings)
    return {
        "unit": "tokens",
        "tokens": request_tokens(messages, settings, tools),
        "tool_definitions": request_tokens([], settings, tools) - REPLY_PRIMING if tools else 0,
        "tokenizer": info["tokenizer"],
        "tokenizer_source": info["source"],
        "image_blocks": sum("image" in row["local_excluded"] for row in rows),
        "encrypted_blocks": sum("encrypted" in row["local_excluded"] for row in rows),
    }


def attach_local_estimate(preview, settings, tools=None):
    """Add the reference to a preview response; the request messages are untouched."""
    preview["trace"] = annotate_rows(preview["trace"], settings)
    preview["local_estimate"] = estimate(
        preview["messages"], preview["trace"], settings, preview.get("tools") if tools is None else tools
    )
    return preview
