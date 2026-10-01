"""Shared HTTP transport independent of story mode."""
import os

import httpx

from tavern.scripting.model_transport import ModelTransport


async def make_provider(settings, capture=None, *, request_timeout=120, request_context=None):
    key = settings.api_key or os.environ.get("TAVERN_API_KEY", "")
    if not settings.model:
        raise ValueError("请先设置模型名称")
    if not key:
        raise ValueError("请先设置 API Key；本地免密端点可填 local")
    client = httpx.AsyncClient(proxy=(settings.proxy or None) if settings.use_proxy else None,
                              timeout=request_timeout, trust_env=False)
    return ModelTransport(settings, key, capture, http_client=client, request_context=request_context)
