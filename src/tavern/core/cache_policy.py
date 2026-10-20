"""Cache configuration and protocol-independent request boundaries."""

import copy
import hashlib
import json
import re
from typing import Any, Literal
from urllib.parse import quote, urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

from tavern.core.gemini_capabilities import gemini_controls

API_MODES = ("chat_completions", "responses", "anthropic", "gemini")
RESERVED = {
    "model",
    "messages",
    "input",
    "contents",
    "system",
    "systemInstruction",
    "instructions",
    "tools",
    "tool_choice",
    "toolConfig",
    "stream",
    "stream_options",
    "generationConfig",
    "max_tokens",
    "max_output_tokens",
    "temperature",
    "api_key",
    "headers",
    "authorization",
    "cachedContent",
    "previous_response_id",
    "conversation",
    "store",
    "top_p",
    "top_k",
    "frequency_penalty",
    "presence_penalty",
    "max_completion_tokens",
    "reasoning",
    "reasoning_effort",
    "parallel_tool_calls",
    "include",
    "stop",
    "stop_sequences",
    "thinking",
    "output_config",
}


class CacheSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["cloud", "runtime"] = "cloud"
    preset: Literal["auto", "custom"] = "auto"
    field: str = Field(default="cache_control", min_length=1, max_length=100)
    value: Any = Field(default_factory=lambda: {"type": "ephemeral"})
    request: dict = Field(default_factory=dict)
    retention: Literal["default", "5m", "15m", "1h", "24h", "30m", "in_memory"] = "default"

    @model_validator(mode="before")
    @classmethod
    def import_retention(cls, value):
        # Keep previously configured JSON retention visible in the new selector.
        if isinstance(value, dict) and "retention" not in value:
            value = copy.deepcopy(value)
            block = value.get("value")
            request = value.get("request") or {}
            request = request if isinstance(request, dict) else {}
            options = request.get("prompt_cache_options")
            ttl = block.get("ttl") if isinstance(block, dict) else None
            ttl = (
                ttl
                or request.get("prompt_cache_retention")
                or (options.get("ttl") if isinstance(options, dict) else None)
            )
            if ttl in ("5m", "1h", "24h", "30m", "in_memory"):
                value["retention"] = ttl
        return value

    @model_validator(mode="after")
    def safe_extensions(self):
        if self.field.strip() != self.field or self.field.lower() in RESERVED | {
            "type",
            "text",
            "role",
            "content",
        }:
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
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def retention_policy(settings):
    """Only advertise documented retention controls for the selected wire protocol."""
    api, cache, model = settings.api_mode, settings.cache, settings.model.lower()
    if not gemini_controls(api, model)[1]:
        return None, ()
    claude_marker = cache.preset != "custom" or (
        cache.field == "cache_control"
        and isinstance(cache.value, dict)
        and cache.value.get("type") == "ephemeral"
    )
    if claude_marker and (
        api == "anthropic" or api == "chat_completions" and ("claude" in model or cache.preset == "custom")
    ):
        return "anthropic", ("5m", "1h")
    if api == "gemini" or api == "chat_completions" and cache.preset != "custom" and "gemini" in model:
        return "gemini", ("5m", "15m", "1h", "24h")
    if api not in ("chat_completions", "responses"):
        return None, ()
    version = re.search(r"(?:^|/)gpt-(\d+)(?:[.-](\d+))?(?:-|$)", model)
    if version:
        major, minor = int(version[1]), int(version[2] or 0)
        if (major, minor) >= (5, 6):
            return "openai_ttl", ("30m",)
        if (major, minor) == (5, 5):
            return "openai_retention", ("24h",)
        if (major == 5 and minor in (0, 1, 2, 4)) or (major, minor) == (4, 1):
            return "openai_retention", ("in_memory", "24h")
    if "gpt" in model or re.match(r"^(o1|o3|o4)(-|$)", model):
        return "openai_retention", ("in_memory",)
    return None, ()


def effective_retention(settings):
    kind, options = retention_policy(settings)
    value = settings.cache.retention
    return kind, value if value in options else options[0] if options else None


def retention_request(settings):
    kind, value = effective_retention(settings)
    if (
        kind == "anthropic"
        and settings.api_mode == "anthropic"
        and settings.cache.mode == "cloud"
        and settings.cache.retention != "default"
    ):
        # Native Messages has an automatic cache control independent of local
        # breakpoints. Leave untouched cloud defaults alone until the user opts in.
        return {"cache_control": {"type": "ephemeral", "ttl": value}}
    if kind == "openai_ttl":
        return {"prompt_cache_options": {"ttl": value}}
    if kind == "openai_retention":
        return {"prompt_cache_retention": value}
    return {}


