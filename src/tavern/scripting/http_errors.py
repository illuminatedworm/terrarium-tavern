"""Preserve the provider's explanation when HTTP errors cross Agent boundaries."""

import re

import httpx

DETAIL_LIMIT = 4000


def redact_detail(detail: str, secret: str) -> str:
    if secret:
        detail = detail.replace(secret, "[REDACTED]")
    return re.sub(r"(?i)Bearer\s+[^\s\"<>]+", "Bearer [REDACTED]", detail).strip()


def response_error(response: httpx.Response, secret: str) -> httpx.HTTPStatusError:
    try:
        payload = response.json()
    except ValueError:
        payload = None
    source = payload.get("error", payload) if isinstance(payload, dict) else payload
    if isinstance(source, dict):
        codes = [
            str(source[key])
            for key in ("code", "status", "type")
            if isinstance(source.get(key), (str, int)) and source[key] not in ("", "error")
        ]
        message = source.get("message") or source.get("detail") or ""
        detail = (" / ".join(codes) + "：" if codes else "") + (message if isinstance(message, str) else "")
    elif isinstance(source, str):
        detail = source
    else:
        detail = response.text
    detail = redact_detail(detail, secret)
    if len(detail) > DETAIL_LIMIT:
        detail = detail[:DETAIL_LIMIT] + "…（仅显示错误片段）"
    label = f"模型请求失败（HTTP {response.status_code} {response.reason_phrase}）"
    return httpx.HTTPStatusError(
        label + ("：" + detail if detail else ""), request=response.request, response=response
    )
