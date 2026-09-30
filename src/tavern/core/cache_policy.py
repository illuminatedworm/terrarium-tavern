"""Cache configuration and protocol-independent request boundaries."""
import copy
import hashlib
import json
from typing import Any, Literal
from urllib.parse import quote, urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

API_MODES = ("chat_completions", "responses", "anthropic", "gemini")
RESERVED = {"model", "messages", "input", "contents", "system", "systemInstruction", "instructions",
            "tools", "tool_choice", "toolConfig", "stream", "stream_options", "generationConfig",
            "max_tokens", "max_output_tokens", "temperature", "api_key", "headers", "authorization",
            "cachedContent", "previous_response_id", "conversation", "store", "top_p", "top_k",
            "frequency_penalty", "presence_penalty", "max_completion_tokens", "reasoning", "reasoning_effort",
            "parallel_tool_calls", "include", "stop", "stop_sequences", "thinking"}


class CacheSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["cloud", "runtime"] = "cloud"
    preset: Literal["auto", "custom"] = "auto"
    field: str = Field(default="cache_control", min_length=1, max_length=100)
    value: Any = Field(default_factory=lambda: {"type": "ephemeral"})
    request: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def safe_extensions(self):
        if self.field.strip() != self.field or self.field.lower() in RESERVED | {"type", "text", "role", "content"}:
            raise ValueError("缓存断点字段不能覆盖消息结构")
        forbidden = RESERVED | {"extra_body", "input_tokens", "output_tokens"}
        if any(key.lower() in {k.lower() for k in forbidden} for key in self.request):
            raise ValueError("缓存参数不能覆盖模型、消息、工具或请求控制字段")
        try:
            encoded = json.dumps([self.value, self.request], allow_nan=False)
        except (ValueError, TypeError) as error:
            raise ValueError("缓存参数必须是合法 JSON") from error
        if len(encoded) > 16384:
            raise ValueError("缓存参数不能超过 16 KiB")
        return self


def runtime_cache():
    return CacheSettings(mode="runtime")


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def preset(settings):
    cache = settings.cache
    if cache.preset == "custom":
        return {"kind": "block", "field": cache.field, "value": copy.deepcopy(cache.value),
                "request": copy.deepcopy(cache.request)}
    model = settings.model.lower()
    if settings.api_mode == "gemini" or (settings.api_mode != "anthropic" and "gemini" in model):
        return {"kind": "gemini", "request": copy.deepcopy(cache.request)}
    if settings.api_mode == "anthropic" or "claude" in model:
        return {"kind": "block", "field": "cache_control", "value": {"type": "ephemeral"},
                "request": copy.deepcopy(cache.request)}
    if "gpt" in model or model.startswith(("o1", "o3", "o4")):
        return {"kind": "block", "field": "prompt_cache_breakpoint", "value": {"mode": "explicit"},
                "request": {"prompt_cache_options": {"mode": "explicit"}, **copy.deepcopy(cache.request)}}
    return {"kind": "unknown", "request": {}}


def endpoint(base_url, api, model="", *, stream=False, resource=None):
    """Accept a root or an API root; preserve deployment-specific path prefixes."""
    parsed = urlsplit(base_url)
    path = parsed.path.rstrip("/")
    if parsed.query or parsed.fragment or parsed.username or parsed.password:
        raise ValueError("接口地址不能包含查询参数、片段或登录凭据")
    if api in ("chat_completions", "responses", "anthropic"):
        suffix = {"chat_completions": "/chat/completions", "responses": "/responses", "anthropic": "/messages"}[api]
        if path.endswith(suffix):
            path = path[:-len(suffix)]
        if not path:
            path = "/v1"
        elif api == "anthropic" and not path.endswith("/v1"):
            path += "/v1"
        path += suffix
    else:
        if path.endswith("/v1"):
            path = path[:-3]
        if path.endswith("/openai"):
            path = path[:-7]
        if not path.endswith(("/v1beta", "/v1alpha")):
            path += "/v1beta"
        if resource is not None:
            path += "/" + resource.lstrip("/")
        else:
            path += "/models/" + quote(model.removeprefix("models/"), safe="-._")
            path += ":streamGenerateContent" if stream else ":generateContent"
    return urlunsplit((parsed.scheme, parsed.netloc, path, "alt=sse" if stream and api == "gemini" else "", ""))


def normalize_usage(raw, api):
    raw = copy.deepcopy(raw or {})
    details = raw.get("prompt_tokens_details") or raw.get("input_tokens_details") or {}
    if api == "gemini":
        prompt = raw.get("promptTokenCount", 0)
        output = raw.get("candidatesTokenCount", 0) + raw.get("thoughtsTokenCount", 0)
        read, write = raw.get("cachedContentTokenCount"), None
    elif api == "anthropic":
        read, write = raw.get("cache_read_input_tokens"), raw.get("cache_creation_input_tokens")
        prompt = raw.get("input_tokens", 0) + (read or 0) + (write or 0)
        output = raw.get("output_tokens", 0)
    else:
        prompt = raw.get("input_tokens", 0) if api == "responses" else raw.get("prompt_tokens", 0)
        output = raw.get("output_tokens", 0) if api == "responses" else raw.get("completion_tokens", 0)
        read = details.get("cached_tokens", raw.get("cache_read_input_tokens"))
        # Relays use all three spellings. Some Chat endpoints
        # report cache_creation_tokens; retain a real zero and skip null aliases.
        write = next((value for value in (
            details.get("cache_write_tokens"), details.get("cached_creation_tokens"),
            details.get("cache_creation_tokens"), raw.get("cache_creation_input_tokens"))
            if value is not None), None)
        if write is None and raw.get("usage_source") == "anthropic":
            fields = ("claude_cache_creation_5_m_tokens", "claude_cache_creation_1_h_tokens")
            if all(k in raw for k in fields):
                write = sum(raw[k] for k in fields)
    return {"prompt_tokens": prompt, "completion_tokens": output, "total_tokens": prompt + output,
            "cached_tokens": read, "cache_write_tokens": write, "raw_usage": raw}
