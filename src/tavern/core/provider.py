"""Shared HTTP transport independent of story mode."""

import os

import httpx

from tavern.scripting.model_transport import ModelTransport


def require_model_connection(settings):
    if settings.channel_origin:
        from tavern.core.channels import require_channel

        require_channel(settings, settings.channel_origin)
    if not settings.model:
        raise ValueError("请先设置模型名称")
    if settings.auth_mode == "chatgpt_subscription":
        if settings._chatgpt is None:
            raise ValueError("SIWC 账号服务不可用，请在提供商设置中连接账号")
        if settings.chatgpt_account_id in settings._auth_generations and not settings._auth_generation:
            raise ValueError("此任务开始时尚未连接 SIWC 账号，请连接后重新开始任务")
        settings._chatgpt.require_account(settings.chatgpt_account_id, settings._auth_generation)
        return
    if settings.auth_mode == "openai_api_key" and not settings.api_key:
        raise ValueError("请在官方 API 连接中填写 API Key")
    key = settings.api_key or os.environ.get("TAVERN_API_KEY", "")
    if not key:
        raise ValueError("请先设置 API Key；本地免密端点可填 local")


async def make_provider(settings, capture=None, *, request_timeout=120, request_context=None):
    require_model_connection(settings)
    if settings.auth_mode == "chatgpt_subscription":
        from tavern.chatgpt.provider import ChatGPTProvider

        account = await settings._chatgpt.access(
            settings.chatgpt_account_id, use_proxy=settings.use_proxy, generation=settings._auth_generation
        )
        settings = settings.model_copy(
            deep=True,
            update={
                "api_mode": "responses",
                "api_path": "v1",
                "base_url": "https://api.openai.com/v1",
                "api_key": "",
            },
        )
        settings._auth_generation = account["generation"]
        client = httpx.AsyncClient(
            proxy=(settings.proxy or None) if settings.use_proxy else None,
            timeout=request_timeout,
            trust_env=False,
            follow_redirects=False,
            transport=settings._chatgpt.http_transport,
        )
        return ChatGPTProvider(
            settings,
            account["tokens"]["access_token"],
            capture,
            http_client=client,
            request_context=request_context,
        )
    key = settings.api_key or os.environ.get("TAVERN_API_KEY", "")
    client = httpx.AsyncClient(
        proxy=(settings.proxy or None) if settings.use_proxy else None,
        timeout=request_timeout,
        trust_env=False,
    )
    if settings.auth_mode == "openai_api_key":
        from tavern.chatgpt.provider import ChatGPTProvider

        return ChatGPTProvider(settings, key, capture, http_client=client, request_context=request_context)
    return ModelTransport(settings, key, capture, http_client=client, request_context=request_context)
