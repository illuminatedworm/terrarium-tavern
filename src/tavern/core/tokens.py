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
    return {"unit": "tokens", "tokenizer": selected, "source": source, "estimated": True,
            "message_framing": "estimated", "model": model}


@lru_cache(maxsize=2)
def encoding(name):
    metadata = json.loads((DATA / "manifest.json").read_text("utf-8"))[name]
    raw = gzip.decompress((DATA / f"{name}.tiktoken.gz").read_bytes())
    if hashlib.sha256(raw).hexdigest() != metadata["sha256"]:
        raise ValueError("Token 分词器词表校验失败，请重新安装")
    ranks = {base64.b64decode(token): int(rank) for token, rank in (line.split() for line in raw.splitlines())}
    return tiktoken.Encoding(name=name, pat_str=metadata["pat_str"], mergeable_ranks=ranks,
                             special_tokens=metadata["special_tokens"])


@lru_cache(maxsize=1024)
def _short_count(text, name):
    return len(encoding(name).encode_ordinary(text))


def count_text(text, settings=None):
    name = counting_info(settings)["tokenizer"]
    value = str(text) if text is not None else ""
    # Cache small repeated blocks, not entire private conversations indefinitely.
    return _short_count(value, name) if len(value) <= 16384 else len(encoding(name).encode_ordinary(value))


def message_tokens(message, settings=None):
    content = message.get("content") or ""
    if not isinstance(content, str):
        content = json.dumps(content, ensure_ascii=False, separators=(",", ":"))
    cost = count_text(content, settings) + count_text(message.get("role", ""), settings) + MESSAGE_FRAMING
    # Context tracing fields are local metadata, never part of the model input.
    extra = {key: message[key] for key in ("name", "tool_calls", "tool_call_id", "function_call") if key in message}
    if extra:
        cost += count_text(json.dumps(extra, ensure_ascii=False, separators=(",", ":")), settings)
    return cost


def request_tokens(messages, settings=None, tools=None):
    cost = REPLY_PRIMING + sum(message_tokens(message, settings) for message in messages)
    if tools:
        cost += count_text(json.dumps(tools, ensure_ascii=False, separators=(",", ":")), settings)
    return cost
