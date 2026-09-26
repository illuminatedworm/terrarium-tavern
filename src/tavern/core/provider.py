"""Shared HTTP provider transport, independent of either story mode."""
import json
import os

import httpx

from tavern.core.tokens import count_text, counting_info, request_tokens


async def make_provider(settings, capture=None, *, request_timeout=120):
    from kohakuterrarium.llm.openai import OpenAIProvider
    from openai import AsyncOpenAI

    from tavern.scripting.prompt_cache import mark_instruction_breakpoint
    from tavern.scripting.responses import ResponsesProvider

    key = settings.api_key or os.environ.get("TAVERN_API_KEY", "")
    if not settings.model:
        raise ValueError("请先设置模型名称")
    if not key:
        raise ValueError("请先设置 API Key；本地免密端点可填 local")
    class ChatProvider(OpenAIProvider):
        user_prompt_cache_key = None
        prompt_cache_mode = "default"

        def _prompt_cache_request_kwargs(self):
            # Terrarium assigns its session ID later; an explicit user key wins.
            cache_key = self.user_prompt_cache_key or self.prompt_cache_key
            return {"prompt_cache_key": cache_key} if cache_key else {}

        def _prepare_messages(self, messages):
            prepared = super()._prepare_messages(messages)
            return (mark_instruction_breakpoint(prepared, text_type="text")
                    if self.prompt_cache_mode == "instructions" else prepared)

        def with_model(self, name):
            clone = super().with_model(name)
            if clone is self:
                return self
            clone.__class__ = ChatProvider
            clone.user_prompt_cache_key = self.user_prompt_cache_key
            clone.prompt_cache_mode = self.prompt_cache_mode
            return clone

    sampling_fields = (("top_p",) if settings.api_mode == "responses" else
                       ("top_p", "top_k", "frequency_penalty", "presence_penalty"))
    sampling = {field: getattr(settings, field) for field in sampling_fields
        if getattr(settings, field) is not None}
    extra = dict(sampling)
    if settings.prompt_cache_mode != "default" and settings.api_mode == "chat_completions":
        extra["prompt_cache_options"] = {"mode": "explicit"}
    if settings.prompt_cache_retention != "default" and settings.api_mode == "chat_completions":
        extra["prompt_cache_retention"] = settings.prompt_cache_retention
    if settings.reasoning_effort != "default" and settings.api_mode == "chat_completions":
        extra.update(
            {"reasoning_effort": settings.reasoning_effort}
            if settings.reasoning_format == "reasoning_effort"
            else {"reasoning": {"effort": settings.reasoning_effort}}
        )
    provider_type = ResponsesProvider if settings.api_mode == "responses" else ChatProvider
    provider = provider_type(
        api_key=key,
        model=settings.model,
        base_url=settings.base_url,
        temperature=settings.temperature,
        max_tokens=settings.reply_tokens,
        max_retries=0,
        extra_body=extra if settings.api_mode == "chat_completions" else {},
    )
    if settings.api_mode == "responses":
        provider.sampling = sampling
        provider.reasoning_effort = settings.reasoning_effort
        provider.prompt_cache_mode = settings.prompt_cache_mode
        provider.prompt_cache_retention = settings.prompt_cache_retention
    else:
        provider.prompt_cache_mode = settings.prompt_cache_mode
    provider.user_prompt_cache_key = (settings.prompt_cache_key or "").strip() or None
    await provider._client.close()

    async def request_hook(request):
        if request.content:
            try:
                payload = json.loads(request.content)
            except (ValueError, UnicodeError):
                return
            wire = {"messages": payload.get("messages", payload.get("input", [])),
                    "tools": payload.get("tools", [])}
            input_tokens = request_tokens(wire["messages"], settings, wire["tools"])
            if settings.api_mode == "responses":
                input_tokens += sum(count_text(json.dumps(item, ensure_ascii=False), settings)
                                    for item in wire["messages"] if "role" not in item)
            if (
                input_tokens + settings.reply_tokens + 2048
                > settings.context_tokens
            ):
                raise ValueError(f"最终模型请求（含工具与结果）估算 {input_tokens} tokens，超过 token 预算，未发送")
            if capture:
                capture(
                    {
                        "model": payload.get("model"),
                        "messages": wire["messages"],
                        "tools": [x.get("function", {}).get("name", x.get("name"))
                                  for x in payload.get("tools", [])],
                        "reasoning": payload.get("reasoning", payload.get("reasoning_effort", "default")),
                        "estimated_input_tokens": input_tokens,
                        "token_counting": counting_info(settings),
                    }
                )

    client = httpx.AsyncClient(
        proxy=(settings.proxy or None) if settings.use_proxy else None,
        timeout=request_timeout,
        trust_env=False,
        event_hooks={"request": [request_hook]},
    )
    provider._client = AsyncOpenAI(api_key=key, base_url=settings.base_url, max_retries=0, http_client=client)
    return provider


