"""Observe bytes before parsers; only failed HTTP attempts are persisted."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import httpx

from tavern.storage.request_debug import BODY_LIMIT, RequestDebugLog

if TYPE_CHECKING:
    from tavern.scripting.model_transport import ModelTransport

REDACTED = "[REDACTED]"
SECRET_HEADERS = {"authorization", "proxy-authorization", "x-api-key", "x-goog-api-key", "api-key",
                  "cookie", "set-cookie"}
SECRET_QUERY = {"key", "api_key", "api-key", "apikey", "access_token", "token"}


def headers(value: httpx.Headers, secret: str) -> dict[str, str]:
    return {key: REDACTED if key.lower() in SECRET_HEADERS else item.replace(secret, REDACTED) if secret else item
            for key, item in value.items()}


def address(value: httpx.URL, secret: str) -> str:
    url = value.copy_with(username="", password="")
    for key in url.params:
        if key.lower() in SECRET_QUERY:
            url = url.copy_set_param(key, REDACTED)
    return str(url).replace(secret, REDACTED) if secret else str(url)


@dataclass
class Body:
    limit: int
    data: bytearray = field(default_factory=bytearray)
    received: int = 0
    complete: bool = False

    def feed(self, data: bytes) -> None:
        self.received += len(data)
        self.data.extend(data[:max(0, self.limit - len(self.data))])

    def metadata(self):
        return {"received_bytes": self.received, "captured_bytes": len(self.data),
                "limit_bytes": self.limit, "truncated": self.received > len(self.data), "complete": self.complete}


class ObservedBody(httpx.AsyncByteStream):
    def __init__(self, stream, body):
        self.stream, self.body = stream, body

    async def __aiter__(self):
        async for chunk in self.stream:
            self.body.feed(chunk)
            yield chunk
        self.body.complete = True

    async def aclose(self):
        await self.stream.aclose()


@dataclass
class Attempt:
    request: httpx.Request
    limit: int
    secret: str
    response: httpx.Response | None = None
    response_representation: str = "transport"
    request_body: Body = field(init=False)
    response_body: Body = field(init=False)

    def __post_init__(self):
        self.request_body, self.response_body = Body(self.limit), Body(self.limit)
        self.request_body.feed(self.request.content)
        self.request_body.complete = True

    def save(self, log: RequestDebugLog, provider: "ModelTransport", error_type: str) -> None:
        def redact(data):
            # Only actual transport credentials are removed; model text and
            # opaque protocol fields remain usable for offline reproduction.
            if self.secret:
                data = data.replace(self.secret.encode(), REDACTED.encode())
            return data

        request = {"method": self.request.method, "url": address(self.request.url, self.secret),
                   "headers": headers(self.request.headers, self.secret), "body": self.request_body.metadata()}
        response = ({"status_code": self.response.status_code, "headers": headers(self.response.headers, self.secret),
                     "body": self.response_body.metadata()} if self.response is not None else None)
        request_data = redact(bytes(self.request_body.data))
        response_data = redact(bytes(self.response_body.data)) if response is not None else None
        request["body"]["saved_bytes"] = len(request_data)
        if response is not None:
            response["body"]["saved_bytes"] = len(response_data)
            response["body"]["representation"] = self.response_representation
        context = provider.request_context
        metadata = {"api_mode": provider.settings.api_mode, "model": provider.config.model,
                    "purpose": getattr(provider, "operation_purpose", provider.settings._adapter_lane),
                    "session_id": context.session_id if context else None,
                    "turn_id": context.turn_id if context else None,
                    "error_type": error_type, "credentials_redacted": True,
                    "request": request, "response": response}
        log.save(metadata, request_data, response_data)


@dataclass
class Capture:
    provider: "ModelTransport"
    log: RequestDebugLog
    attempts: list[Attempt] = field(default_factory=list)
    error_type: str | None = None


class FailureDebug:
    def __init__(self):
        # with_model() shares network clients; operation-local capture prevents
        # concurrent sibling calls from mixing raw data or failure ownership.
        self.current: ContextVar[Capture | None] = ContextVar("failure_debug", default=None)

    @contextmanager
    def operation(self, provider: "ModelTransport"):
        if not provider.settings.debug_failed_requests:
            yield None
            return
        capture = Capture(provider, RequestDebugLog(provider.settings._data_root, provider.settings))
        token = self.current.set(capture)
        try:
            yield capture
        except BaseException as error:
            # Record cancellations/closed generators too, then preserve their
            # original control flow. No retry or draining follows a failure.
            capture.error_type = type(error).__name__
            raise
        finally:
            try:
                for index, attempt in enumerate(capture.attempts):
                    rejected = attempt.response is not None and attempt.response.status_code >= 400
                    if rejected or (capture.error_type and index == len(capture.attempts) - 1):
                        attempt.save(capture.log, provider, capture.error_type or "HTTPStatusError")
            finally:
                self.current.reset(token)

    async def send(self, http: httpx.AsyncClient, request: httpx.Request, *, stream=False, **kwargs) -> httpx.Response:
        capture = self.current.get()
        if capture is None:
            return await http.send(request, stream=stream, **kwargs)
        attempt = Attempt(request, min(BODY_LIMIT, capture.log.max_bytes // 4), capture.provider.key)
        capture.attempts.append(attempt)
        response = await http.send(request, stream=True, **kwargs)
        attempt.response = response
        if response.is_stream_consumed:
            attempt.response_representation = "decoded"
            attempt.response_body.feed(response.content)
            attempt.response_body.complete = True
        else:
            response.stream = ObservedBody(response.stream, attempt.response_body)
        if not stream:
            try:
                await response.aread()
            except BaseException:
                await response.aclose()
                raise
        return response
