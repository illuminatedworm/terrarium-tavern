"""Official Responses transport for ChatGPT subscriptions and OpenAI API keys."""

import copy
import json
from contextlib import aclosing

import httpx

from tavern.chatgpt import API_ROOT
from tavern.chatgpt.errors import ChatGPTError, redact, response_error
from tavern.core.cache_policy import endpoint, validate_openai_cache
from tavern.core.message_adapter import adapt_messages, adapter_snapshot
from tavern.core.usage import observed_usage
from tavern.scripting.model_stream import StreamState
from tavern.scripting.model_transport import ModelTransport
from tavern.scripting.native_response import prepare_messages
from tavern.scripting.protocols import definitions, response_input, response_tools
from tavern.scripting.reasoning import openai_levels
from tavern.scripting.request_context import response_origin
from tavern.scripting.sse import stream_events
from tavern.web.cloud import HOSTED_TOOL, WebSearchUnavailable, search_failure, source_links


class ChatGPTProvider(ModelTransport):
    def __init__(self, settings, key, capture=None, *, http_client=None, request_context=None):
        self.subscription = settings.auth_mode == "chatgpt_subscription"
        updates = {"api_mode": "responses", "api_path": "v1"}
        if self.subscription:
            updates.update(base_url=API_ROOT, api_key="")
        settings = settings.model_copy(deep=True, update=updates)
        self.accounts = settings._chatgpt
        self.account_id = settings.chatgpt_account_id
        self.generation = settings._auth_generation
        # OAuth refresh retains the operation identity; API keys use response_origin.
        self._owner = self.accounts.attach(self.account_id, self.generation) if self.subscription else None
        self._closed = False
        self._allowed_tools = set()
        self._completed_response = None
        self._cloud_search = False
        super().__init__(settings, key, capture, http_client=http_client, request_context=request_context)

    async def close(self):
        if not self._closed:
            self._closed = True
            if self.subscription:
                self.accounts.detach(self.account_id, self._owner)
            await super().close()

    def _body(self, messages, tools, stream, marker, params, kwargs):
        cfg = self.settings
        if self.subscription:
            messages, _ = prepare_messages(messages, response_origin(cfg))
            body = {
                "model": cfg.model,
                "input": response_input(messages),
                "store": False,
                "stream": True,
            }
        else:
            supported = openai_levels(cfg.model)
            if (
                cfg.reasoning_effort != "default"
                and supported is not None
                and cfg.reasoning_effort not in supported
            ):
                raise ValueError("当前官方 API 模型不支持所选思考档位，请重新选择；应用不会自动换档")
            # Reuse the Responses sampling, output limit and cache compiler;
            # tool authorization and maintenance remain common to both connections.
            body = super()._body(messages, tools, True, marker, params, kwargs)
        body["include"] = ["reasoning.encrypted_content"]
        for item in body["input"]:
            if item.get("role") == "system":
                item["role"] = "developer"
            if item.get("type") in ("function_call", "function_call_output"):
                if self.subscription and item.get("namespace", "tavern") != "tavern":
                    raise ChatGPTError("工具历史的命名空间与 Tavern 不匹配", code="invalid_tool_namespace")
                if not self.subscription and item.get("namespace") is not None:
                    raise ChatGPTError(
                        "官方 API 工具历史的协议已改变，请重建上下文", code="invalid_tool_namespace"
                    )
                if self.subscription:
                    item["namespace"] = "tavern"
        if kwargs.get("_maintenance_instructions"):
            body["instructions"] = kwargs["_maintenance_instructions"]
        effort = cfg.reasoning_effort
        capabilities = (
            self.accounts.model_capabilities(self.account_id, self.generation, cfg.model)
            if self.subscription
            else None
        )
        if capabilities and "supported_reasoning_levels" in capabilities:
            supported = {level["effort"] for level in capabilities["supported_reasoning_levels"]}
            if effort != "default" and effort not in supported:
                raise ChatGPTError(
                    "当前账号的此模型不支持所选思考档位，请重新选择", code="unsupported_reasoning_effort"
                )
            if effort == "default":
                effort = capabilities.get("default_reasoning_level", "default")
        if effort != "default":
            body["reasoning"] = {"effort": effort}
        threshold = kwargs.get("_compact_threshold")
        # Capacity is checked before a user-initiated turn; invalidated state
        # may also need repair before a tool continuation. Only dedicated
        # maintenance requests compress, never a normal story/tool response.
        if threshold is not None and not kwargs.get("_compact_disabled"):
            if cfg.runtime_mode not in ("full_agent", "half_agent"):
                raise ValueError("云端 compact 仅适用于官方渠道的剧情上下文")
            if type(threshold) is not int or threshold < 1024:
                raise ValueError("云端压缩阈值必须为至少 1024 tokens 的整数")
            body["context_management"] = [{"type": "compaction", "compact_threshold": threshold}]
        # Subscription cache controls stay server-managed; API keys use their visible cache settings.
        if self.subscription and self.request_context:
            body["prompt_cache_key"] = self.request_context.session_id
        extra = kwargs.get("extra_body") or {}
        choice = extra.get("tool_choice", "auto")
        self._cloud_search = bool(
            kwargs.get("_web_search")
            and kwargs.get("_web_role") in {"advisor", "card_author"}
            and kwargs.get("_web_role") == getattr(self, "operation_purpose", None)
            and choice in ("auto", "required")
            and threshold is None
            and not kwargs.get("_maintenance_instructions")
        )
        for field in ("tools", "tool_choice", "parallel_tool_calls"):
            body.pop(field, None)
        selected = definitions(tools)
        forced = None
        if isinstance(choice, dict):
            forced = choice.get("function", choice).get("name")
            if not any(tool.get("function", tool).get("name") == forced for tool in selected):
                raise ValueError("指定工具不在当前允许的工具列表中")
            if self.subscription:
                selected = [tool for tool in selected if tool.get("function", tool).get("name") == forced]
                choice = "required"
            else:
                # Official allowed_tools selects flat function names; the API
                # rejects namespace selectors. Preserve all definitions and
                # enforce the same subset again when accepting returned calls.
                choice = {
                    "type": "allowed_tools",
                    "mode": "required",
                    "tools": [{"type": "function", "name": forced}],
                }
        elif choice not in ("auto", "none", "required"):
            raise ValueError("此 ChatGPT 工具选择方式不受支持")
        self._allowed_tools = (
            ({forced} if forced else {tool.get("function", tool)["name"] for tool in selected})
            if choice != "none"
            else set()
        )
        if selected:
            functions = response_tools(selected)
            if not self.subscription:
                for function in functions:
                    # Omission means Python defaults for our tools, not null.
                    # Do not let Responses implicitly normalize these schemas
                    # to strict mode. Explicit strict schemas keep their choice.
                    function.setdefault("strict", False)
            body.update(
                tools=[
                    {
                        "type": "namespace",
                        "name": "tavern",
                        "description": "Terrarium Tavern tools",
                        "tools": functions,
                    }
                ]
                if self.subscription
                else functions,
                tool_choice=choice,
                parallel_tool_calls=bool(extra.get("parallel_tool_calls", False)),
            )
        elif choice == "required":
            if not self._cloud_search:
                raise ValueError("当前请求没有可调用的工具")
        if self._cloud_search:
            body.setdefault("tools", []).append(copy.deepcopy(HOSTED_TOOL))
            body["include"].append("web_search_call.action.sources")
            body["tool_choice"] = choice
        if not self.subscription and marker and marker.get("field") == "prompt_cache_breakpoint":
            self._breakpoint_history().restore(body, "responses", self._breakpoint_identity())
        return body

    async def _send(self, messages, tools, stream, kwargs):
        if not self.subscription:
            validate_openai_cache(self.settings)
        if self.subscription:
            account = await self.accounts.access(
                self.account_id, use_proxy=self.settings.use_proxy, generation=self.generation
            )
            self.key = account["tokens"]["access_token"]
        snapshot = adapter_snapshot(self.settings)
        messages, _, warnings = adapt_messages(messages, self.settings, snapshot)
        if self.subscription:
            body = self._body(messages, tools, True, None, {}, kwargs)
            diagnostic = {
                "mode": "cloud",
                "status": "cloud",
                "kind": "implicit",
                "parameters": {},
                "reason": "ChatGPT 订阅由服务端管理缓存；未发送显式缓存控制",
                "planned_index": None,
                "sent_index": None,
            }
        else:
            body, diagnostic, _ = await self._prepare(messages, tools, True, kwargs)
        diagnostic.update(adapter_revision=snapshot["revision"], adapter_warnings=warnings)
        url = endpoint(self.settings.base_url, "responses", api_path="v1")
        request = self.http.build_request(
            "POST", url, json=body, headers={"Authorization": "Bearer " + self.key}
        )
        if not self.subscription:
            history, identity = self._breakpoint_history(), self._breakpoint_identity()
            sent = False

            def remember_sent():
                nonlocal sent
                if not sent:
                    history.remember(identity, diagnostic.get("sent_breakpoints", []))
                    sent = True

            async def trace(name, info):
                if name in ("http11.send_request_body.complete", "http2.send_request_body.complete"):
                    remember_sent()

            request.extensions["trace"] = trace
        await self._capture(messages, tools, body, diagnostic, str(request.url), request)
        response = await self._failure_debug.send(self.http, request, stream=True, follow_redirects=False)
        if not self.subscription:
            remember_sent()
        self._response_headers = {
            key: response.headers[key] for key in ("x-request-id", "request-id") if key in response.headers
        }
        if response.status_code != 200:
            try:
                await response.aread()
                try:
                    payload = response.json()
                except ValueError:
                    payload = {"detail": response.text}
                raise response_error(
                    payload,
                    response.status_code,
                    self._request_id(),
                    secrets=(self.key,),
                    subscription=self.subscription,
                )
            finally:
                await response.aclose()
        return response

    def _request_id(self):
        return self._response_headers.get("x-request-id") or self._response_headers.get("request-id")

    def _record_error(self, error, terminal=None):
        if not self.subscription:
            error.usage_url = "https://platform.openai.com/usage"
        self.last_response_record = {
            "api_mode": "responses",
            "error": error.public(),
            "diagnostics": copy.deepcopy(error.diagnostic),
        }
        if not self.subscription:
            payload = terminal or error.diagnostic.get("body") or {}
            if isinstance(payload, dict):
                payload = payload.get("response", payload)
            raw = payload.get("usage") if isinstance(payload, dict) else None
            if isinstance(raw, dict) and raw:
                # Billed work is independent of narrative success. Never call
                # meter.finish here: the outer failure path must retain the last
                # valid window observation and mark this attempt as failed.
                observed = observed_usage(raw, "responses")
                billed = {
                    "prompt_tokens": observed["tokens"],
                    "completion_tokens": observed["output_tokens"],
                    "total_tokens": observed["reported_total_tokens"],
                    "cached_tokens": observed["cached_input_tokens"],
                    "cache_write_tokens": observed["cache_write_input_tokens"],
                    "raw_usage": copy.deepcopy(raw),
                }
                # Terrarium's numeric compatibility view is separate from the
                # persisted billing evidence, where absent counters stay null.
                self._last_usage = {
                    **billed,
                    **{
                        key: billed[key] or 0
                        for key in ("prompt_tokens", "completion_tokens", "total_tokens")
                    },
                }
                self.usage_history.append(copy.deepcopy(billed))
                self.last_response_record.update(
                    response_id=payload.get("id"),
                    raw_usage=copy.deepcopy(raw),
                    completion={"action": "incomplete", "reason": payload.get("status", "failed")},
                )
                if self.failed_usage_capture:
                    self.failed_usage_capture(
                        {
                            "usage": copy.deepcopy(billed),
                            "calls": 1,
                            "status": "failed",
                            "reason": str(error),
                        }
                    )
        if self.response_capture:
            self.response_capture(copy.deepcopy(self.last_response_record))

    async def _finish(self, payload):
        if self.subscription:
            self.accounts.require_account(self.account_id, self.generation)
        if payload.get("status") != "completed":
            raise response_error(
                payload, request_id=self._request_id(), secrets=(self.key,), subscription=self.subscription
            )
        for item in payload.get("output", []):
            if item.get("type") == "web_search_call":
                if not self._cloud_search:
                    raise ChatGPTError("模型返回了未授权的云端搜索，本轮未提交", code="invalid_tool_call")
                if item.get("status") in {"failed", "incomplete"}:
                    error = response_error(
                        item,
                        request_id=self._request_id(),
                        secrets=(self.key,),
                        subscription=self.subscription,
                    )
                    raise ChatGPTError(str(error), code="web_search_failed", diagnostic=error.diagnostic)
            if item.get("type") == "function_call":
                expected_namespace = "tavern" if self.subscription else None
                if item.get("namespace") != expected_namespace or item.get("name") not in self._allowed_tools:
                    raise ChatGPTError("模型返回了未授权的工具调用，本轮未提交", code="invalid_tool_call")
        # Encrypted reasoning and user-requested JWT examples are model data, not
        # authentication material. Only diagnostic metadata is subject to token-pattern redaction.
        if payload.get("diagnostics") is not None:
            payload = {**payload, "diagnostics": redact(payload["diagnostics"], (self.key,))}
        result = await super()._finish(payload)
        sources = source_links(payload)
        if sources:
            self._last_assistant_extra_fields["web_sources"] = sources
        return result

    async def _consume(self, messages, tools, kwargs):
        self._last_tool_calls, self._last_assistant_extra_fields, self._last_usage = [], {}, {}
        self.last_native_response, self.last_response_record, self._response_headers = None, {}, {}
        self.last_completion = None
        self._completed_response = None
        terminal = None
        response = None
        if kwargs.get("_full_agent_story"):
            # Only Full Agent accepts the visible Kemini transport here.
            tools = self._begin_kemini(tools, kwargs)
        else:
            # Other ChatGPT requests keep plain transport; never inherit the
            # previous story request's Kemini state on a reused provider.
            self._reset_kemini()
        event_sink = kwargs.get("_web_event") or (lambda kind, payload: None)
        try:
            response = await self._send(messages, tools, True, kwargs)
            state = StreamState("responses", secret=self.key)
            async for event in stream_events(response):
                if self.subscription:
                    self.accounts.require_account(self.account_id, self.generation)
                if not isinstance(event, dict):
                    raise ChatGPTError("ChatGPT 流事件格式无效，本轮未提交", code="invalid_stream")
                kind = event.get("type")
                if kind in ("response.completed", "response.incomplete", "response.failed"):
                    terminal = event.get("response")
                if self._cloud_search and kind in {
                    "response.web_search_call.in_progress",
                    "response.web_search_call.searching",
                    "response.web_search_call.completed",
                }:
                    event_sink(
                        "web_activity",
                        {
                            "service": "openai",
                            "auth_mode": "oauth" if self.subscription else "api_key",
                            "operation": "search",
                            "call_id": event.get("item_id"),
                            "status": "completed" if kind.endswith(".completed") else "started",
                        },
                    )
                if kind in ("response.failed", "error"):
                    payload = event.get("response", event)
                    if kind == "error" and "error" not in payload:
                        payload = {"error": payload}
                    raise response_error(
                        payload,
                        request_id=self._request_id(),
                        secrets=(self.key,),
                        subscription=self.subscription,
                    )
                try:
                    delta = state.feed(event)
                except ValueError as error:
                    raise ChatGPTError(
                        str(error), code="invalid_stream", request_id=self._request_id()
                    ) from None
                delta = self._preview_delta(event, delta, "responses")
                if delta:
                    yield delta
            if not state.completion_seen:
                raise ChatGPTError(
                    "ChatGPT 连接在完成事件之前结束，本轮未提交",
                    code="stream_incomplete",
                    request_id=self._request_id(),
                )
            if state.result.get("status") != "completed":
                raise response_error(
                    state.result,
                    request_id=self._request_id(),
                    secrets=(self.key,),
                    subscription=self.subscription,
                )
            try:
                payload = state.finish()
            except ValueError as error:
                raise ChatGPTError(str(error), code="invalid_stream", request_id=self._request_id()) from None
            self._completed_response = await self._finish(payload)
            if self._last_assistant_extra_fields.get("web_sources"):
                event_sink("web_sources", {"sources": self._last_assistant_extra_fields["web_sources"]})
        except ChatGPTError as error:
            self._record_error(error, terminal)
            if self._cloud_search and search_failure(error):
                raise WebSearchUnavailable(str(error)) from None
            raise
        except (httpx.HTTPError, json.JSONDecodeError):
            error = ChatGPTError(
                "ChatGPT 连接中断或响应无效，本轮未提交",
                code="stream_interrupted",
                request_id=self._request_id(),
            )
            self._record_error(error, terminal)
            raise error from None
        finally:
            if response is not None:
                await response.aclose()

    async def _complete_chat(self, messages, *, on_text=None, **kwargs):
        tools = kwargs.get("tools") or (kwargs.get("extra_body") or {}).get("tools")
        async with aclosing(self._consume(messages, tools, kwargs)) as chunks:
            async for chunk in chunks:
                if on_text:
                    on_text(chunk)
        return self._completed_response

    async def _stream_chat(self, messages, *, tools=None, **kwargs):
        tools = tools or (kwargs.get("extra_body") or {}).get("tools")
        async with aclosing(self._consume(messages, tools, kwargs)) as chunks:
            async for chunk in chunks:
                yield chunk
