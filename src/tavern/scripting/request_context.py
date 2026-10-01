"""Local conversation identity, independent of prompt-cache markers or transport."""
import json
import os
from dataclasses import dataclass, field
from uuid import UUID, uuid5

from tavern.core.cache_policy import endpoint, fingerprint


def response_origin(settings, key=None):
    """Bind opaque provider state to its actual credential, endpoint and model."""
    key = key if key is not None else settings.api_key or os.environ.get("TAVERN_API_KEY", "")
    return {"api": settings.api_mode, "model": settings.model,
            "endpoint": endpoint(settings.base_url, settings.api_mode, settings.model),
            "credential": fingerprint(key)}


@dataclass
class ProviderRequestContext:
    session_id: str
    turn_id: str
    window_id: str
    device_id: str
    routing: dict = field(default_factory=dict)
    diagnostics: dict = field(default_factory=dict)

    def __post_init__(self):
        self.session_id = str(UUID(self.session_id))

    def bind(self, origin):
        owner = fingerprint([origin, self.session_id, self.turn_id])
        if self.routing.get("owner") != owner:
            self.routing = {"owner": owner}

    def headers(self, api):
        if api == "anthropic":
            return {"X-Claude-Code-Session-Id": self.session_id}
        if api == "responses":
            result = {"session-id": self.session_id, "thread-id": self.session_id}
            if self.routing.get("turn_state"):
                result["x-codex-turn-state"] = self.routing["turn_state"]
            return result
        return {}

    def apply(self, body, settings):
        if settings.api_mode == "anthropic":
            metadata = body.setdefault("metadata", {})
            if isinstance(metadata, dict):
                custom = settings.cache.request.get("metadata", {})
                user_id = custom.get("user_id") if isinstance(custom, dict) else None
                metadata.setdefault("user_id", user_id if user_id is not None else json.dumps({
                    "device_id": self.device_id, "account_uuid": "", "session_id": self.session_id,
                }, separators=(",", ":")))
        elif settings.api_mode == "responses":
            body.setdefault("prompt_cache_key", settings.cache.request.get(
                "prompt_cache_key", settings.prompt_cache_key or self.session_id))

    def receive(self, api, headers):
        # Codex pins the first routing state for the logical turn. Later responses
        # must not silently replace it, including after a local context rollover.
        if api == "responses" and headers.get("x-codex-turn-state"):
            self.routing.setdefault("turn_state", headers["x-codex-turn-state"])

    def identity_rejection(self, api, payload):
        if api not in ("anthropic", "responses"):
            return False
        error = payload.get("error", {}) if isinstance(payload, dict) else {}
        text = (str(error.get("message", "")) + " " + str(error.get("param", ""))
                if isinstance(error, dict) else str(error)).lower()
        fields = ("metadata", "user_id", "x-claude-code-session-id") if api == "anthropic" else (
            "prompt_cache_key", "session-id", "thread-id", "x-codex-turn-state")
        return any(field in text for field in fields)

    def saved(self):
        return {"turn_id": self.turn_id, "routing": dict(self.routing)}

    def audit(self):
        state = self.routing.get("turn_state")
        return {"session_id": self.session_id, "turn_id": self.turn_id, "window_id": self.window_id,
                "turn_state_present": bool(state), "turn_state_sha256": fingerprint(state) if state else None,
                **self.diagnostics}


def identity_headers(headers):
    """Allowlist only; never copy authentication or opaque routing tokens to logs."""
    allowed = {"x-claude-code-session-id", "session-id", "thread-id"}
    return {key.lower(): value for key, value in headers.items() if key.lower() in allowed}


class OperationContexts:
    """Stable story/purpose identities, with routing scoped to one actual operation."""

    def __init__(self, store, session_id, turn_id):
        self.store, self.session_id, self.turn_id = store, session_id, turn_id
        self.counts = {}

    def new(self, purpose="narrative", window_id=""):
        number = self.counts.get(purpose, 0)
        self.counts[purpose] = number + 1
        sid = UUID(self.session_id)
        if purpose != "narrative":
            sid = uuid5(sid, "half_agent:" + purpose)
        return ProviderRequestContext(str(sid), f"{self.turn_id}:{purpose}:{number}",
                                      window_id, self.store.device_id())


def configure_operation(provider, context, event, purpose):
    """Also used for injected providers, so test and production lifecycles agree."""
    provider.request_context = context
    if hasattr(provider, "settings") and hasattr(provider, "key"):
        context.bind(response_origin(provider.settings, provider.key))
    provider.response_capture = lambda record: event("provider_response", {"purpose": purpose, **record})