def _preset(settings):
    cache = settings.cache
    if not gemini_controls(settings.api_mode, settings.model)[1]:
        return {"kind": "implicit", "request": {}}
    if settings.api_mode == "gemini":
        # Native Gemini has no arbitrary content-block cache markers or cache
        # extension JSON. The UI explains that saved controls for other APIs do
        # not apply; retain those values for switching back without data loss.
        return {"kind": "gemini", "request": {}}
    if cache.preset == "custom":
        return {
            "kind": "block",
            "field": cache.field,
            "value": copy.deepcopy(cache.value),
            "request": copy.deepcopy(cache.request),
        }
    model = settings.model.lower()
    if settings.api_mode != "anthropic" and "gemini" in model:
        return {"kind": "gemini", "request": copy.deepcopy(cache.request)}
    if settings.api_mode == "anthropic" or (settings.api_mode == "chat_completions" and "claude" in model):
        return {
            "kind": "block",
            "field": "cache_control",
            "value": {"type": "ephemeral"},
            "request": copy.deepcopy(cache.request),
        }
    if "gpt" in model or model.startswith(("o1", "o3", "o4")):
        kind, _ = retention_policy(settings)
        if kind == "openai_retention" and (
            re.search(r"(?:^|/)gpt-\d", model) or re.match(r"^(o1|o3|o4)(-|$)", model)
        ):
            # Earlier GPT models accept retention but not the newer explicit
            # breakpoint protocol. Keep their cache selection server-managed.
            return {"kind": "implicit", "request": copy.deepcopy(cache.request)}
        return {
            "kind": "block",
            "field": "prompt_cache_breakpoint",
            "value": {"mode": "explicit"},
            "request": {"prompt_cache_options": {"mode": "explicit"}, **copy.deepcopy(cache.request)},
        }
    return {"kind": "unknown", "request": {}}


def preset(settings):
    result = _preset(settings)
    kind, value = effective_retention(settings)
    if (
        kind == "anthropic"
        and result.get("field") == "cache_control"
        and isinstance(result.get("value"), dict)
    ):
        if value == "1h":
            result["value"]["ttl"] = value
        else:
            result["value"].pop("ttl", None)  # Anthropic's default is the lowest, 5m tier.
    # The explicit selector takes precedence over legacy extension JSON. Merge
    # nested OpenAI options without losing mode or unrelated custom options.
    request = result["request"]
    for key, item in retention_request(settings).items():
        if key == "prompt_cache_options":
            request.pop("prompt_cache_retention", None)
            request[key] = {**request.get(key, {}), **item}
        else:
            request[key] = item
    return result


def endpoint(base_url, api, model="", *, stream=False, resource=None, api_path=None):
    """Accept a root or an API root; preserve deployment-specific path prefixes."""
    parsed = urlsplit(base_url)
    path = parsed.path.rstrip("/")
    if parsed.query or parsed.fragment or parsed.username or parsed.password:
        raise ValueError("接口地址不能包含查询参数、片段或登录凭据")
    if api_path is not None:
        path = "/" + (api_path or ("v1beta" if api == "gemini" else "v1")).strip("/")
        if resource is not None:
            path += "/" + resource.lstrip("/")
        elif api == "gemini":
            path += "/models/" + quote(model.removeprefix("models/"), safe="-._")
            path += ":streamGenerateContent" if stream else ":generateContent"
        else:
            path += {
                "chat_completions": "/chat/completions",
                "responses": "/responses",
                "anthropic": "/messages",
            }[api]
    elif api in ("chat_completions", "responses", "anthropic"):
        suffix = {
            "chat_completions": "/chat/completions",
            "responses": "/responses",
            "anthropic": "/messages",
        }[api]
        if path.endswith(suffix):
            path = path[: -len(suffix)]
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
    return urlunsplit(
        (parsed.scheme, parsed.netloc, path, "alt=sse" if stream and api == "gemini" else "", "")
    )


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
        write = next(
            (
                value
                for value in (
                    details.get("cache_write_tokens"),
                    details.get("cached_creation_tokens"),
                    details.get("cache_creation_tokens"),
                    raw.get("cache_creation_input_tokens"),
                )
                if value is not None
            ),
            None,
        )
        if write is None and raw.get("usage_source") == "anthropic":
            fields = ("claude_cache_creation_5_m_tokens", "claude_cache_creation_1_h_tokens")
            if all(k in raw for k in fields):
                write = sum(raw[k] for k in fields)
    return {
        "prompt_tokens": prompt,
        "completion_tokens": output,
        "total_tokens": prompt + output,
        "cached_tokens": read,
        "cache_write_tokens": write,
        "raw_usage": raw,
    }
