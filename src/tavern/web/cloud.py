"""Per-operation hosted-search selection and bounded, request-only fallback."""

import copy
from contextlib import aclosing

from kohakuterrarium.llm.base import ToolSchema

from tavern.web.sources import source_links as public_links

HOSTED_TOOL = {"type": "web_search", "external_web_access": True}


class WebSearchUnavailable(ValueError):
    pass


def search_failure(error):
    """Do not disguise an account/model failure as a recoverable search failure."""
    code = getattr(error, "code", "")
    if (
        code.startswith("chatpass_")
        or code
        in {
            "subscription_sharing_usage_limit_exceeded",
            "subscription_sharing_usage_unavailable",
            "subscription_sharing_user_not_eligible",
            "subscription_sharing_invalid_user",
            "subscription_sharing_user_unavailable",
            "sign_in_required",
            "account_missing",
        }
        or getattr(error, "status", None) == 401
    ):
        return False
    if code in {
        "web_search_unavailable",
        "web_search_failed",
        "web_search_rate_limit_exceeded",
        "web_search_not_supported",
    }:
        return True
    body = getattr(error, "diagnostic", {}).get("body", {})
    source = body.get("error", body.get("response", {}).get("error", {})) if isinstance(body, dict) else {}
    if not isinstance(source, dict):
        return False
    text = (code + " " + str(source.get("param", "")) + " " + str(source.get("message", ""))).lower()
    return ("web_search" in text or "web search" in text) and any(
        word in text
        for word in (
            "not supported",
            "unsupported",
            "not allowed",
            "disabled",
            "unavailable",
            "failed",
            "rate limit",
            "quota",
            "not enabled",
            "invalid tool",
        )
    )


def source_links(payload):
    """Portable links alongside lossless native output; no provider item IDs reach prose."""
    rows = []
    for item in payload.get("output", []):
        if item.get("type") == "web_search_call":
            sources = (item.get("action") or {}).get("sources")
            if isinstance(sources, list):
                rows.extend(sources)
        else:
            rows.extend(
                a
                for part in item.get("content", [])
                for a in (part.get("annotations") or [])
                if isinstance(a, dict) and a.get("type") == "url_citation"
            )
    return public_links(rows)


class WebSession:
    def __init__(self, mode, cfg, role, event=None, *, failed=False):
        self.mode, self.role = mode, role
        self.failed = failed
        self.cloud = mode == "auto" and cfg.auth_mode == "chatgpt_subscription" and not failed
        self.event = event or (lambda kind, payload: None)

    def definitions(self, base):
        excluded = (
            {"web_search", "read_webpage"} if self.mode == "off" else {"web_search"} if self.cloud else set()
        )
        result = [d for d in base if d.get("function", {}).get("name") not in excluded]
        return [*result, copy.deepcopy(HOSTED_TOOL)] if self.cloud else result

    async def chat(self, provider, messages, base, *, extra_body=None, on_fallback=None):
        options = extra_body or {}
        for _ in range(2):
            cloud = self.cloud and options.get("tool_choice", "auto") in ("auto", "required")
            definitions = self.definitions(base)
            schemas = [ToolSchema(**d["function"]) for d in definitions if d.get("type") == "function"]
            kwargs = {"tools": schemas, "extra_body": options}
            if cloud:
                kwargs.update(_web_search=True, _web_role=self.role, _web_event=self.event)
            try:
                async with aclosing(provider.chat(messages, **kwargs)) as chunks:
                    async for chunk in chunks:
                        yield chunk
                return
            except WebSearchUnavailable:
                if not cloud:
                    raise
                self.cloud, self.failed = False, True
                self.event(
                    "web_activity",
                    {
                        "service": "openai",
                        "auth_mode": "oauth",
                        "operation": "search",
                        "status": "fallback",
                        "reason": "云端搜索不可用，本请求改用共享本地搜索服务",
                    },
                )
                self.event(
                    "response_restarted",
                    {"reason": "云端搜索失败，已丢弃此请求的未完成输出，使用本地搜索重试"},
                )
                yield None  # Reset only this request's uncommitted text.
                if on_fallback:
                    on_fallback()
