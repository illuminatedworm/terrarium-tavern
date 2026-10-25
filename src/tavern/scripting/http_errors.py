"""Preserve the provider's explanation when HTTP errors cross Agent boundaries."""

import json
import re

import httpx

DETAIL_LIMIT = 4000


def redact_detail(detail: str, secret: str) -> str:
    if secret:
        detail = detail.replace(secret, "[REDACTED]")
    detail = re.sub(r"(?i)Bearer\s+[^\s\"<>]+", "Bearer [REDACTED]", detail)
    return re.sub(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", "[REDACTED]", detail).strip()


def upstream_detail(payload: object, *, secret: str = "") -> str:
    """Project error fields, never a whole response containing model/user data."""
    if isinstance(payload, str):
        detail = payload.strip()
        if detail.startswith(("{", "[")):
            try:
                payload = json.loads(detail)
            except ValueError:
                return ""  # A broken JSON body is not a safe error-field projection.
            return upstream_detail(payload, secret=secret)
    elif isinstance(payload, dict):
        response = payload.get("response")
        if isinstance(response, dict) and not payload.get("error"):
            payload = response
        error = payload.get("error")
        source = error if isinstance(error, dict) else payload
        codes = [
            str(source[key])
            for key in ("code", "status", "type", "reason")
            if isinstance(source.get(key), (str, int))
            and source[key]
            not in ("", "error", "failed", "incomplete", "response.failed", "response.incomplete")
        ]
        messages = [
            source[key].strip()
            for key in ("message", "detail", "error_description")
            if isinstance(source.get(key), str) and source[key].strip()
        ]
        if isinstance(error, str) and error.strip():
            messages.insert(0, error.strip())
        incomplete = payload.get("incomplete_details")
        if isinstance(incomplete, dict) and isinstance(incomplete.get("reason"), str):
            codes.append(incomplete["reason"])
        # Native completion metadata is useful even without an error object.
        candidates = payload.get("candidates")
        candidate = candidates[0] if isinstance(candidates, list) and candidates else {}
        feedback = payload.get("promptFeedback") or {}
        choices = payload.get("choices")
        choice = choices[0] if isinstance(choices, list) and choices else {}
        for item, code, message in (
            (payload, "stop_reason", "stop_message"),
            (candidate, "finishReason", "finishMessage"),
            (feedback, "blockReason", "blockReasonMessage"),
            (choice, "finish_reason", ""),
        ):
            if isinstance(item, dict):
                if isinstance(item.get(code), str):
                    codes.append(item[code])
                if isinstance(item.get(message), str):
                    messages.append(item[message])
        detail = " / ".join(dict.fromkeys(codes))
        message = "\n".join(dict.fromkeys(messages))
        detail += ("：" if detail and message else "") + message
    else:
        return ""
    if re.search(r"<\s*(?:!doctype\s+html|html|head|body)\b", detail, re.I):
        return ""
    detail = redact_detail(detail, secret)
    if len(detail) > DETAIL_LIMIT:
        detail = detail[:DETAIL_LIMIT] + "…（仅显示错误片段）"
    return detail


def upstream_message(fallback: str, payload: object, *, secret: str = "") -> str:
    detail = upstream_detail(payload, secret=secret)
    return f"{detail}；{fallback}" if detail else fallback


def response_error(response: httpx.Response, secret: str) -> httpx.HTTPStatusError:
    try:
        payload = response.json()
    except ValueError:
        payload = response.text
    label = f"模型请求失败（HTTP {response.status_code} {response.reason_phrase}）"
    return httpx.HTTPStatusError(
        upstream_message(label, payload, secret=secret), request=response.request, response=response
    )
