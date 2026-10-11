"""ChatGPT plan requests over public Responses, preserving Terrarium's provider contract."""
import copy
import json

import httpx

from tavern.chatgpt import API_ROOT
from tavern.chatgpt.errors import ChatGPTError, redact, response_error
from tavern.core.message_adapter import adapt_messages, adapter_snapshot
from tavern.scripting.model_stream import StreamState
from tavern.scripting.model_transport import ModelTransport
from tavern.scripting.native_response import prepare_messages
from tavern.scripting.protocols import definitions, response_input, response_tools
from tavern.scripting.request_context import response_origin


async def stream_events(response):
    """SSE allows multiple data lines and an event ending at EOF."""
    data = []
    async for line in response.aiter_lines():
        if line.startswith("data:"):
            data.append(line[5:].lstrip(" "))
        elif not line and data:
            value, data = "\n".join(data), []
            if value != "[DONE]":
                yield json.loads(value)
    if data and "\n".join(data) != "[DONE]":
        yield json.loads("\n".join(data))


class ChatGPTProvider(ModelTransport):
    def __init__(self, settings, key, capture=None, *, http_client=None, request_context=None):
        # The identity is pinned for the operation; an hourly token refresh is not a new session.
        settings = settings.model_copy(deep=True, update={"auth_mode": "chatgpt_subscription", "api_mode": "responses",
                                                        "base_url": API_ROOT, "api_path": "v1", "api_key": ""})
        self.accounts = settings._chatgpt
        self.account_id = settings.chatgpt_account_id
        self.generation = settings._auth_generation
        self._owner = self.accounts.attach(self.account_id, self.generation)
        self._closed = False
        self._allowed_tools = set()
        self._completed_response = None
        super().__init__(settings, key, capture, http_client=http_client, request_context=request_context)

    async def close(self):
        if not self._closed:
            self._closed = True
            self.accounts.detach(self.account_id, self._owner)
            await super().close()

    def _body(self, messages, tools, stream, marker, params, kwargs):
        messages, _ = prepare_messages(messages, response_origin(self.settings))
        items = response_input(messages)
        for item in items:
            if item.get("role") == "system":
                item["role"] = "developer"
            if item.get("type") in ("function_call", "function_call_output"):
                if item.get("namespace", "tavern") != "tavern":
                    raise ChatGPTError("工具历史的命名空间与 Tavern 不匹配", code="invalid_tool_namespace")
                item["namespace"] = "tavern"
        cfg = self.settings
        body = {"model": cfg.model, "input": items, "store": False, "stream": True,
                "include": ["reasoning.encrypted_content"]}
        if kwargs.get("_maintenance_instructions"):
            body["instructions"] = kwargs["_maintenance_instructions"]
        effort = cfg.reasoning_effort
        capabilities = self.accounts.model_capabilities(self.account_id, self.generation, cfg.model)
        if capabilities and "supported_reasoning_levels" in capabilities:
            supported = {level["effort"] for level in capabilities["supported_reasoning_levels"]}
            if effort != "default" and effort not in supported:
                raise ChatGPTError("当前账号的此模型不支持所选思考档位，请重新选择", code="unsupported_reasoning_effort")
            if effort == "default":
                effort = capabilities.get("default_reasoning_level", "default")
        if effort != "default":
            body["reasoning"] = {"effort": effort}
        threshold = kwargs.get("_compact_threshold")
        # The runtime checks capacity once before a user-initiated turn. Only
        # dedicated maintenance requests may compress; story/tool continuations
        # must not trigger a second, server-side threshold policy mid-turn.
        if threshold is not None and not kwargs.get("_compact_disabled"):
            if cfg.runtime_mode not in ("full_agent", "half_agent"):
                raise ValueError("云端 compact 仅适用于官方渠道的剧情上下文")
            if type(threshold) is not int or threshold < 1024:
                raise ValueError("云端压缩阈值必须为至少 1024 tokens 的整数")
            body["context_management"] = [{"type": "compaction", "compact_threshold": threshold}]
        # No custom cache fields, routing headers or ordinary Responses extras can reach this route.
        if self.request_context:
            body["prompt_cache_key"] = self.request_context.session_id
        extra = kwargs.get("extra_body") or {}
        choice = extra.get("tool_choice", "auto")
        selected = definitions(tools)
        if isinstance(choice, dict):
            forced = choice.get("function", choice).get("name")
            selected = [tool for tool in selected if tool.get("function", tool).get("name") == forced]
            if not selected:
                raise ValueError("指定工具不在当前允许的工具列表中")
            choice = "required"
        if choice not in ("auto", "none", "required"):
            raise ValueError("此 ChatGPT 工具选择方式不受支持")
        self._allowed_tools = {tool.get("function", tool)["name"] for tool in selected} if choice != "none" else set()
        if selected:
            body.update(tools=[{"type": "namespace", "name": "tavern", "description": "Terrarium Tavern tools",
                                "tools": response_tools(selected)}], tool_choice=choice,
                        parallel_tool_calls=bool(extra.get("parallel_tool_calls", False)))
        elif choice == "required":
            raise ValueError("当前请求没有可调用的工具")
        return body

    async def _send(self, messages, tools, stream, kwargs):
        account = await self.accounts.access(self.account_id, use_proxy=self.settings.use_proxy,
                                             generation=self.generation)
        self.key = account["tokens"]["access_token"]
        snapshot = adapter_snapshot(self.settings)
        messages, _, warnings = adapt_messages(messages, self.settings, snapshot)
        body = self._body(messages, tools, True, None, {}, kwargs)
        diagnostic = {"mode": "cloud", "status": "cloud", "kind": "implicit", "parameters": {},
                      "reason": "ChatGPT 订阅由服务端管理缓存；未发送显式缓存控制",
                      "adapter_revision": snapshot["revision"], "adapter_warnings": warnings,
                      "planned_index": None, "sent_index": None}
        request = self.http.build_request("POST", API_ROOT + "/responses", json=body,
                                          headers={"Authorization": "Bearer " + self.key})
        self._capture(messages, tools, body, diagnostic, str(request.url), request)
        response = await self.http.send(request, stream=True, follow_redirects=False)
        self._response_headers = {key: response.headers[key] for key in ("x-request-id", "request-id") if key in response.headers}
        if response.status_code != 200:
            try:
                await response.aread()
                try:
                    payload = response.json()
                except ValueError:
                    payload = {"detail": response.text}
                raise response_error(payload, response.status_code, self._request_id(), secrets=(self.key,))
            finally:
                await response.aclose()
        return response

    def _request_id(self):
        return self._response_headers.get("x-request-id") or self._response_headers.get("request-id")

    def _record_error(self, error):
        self.last_response_record = {"api_mode": "responses", "error": error.public(),
                                     "diagnostics": copy.deepcopy(error.diagnostic)}
        if self.response_capture:
            self.response_capture(copy.deepcopy(self.last_response_record))

    def _finish(self, payload):
        self.accounts.require_account(self.account_id, self.generation)
        if payload.get("status") != "completed":
            raise ChatGPTError("ChatGPT 响应未完整结束，本轮未提交", code="incomplete_response",
                               request_id=self._request_id())
        for item in payload.get("output", []):
            if item.get("type") == "function_call":
                if item.get("namespace") != "tavern" or item.get("name") not in self._allowed_tools:
                    raise ChatGPTError("模型返回了未授权的工具调用，本轮未提交", code="invalid_tool_call")
        # Encrypted reasoning and user-requested JWT examples are model data, not
        # authentication material. Only diagnostic metadata is subject to token-pattern redaction.
        if payload.get("diagnostics") is not None:
            payload = {**payload, "diagnostics": redact(payload["diagnostics"], (self.key,))}
        return super()._finish(payload)

    async def _consume(self, messages, tools, kwargs):
        self._last_tool_calls, self._last_assistant_extra_fields, self._last_usage = [], {}, {}
        self.last_native_response, self.last_response_record, self._response_headers = None, {}, {}
        self._completed_response = None
        response = None
        try:
            response = await self._send(messages, tools, True, kwargs)
            state = StreamState("responses")
            async for event in stream_events(response):
                self.accounts.require_account(self.account_id, self.generation)
                if not isinstance(event, dict):
                    raise ChatGPTError("ChatGPT 流事件格式无效，本轮未提交", code="invalid_stream")
                kind = event.get("type")
                if kind in ("response.failed", "response.incomplete", "error"):
                    payload = event.get("response", event)
                    if kind == "error" and "error" not in payload:
                        payload = {"error": payload}
                    raise response_error(payload, request_id=self._request_id(), secrets=(self.key,))
                if state.done:
                    raise ChatGPTError("ChatGPT 完成事件之后仍有响应内容，本轮未提交", code="invalid_stream")
                delta = state.feed(event)
                if delta:
                    yield delta
            if not state.done:
                raise ChatGPTError("ChatGPT 连接在完成事件之前结束，本轮未提交", code="stream_incomplete",
                                   request_id=self._request_id())
            self._completed_response = self._finish(state.finish())
        except ChatGPTError as error:
            self._record_error(error)
            raise
        except (httpx.HTTPError, json.JSONDecodeError):
            error = ChatGPTError("ChatGPT 连接中断或响应无效，本轮未提交", code="stream_interrupted",
                                 request_id=self._request_id())
            self._record_error(error)
            raise error from None
        finally:
            if response is not None:
                await response.aclose()

    async def _complete_chat(self, messages, **kwargs):
        tools = kwargs.get("tools") or (kwargs.get("extra_body") or {}).get("tools")
        async for _ in self._consume(messages, tools, kwargs):
            pass
        return self._completed_response

    async def _stream_chat(self, messages, *, tools=None, **kwargs):
        tools = tools or (kwargs.get("extra_body") or {}).get("tools")
        async for chunk in self._consume(messages, tools, kwargs):
            yield chunk
