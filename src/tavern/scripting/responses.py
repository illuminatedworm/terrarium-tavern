"""Compatibility import for integrations; production uses the shared four-protocol transport."""

from tavern.core.models import Settings
from tavern.scripting.model_transport import ModelTransport
from tavern.scripting.protocols import response_input as _input_items
from tavern.scripting.protocols import response_tools as _tools

__all__ = ["ResponsesProvider", "_input_items", "_tools"]


class ResponsesProvider(ModelTransport):
    def __init__(self, api_key, model, base_url="https://api.openai.com/v1", **kwargs):
        settings = Settings(
            api_key=api_key,
            model=model,
            base_url=base_url,
            api_mode="responses",
            temperature=kwargs.get("temperature", 0.7),
            reply_tokens=kwargs.get("max_tokens") or 2048,
        )
        super().__init__(settings, api_key)

    def _request(self, messages, *, tools=None, **kwargs):
        marker = (
            {"field": "prompt_cache_breakpoint", "value": {"mode": "explicit"}}
            if getattr(self, "session_breakpoint", False)
            else None
        )
        params = {"prompt_cache_options": {"mode": "explicit"}} if marker else {}
        return self._body(messages, tools, False, marker, params, kwargs)
