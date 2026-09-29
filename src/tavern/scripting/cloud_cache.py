"""Short-lived Gemini objects and negative capability results, scoped to credentials."""
import asyncio
import copy
import re
import time
from datetime import datetime

import httpx

from tavern.core.cache_policy import endpoint, fingerprint
from tavern.core.tokens import count_text
from tavern.scripting.protocols import native_messages, native_tools

DISABLED = {}
OBJECTS = {}
LOCKS = {}


def capability_key(settings, key):
    return fingerprint([settings.base_url, fingerprint(key), settings.model, settings.api_mode,
                        settings.cache.model_dump()])


def reset_capability(settings, key):
    DISABLED.pop(capability_key(settings, key), None)


def cache_rejection(status, payload, field, *, resource=False):
    if status not in (400, 404, 422):
        return False
    error = payload.get("error", {}) if isinstance(payload, dict) else {}
    error = error if isinstance(error, dict) else {"message": str(error)}
    message = str(error.get("message", "")).lower()
    param = str(error.get("param", "")).lower()
    fields = [field] if isinstance(field, str) else list(field)
    named = any(x.lower() in message or x.lower() in param for x in
                fields + ["prompt_cache", "cache_control", "cachedcontent", "cached_content", "cached content"] if x)
    return named and (resource and status == 404 or any(x in message for x in
                    ("not supported", "unsupported", "unknown", "unrecognized", "invalid", "expired", "not found",
                     "不支持", "不存在", "过期")))


async def gemini_object(provider, messages, tools, tool_choice="auto"):
    settings = provider.settings
    system, contents, boundary = native_messages(messages, "gemini")
    if boundary is None:
        return None, "没有可用的固定前缀断点"
    at = next(i for i, message in enumerate(messages) if message.get("_cache_breakpoint"))
    prefix_system, prefix_contents, _ = native_messages(messages[:at + 1], "gemini")
    if prefix_system != system:
        # Gemini cannot combine cached and uncached systemInstruction blocks.
        # Never silently expand a user-selected breakpoint to cache later content.
        return None, "Gemini 无法拆分固定系统前缀；请将断点固定到更后的正文消息，当前完整发送"
    prefix = {"model": "models/" + settings.model.removeprefix("models/"),
              "systemInstruction": {"parts": prefix_system}, "contents": prefix_contents}
    if tools:
        prefix["tools"] = native_tools(tools, "gemini")
        selection = {"mode": "AUTO"}
        if isinstance(tool_choice, dict):
            selection = {"mode": "ANY", "allowedFunctionNames": [tool_choice.get("function", tool_choice)["name"]]}
        elif tool_choice == "none":
            selection = {"mode": "NONE"}
        prefix["toolConfig"] = {"functionCallingConfig": selection}
    import json
    # A conservative reference estimate avoids creating known undersized objects.
    minimum = 2048 if "2.5-flash" in settings.model.lower() else 4096
    if count_text(json.dumps(prefix, ensure_ascii=False), settings) < minimum:
        return None, f"固定前缀估算不足 {minimum} tokens，使用云端默认"
    cap = provider.capability_key
    scope = next((m.get("_cache_scope") for m in messages if m.get("_cache_scope")), settings.cache_scope)
    window = next((m.get("_cache_window") for m in messages if m.get("_cache_window")), settings.cache_window)
    slot = fingerprint([cap, scope or "standalone", settings.model])
    digest = fingerprint([prefix, window])
    lock_key = (id(asyncio.get_running_loop()), slot)
    lock = LOCKS.setdefault(lock_key, asyncio.Lock())
    async with lock:
        old = OBJECTS.get(slot)
        if old and old["digest"] == digest and old["expires"] > time.time() + 10:
            return {**old, "boundary": boundary, "slot": slot}, None
        url = endpoint(settings.base_url, "gemini", resource="cachedContents")
        try:
            response = await provider.http.post(url, headers=provider.gemini_headers,
                                                json={**prefix, "ttl": "300s"})
            if response.status_code >= 400:
                if response.status_code in (404, 405):
                    DISABLED[cap] = f"服务商不支持创建 Gemini 缓存对象（HTTP {response.status_code}）"
                return None, f"创建缓存对象失败（HTTP {response.status_code}），完整发送上下文"
            payload = response.json()
            name = payload.get("name", "")
            # Never send the credential to a URL returned by the service.
            if not re.fullmatch(r"cachedContents/[A-Za-z0-9_-]+", name):
                return None, "服务商未返回有效缓存资源名称"
            expires = time.time() + 300
            if payload.get("expireTime"):
                expires = min(expires, datetime.fromisoformat(payload["expireTime"].replace("Z", "+00:00")).timestamp())
            entry = {"name": name, "digest": digest, "expires": expires, "boundary": boundary, "slot": slot}
            OBJECTS[slot] = entry
            if old and old["name"] != name:
                try:
                    await provider.http.delete(endpoint(settings.base_url, "gemini", resource=old["name"]),
                                               headers=provider.gemini_headers)
                except httpx.HTTPError:
                    pass  # The old object has a bounded TTL.
            return copy.deepcopy(entry), None
        except (httpx.HTTPError, ValueError, TypeError):
            return None, "创建缓存对象不可用，完整发送上下文"


async def invalidate_object(provider, entry):
    if entry:
        OBJECTS.pop(entry["slot"], None)
        try:
            await provider.http.delete(endpoint(provider.settings.base_url, "gemini", resource=entry["name"]),
                                       headers=provider.gemini_headers)
        except httpx.HTTPError:
            pass
