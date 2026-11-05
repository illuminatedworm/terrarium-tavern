"""Inspect the production adapter/cache preparation path without dispatching HTTP."""

from tavern.core.message_adapter import adapt_messages


async def request_payload(provider, messages, *, tools=None, **kwargs):
    messages, _, _ = adapt_messages(messages, provider.settings)
    body, _, _ = await provider._prepare(
        messages, tools or (kwargs.get("extra_body") or {}).get("tools"), False, kwargs
    )
    return body
