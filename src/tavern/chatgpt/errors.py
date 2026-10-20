"""Safe actionable errors; raw OAuth responses and bearer credentials stay private."""

import re

from tavern.chatgpt import USAGE_URL

MESSAGES = {
    "invalid_client": "ChatGPT 客户端注册无效，请检查连接配置",
    "subscription_sharing_user_not_eligible": "此账号或工作区目前不能使用 ChatGPT 订阅接入",
    "subscription_sharing_usage_limit_exceeded": "已达到 ChatGPT 订阅或本应用的用量限制，请管理用量",
    "subscription_sharing_usage_unavailable": "暂时无法核实 ChatGPT 订阅用量，请稍后重试",
    "subscription_sharing_unsupported_capability": "ChatGPT 订阅通道不支持此次请求的模型或能力",
    "subscription_sharing_route_not_supported": "ChatGPT 订阅通道不支持此次请求的接口",
    "subscription_sharing_invalid_user": "无法验证 ChatGPT 账号，请检查连接或重新登录",
    "chatpass_v2_scope_not_authorized": "ChatGPT 订阅使用权限不足，请重新授权",
    "chatpass_v2_invalid_authorization_context": "ChatGPT 授权上下文无效，请重新登录",
    "subscription_sharing_user_unavailable": "暂时无法获取 ChatGPT 账号信息，请稍后重试",
}
TERMINAL_REFRESH = {
    "invalid_grant",
    "invalid_refresh_token",
    "token_expired",
    "refresh_token_expired",
    "refresh_token_invalidated",
    "refresh_token_invalid",
    "refresh_token_reused",
}


def redact(value, secrets=()):
    if isinstance(value, dict):
        return {
            key: "[REDACTED]"
            if key.lower()
            in {
                "authorization",
                "access_token",
                "refresh_token",
                "id_token",
                "id_token_hint",
                "code_verifier",
            }
            else redact(item, secrets)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item, secrets) for item in value]
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, "[REDACTED]")
        value = re.sub(r"(?i)Bearer\s+[^\s\"<>]+", "Bearer [REDACTED]", value)
        return re.sub(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", "[REDACTED]", value)
    return value


class ChatGPTError(ValueError):
    def __init__(self, message, *, code="chatgpt_error", status=None, request_id=None, diagnostic=None):
        self.code, self.status, self.request_id = code, status, request_id
        self.diagnostic = diagnostic or {}
        suffix = (
            f"（{code}"
            + (f"，HTTP {status}" if status else "")
            + (f"，request ID: {request_id}" if request_id else "")
            + ")"
        )
        super().__init__(message + suffix)

    def public(self):
        return {
            "detail": str(self),
            "code": self.code,
            "upstream_status": self.status,
            "request_id": self.request_id,
            "usage_url": USAGE_URL,
        }


def response_error(payload, status=None, request_id=None, *, secrets=()):
    source = payload.get("error", {}) if isinstance(payload, dict) else {}
    code = source.get("code") or source.get("type") if isinstance(source, dict) else source
    code = (
        code
        if isinstance(code, str) and re.fullmatch(r"[a-zA-Z0-9_.-]{1,120}", code)
        else "chatgpt_request_failed"
    )
    request_id = redact(request_id, secrets)
    message = MESSAGES.get(
        code,
        {
            401: "ChatGPT 授权未被接受，请检查账号权限或重新登录",
            403: "ChatGPT 拒绝此请求，请检查账号、工作区权限及服务地区",
            429: "ChatGPT 请求达到限制，请管理用量或稍后重试",
            503: "ChatGPT 服务暂不可用，请稍后重试",
        }.get(status, "ChatGPT 请求未完成"),
    )
    return ChatGPTError(
        message,
        code=code,
        status=status,
        request_id=request_id,
        diagnostic={"status": status, "request_id": request_id, "body": redact(payload, secrets)},
    )
