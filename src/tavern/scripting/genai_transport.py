"""Official Google GenAI SDK with Runtime-owned HTTP policy and lifetimes.

The SDK owns serialization and streaming. Runtime still owns tools, retries,
context and cancellation; an operation-local HTTP observer records the actual
serialized request and closes streams even when the SDK iterator is cancelled.
"""
import json
from collections.abc import Callable
from contextlib import aclosing, asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from urllib.parse import quote, urlsplit

import httpx
from google import genai
from google.genai import errors, types
from pydantic import ValidationError

from tavern.core.cache_policy import endpoint
from tavern.scripting.sse import stream_events


@dataclass
class _Operation:
    capture: Callable | None = None
    responses: list[httpx.Response] = field(default_factory=list)
    raw_event: dict | None = None


class _ObservedEvents(httpx.AsyncByteStream):
    """Retain raw native fields that the SDK's typed response would discard.

    Feed one complete event at a time through the public HTTP transport so the
    SDK still validates/handles its stream. Runtime receives the same original
    payload, including opaque signatures and provider-specific usage fields.
    """
    def __init__(self, response, operation):
        self.response, self.operation = response, operation

    async def __aiter__(self):
        async for event in stream_events(self.response):
            self.operation.raw_event = event
            yield ("data: " + json.dumps(event, ensure_ascii=False) + "\n\n").encode()

    async def aclose(self):
        await self.response.aclose()


class _RuntimeHTTP(httpx.AsyncBaseTransport):
    def __init__(self, http: httpx.AsyncClient):
        self.http = http
        self.operation: ContextVar[_Operation | None] = ContextVar("genai_operation", default=None)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        operation = self.operation.get()
        # SDK's default timeout=None must not override the configured Runtime
        # timeout. Proxy and TLS policy also remain on the existing client.
        request.extensions["timeout"] = self.http.timeout.as_dict()
        if operation and len(operation.responses) >= 100:
            raise ValueError("模型列表超过 100 页，未更新本地列表")
        if operation and operation.capture:
            operation.capture(request)
        response = await self.http.send(request, stream=True)
        if operation is not None:
            operation.responses.append(response)
            if response.status_code == 200 and request.url.path.endswith(":streamGenerateContent"):
                headers = {k: v for k, v in response.headers.items() if k not in ("content-length", "content-encoding")}
                response = httpx.Response(200, headers=headers, request=request,
                    stream=_ObservedEvents(response, operation))
                operation.responses.append(response)
        return response


class GenAITransport:
    def __init__(self, base_url: str, api_path: str, key: str, http: httpx.AsyncClient):
        url = urlsplit(endpoint(base_url, "gemini", resource="models", api_path=api_path))
        self.transport = _RuntimeHTTP(http)
        self.http = httpx.AsyncClient(transport=self.transport, trust_env=False)
        self.client = genai.Client(api_key=key, vertexai=False, http_options=types.HttpOptions(
            base_url=f"{url.scheme}://{url.netloc}", api_version=url.path.removesuffix("/models").strip("/"),
            httpx_async_client=self.http, client_args={"trust_env": False},
            retry_options=types.HttpRetryOptions(attempts=1)))

    @asynccontextmanager
    async def operation(self, capture=None):
        operation = _Operation(capture=capture)
        token = self.transport.operation.set(operation)
        try:
            yield operation
        except ValidationError:
            # Pydantic errors include input fragments, potentially opaque native
            # state. Do not echo those fragments into runtime/tool diagnostics.
            raise ValueError("Gemini 消息或参数不符合官方 SDK 格式，请检查工具定义、消息内容和思考签名") from None
        except errors.APIError as error:
            # Keep the application's existing HTTP error/fallback contract.
            if isinstance(error.response, httpx.Response):
                error.response.raise_for_status()
            raise ValueError(f"Gemini 返回无效响应（{error.code}）") from None
        finally:
            try:
                for response in operation.responses:
                    await response.aclose()
            finally:
                self.transport.operation.reset(token)

    @staticmethod
    def arguments(model, body, *, stream=False):
        config = dict(body.get("generationConfig", {}))
        for wire, target in (("systemInstruction", "system_instruction"), ("tools", "tools"),
                            ("toolConfig", "tool_config"), ("cachedContent", "cached_content")):
            if wire in body:
                config[target] = body[wire]
        config.update(automatic_function_calling={"disable": True}, automatic_continuation=False,
                      should_return_http_response=not stream)
        # Keep deployment aliases a single path segment, matching endpoint()
        # and the address shown in settings (including slashes in relay IDs).
        return {"model": "models/" + quote(model.removeprefix("models/"), safe="-._"),
                "contents": body.get("contents", []),
                "config": types.GenerateContentConfig.model_validate(config)}

    async def generate(self, model, body, capture):
        async with self.operation(capture):
            response = await self.client.aio.models.generate_content(**self.arguments(model, body))
            raw = response.sdk_http_response
            return json.loads(raw.body), dict(raw.headers or {})

    async def stream(self, model, body, capture):
        async with self.operation(capture) as operation:
            chunks = await self.client.aio.models.generate_content_stream(**self.arguments(model, body, stream=True))
            async with aclosing(chunks):
                async for chunk in chunks:
                    headers = dict(chunk.sdk_http_response.headers or {}) if chunk.sdk_http_response else {}
                    if operation.raw_event is None:
                        raise ValueError("Gemini SDK 流未提供对应的原生响应")
                    yield operation.raw_event, headers

    async def create_cache(self, prefix, ttl):
        config = {"contents": prefix["contents"], "system_instruction": prefix["systemInstruction"], "ttl": ttl}
        if "tools" in prefix:
            config.update(tools=prefix["tools"], tool_config=prefix["toolConfig"])
        async with self.operation():
            result = await self.client.aio.caches.create(model=prefix["model"], config=config)
            return result.model_dump(mode="json", by_alias=True, exclude_none=True)

    async def delete_cache(self, name):
        async with self.operation():
            await self.client.aio.caches.delete(name=name)

    async def list_models(self):
        async with self.operation() as operation:
            result = []
            async for model in await self.client.aio.models.list():
                if model.name:
                    result.append(model.name)
                if len(result) > 10000:
                    raise ValueError("模型列表超过 100 页或 10000 项，未更新本地列表")
            for response in operation.responses:
                payload = response.json()
                if not isinstance(payload, dict) or not isinstance(payload.get("models"), list):
                    raise ValueError("模型接口未返回有效的模型列表")
            return result

    async def close(self):
        try:
            await self.client.aio.aclose()
        finally:
            self.client.close()
            await self.http.aclose()
