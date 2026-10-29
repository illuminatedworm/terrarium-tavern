"""Local BPE token counts, with explicit model mapping and estimation provenance.

Text is counted using an actual tokenizer, never bytes/characters divided by a
constant. Chat framing and unknown providers remain estimates; provider usage is
the authority after a request. Vocabularies are bundled for offline operation.
"""

import base64
import gzip
import hashlib
import json
from functools import lru_cache
from pathlib import Path

import tiktoken

DATA = Path(__file__).parents[1] / "tokenizer_data"
SUPPORTED = {"o200k_base", "cl100k_base"}
REPLY_PRIMING = 3
MESSAGE_FRAMING = 4
# Kept on both the durable editor message and its hydrated request copy. These
# references are immutable within their owning conversation, unlike base64 wire
# encoding; protocol adapters never send this local metadata to the model.
IMAGE_REFS = "_editor_image_refs"
OPAQUE_TYPES = {
    "compaction",
    "redacted_thinking",
    "reasoning.encrypted",
    "image_url",
    "input_image",
    "image",
}
OPAQUE_FIELDS = {"encrypted_content", "thoughtSignature", "thought_signature", "signature", "inlineData"}
BUSINESS_FIELDS = {"args", "arguments", "input", "response"}


def counting_info(settings=None):
    model = getattr(settings, "model", "")
    selected = getattr(settings, "tokenizer", "auto")
    source = "configured"
    if selected == "auto":
        try:
            selected = tiktoken.encoding_name_for_model(model)
        except KeyError:
            selected = None
        source = "model_mapping" if selected in SUPPORTED else "reference_fallback"
        if selected not in SUPPORTED:
            selected = "o200k_base"
    return {
        "unit": "tokens",
        "tokenizer": selected,
        "source": source,
        "estimated": True,
        "message_framing": "estimated",
        "model": model,
    }


@lru_cache(maxsize=2)
def encoding(name):
    metadata = json.loads((DATA / "manifest.json").read_text("utf-8"))[name]
    raw = gzip.decompress((DATA / f"{name}.tiktoken.gz").read_bytes())
    if hashlib.sha256(raw).hexdigest() != metadata["sha256"]:
        raise ValueError("Token 分词器词表校验失败，请重新安装")
    ranks = {
        base64.b64decode(token): int(rank) for token, rank in (line.split() for line in raw.splitlines())
    }
    return tiktoken.Encoding(
        name=name,
        pat_str=metadata["pat_str"],
        mergeable_ranks=ranks,
        special_tokens=metadata["special_tokens"],
    )


@lru_cache(maxsize=1024)
def _short_count(text, name):
    return len(encoding(name).encode_ordinary(text))


def count_text(text, settings=None):
    name = counting_info(settings)["tokenizer"]
    value = str(text) if text is not None else ""
    # Cache small repeated blocks, not entire private conversations indefinitely.
    return _short_count(value, name) if len(value) <= 16384 else len(encoding(name).encode_ordinary(value))


def visible_payload(value):
    """Counting projection only: opaque state must still be replayed unchanged."""
    if isinstance(value, list):
        return [visible_payload(item) for item in value]
    if isinstance(value, dict):
        if value.get("type") in OPAQUE_TYPES:
            return {"type": value["type"]}
        return {
            # These are model/tool business data, not provider metadata. A user
            # is allowed to have a field named signature or encrypted_content.
            key: item if key in BUSINESS_FIELDS else visible_payload(item)
            for key, item in value.items()
            if key not in OPAQUE_FIELDS
        }
    return value


def counting_payload(message, settings=None):
    """Select the protocol's actual content, without its portable shadow copies."""
    native = message.get("_native_response")
    if native:
        return {"role": message.get("role", ""), "native": native["items"]}
    api = getattr(settings, "api_mode", "chat_completions")
    details = message.get("reasoning_details") or []
    native = next((d for d in details if isinstance(d, dict) and d.get("provider") == api), None)
    if native and api == "gemini" and message.get("role") == "assistant":
        return {"role": "assistant", "native": native["parts"]}
    keys = ["role", "content", "name", "tool_calls", "tool_call_id", "function_call"]
    if api == "chat_completions":
        keys.append("reasoning_content")
        if not any(d.get("provider") in ("gemini", "anthropic") for d in details if isinstance(d, dict)):
            keys.append("reasoning_details")
    elif api == "responses":
        keys.append("reasoning")
    result = {key: message[key] for key in keys if key in message}
    if refs := message.get(IMAGE_REFS):
        # materialize_images appends the original images to the user text;
        # wrapping may add text parts on either side. Count their complete text
        # once and identify the images by the same immutable references in both
        # preview and transport, without loading blobs during a stats refresh.
        content = result["content"]
        text = (
            content
            if isinstance(content, str)
            else "".join(p["text"] for p in content if p.get("type") == "text")
        )
        result["content"] = [
            {"type": "text", "text": text},
            *({"type": "image", "ref": ref} for ref in refs),
        ]
    if native and api == "anthropic":
        result["thinking"] = native.get("thinking", [])
    if api == "anthropic" and message.get("_tool_error"):
        result["is_error"] = True
    return result


def message_tokens(message, settings=None):
    return payload_tokens(counting_payload(message, settings), settings)


def payload_tokens(payload, settings=None):
    payload = visible_payload(payload)
    if "native" in payload:
        return (
            count_text(json.dumps(payload["native"], ensure_ascii=False, separators=(",", ":")), settings)
            + count_text(payload["role"], settings)
            + MESSAGE_FRAMING
        )
    content = payload.get("content") or ""
    if isinstance(content, list):
        # Base64 is transport encoding, not rendered model text. Vision usage
        # is unknown locally and is reported separately from this text estimate.
        content = [
            part
            for part in content
            if not isinstance(part, dict)
            or part.get("type") not in ("image_url", "input_image", "image")
            and "inlineData" not in part
        ]
    if not isinstance(content, str):
        content = json.dumps(content, ensure_ascii=False, separators=(",", ":"))
    cost = count_text(content, settings) + count_text(payload.get("role", ""), settings) + MESSAGE_FRAMING
    # Context tracing fields are local metadata, never part of the model input.
    extra = {
        key: payload[key]
        for key in ("name", "tool_calls", "tool_call_id", "function_call", "is_error")
        if key in payload
    }
    if extra:
        cost += count_text(json.dumps(extra, ensure_ascii=False, separators=(",", ":")), settings)
    reasoning = {
        key: payload[key]
        for key in ("reasoning_content", "reasoning_details", "reasoning", "thinking")
        if key in payload
    }
    if reasoning:
        # Count Unicode text rather than JSON's ASCII escape representation.
        # Native attachments return above: never count their shadow fields twice.
        cost += count_text(json.dumps(reasoning, ensure_ascii=False), settings)
    return cost


def request_tokens(messages, settings=None, tools=None):
    return request_payload_tokens(
        (counting_payload(message, settings) for message in messages), settings, tools
    )


def request_payload_tokens(payloads, settings=None, tools=None):
    cost = REPLY_PRIMING + sum(payload_tokens(payload, settings) for payload in payloads)
    if tools:
        cost += count_text(json.dumps(tools, ensure_ascii=False, separators=(",", ":")), settings)
    return cost
