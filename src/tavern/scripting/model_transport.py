"""Four HTTP protocols behind the same Terrarium chat/tool contract."""

import copy
import hashlib
import json
import uuid
from collections.abc import Callable
from contextlib import aclosing

import httpx
from kohakuterrarium.llm.base import BaseLLMProvider, ChatResponse, LLMConfig, NativeToolCall
from openai import AsyncOpenAI

from tavern.core.cache_policy import (
    effective_retention,
    endpoint,
    fingerprint,
    normalize_usage,
    preset,
    retention_request,
)
from tavern.core.gemini_capabilities import gemini_controls
from tavern.core.kemini import transport_tools
from tavern.core.message_adapter import adapt_messages, adapter_snapshot
from tavern.core.tokens import counting_info, request_tokens
from tavern.scripting.cloud_cache import (
    DISABLED,
    cache_rejection,
    capability_key,
    gemini_object,
    invalidate_object,
)
from tavern.scripting.failure_debug import FailureDebug
from tavern.scripting.genai_transport import GenAITransport
from tavern.scripting.http_errors import response_error
from tavern.scripting.kemini import KeminiReply
from tavern.scripting.model_stream import StreamState
from tavern.scripting.native_response import attachment, prepare_messages, response_completion
from tavern.scripting.protocols import (
    definitions,
    native_messages,
    native_tools,
    openai_messages,
    response_input,
    response_tools,
)
from tavern.scripting.reasoning import anthropic_thinking, gemini_effort, gemini_thinking
from tavern.scripting.request_context import identity_headers, response_origin
from tavern.scripting.sent_breakpoints import APIS, FIELD, SentBreakpoints, marked_boundaries
from tavern.scripting.sse import stream_events


class ModelTransport(BaseLLMProvider):
    def __init__(self, settings, key, capture=None, *, http_client=None, request_context=None):
        super().__init__(
            LLMConfig(
                model=settings.model, temperature=settings.temperature, max_tokens=settings.reply_tokens
            )
        )
        self.settings, self.key, self.capture = settings, key, capture
        self._http = (
            http_client
            if http_client is not None
            else httpx.AsyncClient(
                proxy=(settings.proxy or None) if settings.use_proxy else None, timeout=120, trust_env=False
            )
        )
        self._client = (
            None
            if settings.api_mode == "gemini"
            else AsyncOpenAI(api_key=key, base_url=settings.base_url, max_retries=0, http_client=self._http)
        )
        self._genai = None
        self._failure_debug = FailureDebug()
        self.capability_key = capability_key(settings, key)
        self._last_usage, self._last_assistant_extra_fields = {}, {}
        self.usage_history = []
        self.echo_reasoning = True
        self.request_context = request_context
        self.last_native_response = None
        self.last_completion = None
        self.last_response_record = {}
        self.response_capture = None
        self.auxiliary_capture = None
        self._reset_kemini()
        self.request_observer: Callable[[httpx.Request], None] | None = None
        self._response_headers = {}
        self._sent_breakpoints = SentBreakpoints()
        if request_context:
            request_context.bind(response_origin(settings, key))

    @property
    def http(self):
        return self._client._client if self._client else self._http

    @property
    def genai(self):
        if self._genai is None:
            self._genai = GenAITransport(
                self.settings.base_url, self.settings.api_path, self.key, self.http, debug=self._failure_debug
            )
        return self._genai

    async def chat_complete(self, messages, **kwargs):
        with self._failure_debug.operation(self) as capture:
            result = await super().chat_complete(messages, **kwargs)
            if capture is not None and result.finish_reason in ("length", "content_filter"):
                capture.error_type = "IncompleteResponse"
            return result

    async def chat(self, messages, **kwargs):
        with self._failure_debug.operation(self):
            async with aclosing(super().chat(messages, **kwargs)) as chunks:
                async for chunk in chunks:
                    yield chunk

    async def _recover_from_overflow(self, current, state):
        return None

    @property
    def last_usage(self):
        # Terrarium's numeric accounting cannot add None. Preserve the real values
        # separately for the application's usage event and keep raw evidence.
        return {
            **self._last_usage,
            "cached_tokens": self._last_usage.get("cached_tokens") or 0,
            "cache_write_tokens": self._last_usage.get("cache_write_tokens") or 0,
        }

    @property
    def reported_usage(self):
        result = {
            key: sum(u[key] for u in self.usage_history)
            for key in ("prompt_tokens", "completion_tokens", "total_tokens")
        }
        for key in ("cached_tokens", "cache_write_tokens"):
            result[key] = (
                sum(u[key] for u in self.usage_history)
                if self.usage_history and all(u[key] is not None for u in self.usage_history)
                else None
            )
        result["raw_usage"] = [u["raw_usage"] for u in self.usage_history]
        return result

    def with_model(self, name):
        if not name or name == self.config.model:
            return self
        clone = copy.copy(self)
        clone.settings = self.settings.model_copy(deep=True, update={"model": name})
        clone.config = (
            self.config.model_copy(update={"model": name})
            if hasattr(self.config, "model_copy")
            else copy.copy(self.config)
        )
        clone.config.model = name
        clone.capability_key = capability_key(clone.settings, self.key)
        clone._last_tool_calls, clone._last_usage, clone._last_assistant_extra_fields, clone.usage_history = (
            [],
            {},
            {},
            [],
        )
        clone.last_native_response, clone.last_response_record, clone._response_headers = None, {}, {}
        clone.last_completion = None
        clone.request_context = copy.deepcopy(self.request_context)
        clone._sent_breakpoints = copy.deepcopy(self._sent_breakpoints)
        if clone.request_context:
            clone.request_context.bind(response_origin(clone.settings, clone.key))
        return clone

    async def close(self):
        try:
            if self._genai is not None:
                await self._genai.close()
        finally:
            if self._client:
                await self._client.close()
            else:
                await self.http.aclose()

    def _breakpoint_history(self):
        return (
            self.request_context.sent_breakpoints
            if self.request_context and self.request_context.sent_breakpoints is not None
            else self._sent_breakpoints
        )

    def _breakpoint_identity(self):
        origin = response_origin(self.settings, self.key)
        if not self.request_context or self.request_context.sent_breakpoints is None:
            origin["scope"] = (
                self.request_context.session_id if self.request_context else self.settings.cache_scope
            )
        return fingerprint(origin)

    def _retains_breakpoints(self, selected):
        return self.settings.api_mode in APIS and selected.get("field") == FIELD

    def _request(self, messages, *, tools=None, **kwargs):
        messages, _, _ = adapt_messages(messages, self.settings)
        selected = preset(self.settings)
        enabled = self.settings.cache.mode == "runtime" and (
            any(m.get("_cache_breakpoint") for m in messages) or self._retains_breakpoints(selected)
        )
        marker = selected if enabled and selected["kind"] == "block" else None
        tools = tools or (kwargs.get("extra_body") or {}).get("tools")
        return self._body(
            messages,
            tools,
            False,
            marker,
            selected["request"] if marker or selected["kind"] == "implicit" else {},
            kwargs,
        )

    def _body(self, messages, tools, stream, marker, params, kwargs):
        cfg, api = self.settings, self.settings.api_mode
        messages, _ = prepare_messages(messages, response_origin(cfg, self.key))
        maximum = kwargs.get("max_tokens", self.config.max_tokens)
        temperature = kwargs.get("temperature", self.config.temperature)
        sampling, _ = gemini_controls(api, cfg.model)
        if not sampling:
            temperature = None
        extra = kwargs.get("extra_body") or {}
        choice = extra.get("tool_choice", "auto")
        if api == "chat_completions":
            body = {"model": cfg.model, "messages": openai_messages(messages, marker), "max_tokens": maximum}
            if tools:
                body.update(
                    tools=definitions(tools),
                    tool_choice=choice,
                    parallel_tool_calls=extra.get("parallel_tool_calls", False),
                )
            for field in ("top_p", "top_k", "frequency_penalty", "presence_penalty"):
                if getattr(cfg, field) is not None and (sampling or field not in ("top_p", "top_k")):
                    body[field] = getattr(cfg, field)
            if cfg.reasoning_effort != "default":
                effort = gemini_effort(cfg.model, cfg.reasoning_effort)
                body.update(
                    {"reasoning_effort": effort}
                    if cfg.reasoning_format == "reasoning_effort"
                    else {"reasoning": {"effort": effort}}
                )
            if stream:
                body.update(stream=True, stream_options={"include_usage": True})
        elif api == "responses":
            body = {
                "model": cfg.model,
                "input": response_input(messages, marker),
                "store": False,
                "max_output_tokens": maximum,
            }
            if tools:
                if isinstance(choice, dict) and choice.get("type") == "function":
                    choice = {"type": "function", "name": choice.get("function", choice)["name"]}
                body.update(
                    tools=response_tools(tools),
                    tool_choice=choice,
                    parallel_tool_calls=extra.get("parallel_tool_calls", False),
                )
            if cfg.top_p is not None:
                body["top_p"] = cfg.top_p
            if cfg.reasoning_effort != "default":
                body.update(
                    reasoning={"effort": cfg.reasoning_effort}, include=["reasoning.encrypted_content"]
                )
            if stream:
                body["stream"] = True
            if "tool_choice" in extra:
                choice = extra["tool_choice"]
                if isinstance(choice, dict) and choice.get("type") == "function":
                    choice = {"type": "function", "name": choice.get("function", choice)["name"]}
                body["tool_choice"] = choice
            if "parallel_tool_calls" in extra:
                body["parallel_tool_calls"] = extra["parallel_tool_calls"]
        else:
            system, contents, _ = native_messages(messages, api, marker)
            if api == "anthropic":
                body = {"model": cfg.model, "system": system, "messages": contents, "max_tokens": maximum}
                policy, thinking = anthropic_thinking(cfg.model, cfg.reasoning_effort)
                if tools:
                    disable_parallel = not extra.get("parallel_tool_calls", False)
                    selection = {"type": "auto", "disable_parallel_tool_use": disable_parallel}
                    selected_tools = tools
                    if isinstance(choice, dict):
                        name = choice.get("function", choice)["name"]
                        if not policy.forced_tools:
                            # Full Agent still enforces new_context during forced
                            # rollover. Expose only the requested tool, using the
                            # auto mode accepted by the model's native API.
                            selected_tools = [
                                t for t in definitions(tools) if t.get("function", t)["name"] == name
                            ]
                            if not selected_tools:
                                raise ValueError("指定工具不在当前允许的工具列表中")
                        else:
                            selection = {
                                "type": "tool",
                                "name": name,
                                "disable_parallel_tool_use": disable_parallel,
                            }
                    elif choice == "none":
                        selection = {"type": "none"}
                    body.update(tools=native_tools(selected_tools, api), tool_choice=selection)
                body.update(thinking)
                thinking_on = thinking.get("thinking", {}).get("type") == "adaptive"
                if not policy.sampling or thinking_on:
                    temperature = None
                if cfg.top_p is not None and policy.sampling and (not thinking_on or cfg.top_p >= 0.95):
                    body["top_p"] = cfg.top_p
                if cfg.top_k is not None and policy.sampling and not thinking_on:
                    body["top_k"] = cfg.top_k
                if stream:
                    body["stream"] = True
            else:
                generation = {"maxOutputTokens": maximum}
                if temperature is not None:
                    generation["temperature"] = temperature
                if cfg.top_p is not None and sampling:
                    generation["topP"] = cfg.top_p
                if cfg.top_k is not None and sampling:
                    generation["topK"] = cfg.top_k
                thinking = gemini_thinking(cfg.model, cfg.reasoning_effort)
                if thinking:
                    generation["thinkingConfig"] = thinking
                body = {
                    "systemInstruction": {"parts": system},
                    "contents": contents,
                    "generationConfig": generation,
                }
                if tools:
                    fc = {"mode": "AUTO"}
                    if isinstance(choice, dict):
                        fc = {"mode": "ANY", "allowedFunctionNames": [choice.get("function", choice)["name"]]}
                    elif choice == "none":
                        fc = {"mode": "NONE"}
                    body.update(tools=native_tools(tools, api), toolConfig={"functionCallingConfig": fc})
        if api != "gemini" and temperature is not None:
            body["temperature"] = temperature
        body.update(copy.deepcopy(params))
        if cfg.auth_mode != "chatgpt_subscription" and "context_management" in body:
            raise ValueError("普通渠道使用本地上下文机制，不能通过自定义请求参数启用云端 compact")
        if self.request_context:
            self.request_context.apply(body, cfg)
        if api in APIS and marker and marker.get("field") == FIELD:
            self._breakpoint_history().restore(body, api, self._breakpoint_identity())
        return body

    async def _prepare(self, messages, tools, stream, kwargs, *, fallback=None):
        cfg, api = self.settings, self.settings.api_mode
        messages, invalid_native = prepare_messages(messages, response_origin(cfg, self.key))
        explicit = gemini_controls(api, cfg.model)[1]
        if explicit and cfg.cache.mode == "runtime" and not any(m.get("_cache_breakpoint") for m in messages):
            # Standalone editor/maintenance calls can opt in without a story history.
            # Annotated story messages explicitly exclude dynamic system content.
            target = None
            for index, message in enumerate(messages):
                if message["role"] not in ("system", "developer") or not message.get("_cache_static", True):
                    break
                if message.get("content"):
                    target = index
            if target is not None:
                messages = copy.deepcopy(messages)
                messages[target]["_cache_breakpoint"] = True
        selected = preset(cfg)
        planned = next((i for i, m in enumerate(messages) if explicit and m.get("_cache_breakpoint")), None)
        diagnostic = {
            "mode": cfg.cache.mode,
            "status": "cloud",
            "planned_index": planned,
            "sent_index": None,
            "reason": fallback,
            "kind": selected["kind"],
        }
        if invalid_native:
            diagnostic["native_replay_invalidations"] = invalid_native
        if planned is not None:
            diagnostic.update(
                source=messages[planned].get("_cache_source"),
                message_id=messages[planned].get("_cache_message_id"),
            )
        marker, params, resource = (
            None,
            (retention_request(cfg) if cfg.cache.mode == "cloud" and not fallback else {}),
            None,
        )
        diagnostic["retention"] = effective_retention(cfg)[1]
        if not explicit:
            diagnostic.update(mode="cloud", reason="当前 Gemini 模型仅支持隐式缓存，本地缓存配置不适用")
        if cfg.cache.mode == "runtime":
            reason = fallback or DISABLED.get(self.capability_key)
            if (
                not reason
                and planned is None
                and selected["kind"] != "implicit"
                and not self._retains_breakpoints(selected)
            ):
                reason = "没有可用的固定前缀断点"
            if not reason and selected["kind"] == "unknown":
                reason = "模型没有已匹配的缓存参数，请手动配置"
            if not reason and selected["kind"] == "gemini":
                if api not in ("gemini", "chat_completions"):
                    reason = "当前协议不支持 Gemini 缓存对象引用"
                else:
                    try:
                        resource, reason = await gemini_object(
                            self, messages, tools, (kwargs.get("extra_body") or {}).get("tool_choice", "auto")
                        )
                    except ValueError:
                        resource, reason = (
                            None,
                            "当前消息映射不能用于原生缓存对象；按所选消息角色发送普通请求",
                        )
            if reason:
                diagnostic.update(status="fallback", reason=reason)
            else:
                diagnostic.update(status="sent", sent_index=planned)
                params = selected["request"]
                if selected["kind"] == "block":
                    marker = selected
                elif selected["kind"] == "implicit":
                    diagnostic.update(
                        status="cloud",
                        sent_index=None,
                        reason=(
                            "当前 Gemini 模型仅支持隐式缓存，不发送显式缓存对象、断点或时效参数"
                            if not gemini_controls(api, cfg.model)[1]
                            else "该模型由服务端管理缓存前缀，仅发送时效参数"
                        ),
                    )
        body = self._body(messages, tools, stream, marker, params, kwargs)
        if resource:
            if api == "gemini":
                body["contents"] = body["contents"][resource["boundary"] :]
                for field in ("systemInstruction", "tools", "toolConfig"):
                    body.pop(field, None)
                body["cachedContent"] = resource["name"]
            else:
                # Google-compatible Chat can reference an object created through the native endpoint.
                suffix = copy.deepcopy(messages[planned + 1 :])
                for message in suffix:
                    if message["role"] in ("system", "developer") and not message.get("_adapter_typed"):
                        message["role"] = "user"
                        message["content"] = (
                            "<tavern_runtime_context>\n"
                            + str(message.get("content", ""))
                            + "\n</tavern_runtime_context>"
                        )
                body["messages"] = openai_messages(suffix)
                body.pop("tools", None)
                body.pop("tool_choice", None)
                body.pop("parallel_tool_calls", None)
                body["extra_body"] = {"google": {"cached_content": resource["name"]}}
            diagnostic["resource"] = resource["name"]
        diagnostic["parameters"] = {marker["field"]: marker["value"], **params} if marker else params
        if api in APIS and marker and marker.get("field") == FIELD:
            diagnostic["sent_breakpoints"] = marked_boundaries(body, api)
            if planned is None and not diagnostic["sent_breakpoints"]:
                return await self._prepare(messages, tools, stream, kwargs, fallback="没有可用的固定前缀断点")
        diagnostic["prefix_sha256"] = fingerprint(messages[: planned + 1]) if planned is not None else None
        return body, diagnostic, resource

    def _capture(self, messages, tools, body, diagnostic, url, request=None):
        if request is not None and self.request_observer is not None:
            self.request_observer(request)
        if not self.capture:
            return
        cfg = self.settings
        wire_messages = body.get("messages", body.get("input", body.get("contents", [])))
        effective_messages, _ = prepare_messages(messages, response_origin(cfg, self.key))
        cost = request_tokens(effective_messages, cfg, definitions(tools))
        payload = {
            "model": cfg.model,
            "api_mode": cfg.api_mode,
            "url": url,
            "messages": wire_messages,
            "wire": copy.deepcopy(body),
            "cache": diagnostic,
            "tools": [t.get("function", t).get("name") for t in definitions(tools)],
            "tools_sha256": fingerprint(definitions(tools)),
            "reasoning": cfg.reasoning_effort,
            "estimated_input_tokens": cost,
            "context_warning": "估算超出模型上下文，完整发送"
            if cost + cfg.reply_tokens + 2048 > cfg.context_tokens
            else None,
            "token_counting": counting_info(cfg),
        }
        if request is not None:
            payload.update(
                headers=identity_headers(request.headers),
                wire_sha256=hashlib.sha256(request.content).hexdigest(),
            )
        if self.request_context:
            payload["request_context"] = self.request_context.audit()
        serialized = json.dumps(payload, ensure_ascii=False)
        if len(self.key) >= 8:
            serialized = serialized.replace(self.key, "[REDACTED]")
        self.capture(json.loads(serialized))

    async def _send(self, messages, tools, stream, kwargs):
        cfg, api = self.settings, self.settings.api_mode
        snapshot = adapter_snapshot(cfg)
        messages, _, adapter_warnings = adapt_messages(messages, cfg, snapshot)
        url = endpoint(cfg.base_url, api, cfg.model, stream=stream, api_path=cfg.api_path)
        headers = (
            {"x-api-key": self.key, "anthropic-version": "2023-06-01"}
            if api == "anthropic"
            else {"Authorization": "Bearer " + self.key}
        )
        if self.request_context:
            self.request_context.bind(response_origin(cfg, self.key))
            headers.update(self.request_context.headers(api))
        fallback = None
        for attempt in range(2):
            body, diagnostic, resource = await self._prepare(
                messages, tools, stream, kwargs, fallback=fallback
            )
            diagnostic.update(adapter_revision=snapshot["revision"], adapter_warnings=adapter_warnings)
            request = self.http.build_request("POST", url, headers=headers, json=body)
            history, identity = self._breakpoint_history(), self._breakpoint_identity()
            sent = False

            def remember_sent():
                nonlocal sent
                if not sent:
                    history.remember(identity, diagnostic.get("sent_breakpoints", []))
                    sent = True

            async def trace(name, info):
                # HTTPcore confirms body transmission even if reading the
                # response later times out, fails, or is cancelled.
                if name in ("http11.send_request_body.complete", "http2.send_request_body.complete"):
                    remember_sent()

            request.extensions["trace"] = trace
            self._capture(messages, tools, body, diagnostic, url, request)
            response = await self._failure_debug.send(self.http, request, stream=stream)
            # Custom transports may not expose HTTPcore tracing. Receiving a
            # response still proves dispatch, regardless of status or parsing.
            remember_sent()
            if response.status_code < 400:
                self._response_headers = {
                    k: response.headers[k] for k in ("request-id", "x-request-id") if k in response.headers
                }
                if self.request_context:
                    self.request_context.receive(api, response.headers)
                return response
            try:
                await response.aread()
                try:
                    payload = response.json()
                except ValueError:
                    payload = {}
                selected = preset(cfg)
                fields = [selected.get("field", "cachedContent"), *selected["request"]]
                retry = (
                    attempt == 0
                    and diagnostic["status"] == "sent"
                    and not (self.request_context and self.request_context.identity_rejection(api, payload))
                    and cache_rejection(response.status_code, payload, fields, resource=bool(resource))
                )
                if not retry:
                    raise response_error(response, self.key)
            finally:
                await response.aclose()
            fallback = "服务商拒绝缓存参数或缓存对象已过期；已完整发送上下文"
            if resource:
                await invalidate_object(self, resource)
            if not resource or response.status_code != 404:
                DISABLED[self.capability_key] = fallback
        raise ValueError("缓存降级请求失败")

    async def _gemini_events(self, messages, tools, stream, kwargs):
        """Use public SDK calls; only retry an explicitly rejected cache before output."""
        cfg = self.settings
        snapshot = adapter_snapshot(cfg)
        messages, _, warnings = adapt_messages(messages, cfg, snapshot)
        fallback = None
        for attempt in range(2):
            body, diagnostic, resource = await self._prepare(
                messages, tools, stream, kwargs, fallback=fallback
            )
            diagnostic.update(
                adapter_revision=snapshot["revision"], adapter_warnings=warnings, transport="google-genai"
            )

            def capture(request):
                self._capture(
                    messages, tools, json.loads(request.content), diagnostic, str(request.url), request
                )

            emitted = False
            try:
                if stream:
                    async with aclosing(self.genai.stream(cfg.model, body, capture)) as events:
                        async for payload, headers in events:
                            emitted = True
                            self._response_headers = {
                                k: v
                                for k, v in headers.items()
                                if k.lower() in ("request-id", "x-request-id")
                            }
                            yield payload
                else:
                    payload, headers = await self.genai.generate(cfg.model, body, capture)
                    self._response_headers = {
                        k: v for k, v in headers.items() if k.lower() in ("request-id", "x-request-id")
                    }
                    yield payload
                return
            except httpx.HTTPStatusError as error:
                try:
                    payload = error.response.json()
                except ValueError:
                    payload = {}
                if (
                    emitted
                    or attempt
                    or not resource
                    or not cache_rejection(
                        error.response.status_code, payload, ["cachedContent"], resource=True
                    )
                ):
                    raise
                fallback = "服务商拒绝缓存对象或缓存对象已过期；已完整发送上下文"
                await invalidate_object(self, resource)
                if error.response.status_code != 404:
                    DISABLED[self.capability_key] = fallback

    def _reset_kemini(self):
        self._kemini, self.last_auxiliary, self._story_tool = None, [], False

    def _begin_kemini(self, tools, kwargs):
        # Full Agent accepts ordinary prose itself and keeps the opt-in Kemini
        # transport call paired with its native result, including mixed batches.
        self._reset_kemini()
        self._story_tool = kwargs.get("_full_agent_story", False)
        config = self.settings.kemini
        if (
            getattr(self, "operation_purpose", None) == "narrative"
            and config.enabled
            and not kwargs.get("_maintenance_instructions")
            and not kwargs.get("_compact_threshold")
        ):
            choice = (kwargs.get("extra_body") or {}).get("tool_choice", "auto")
            self._kemini = KeminiReply(config, choice == "auto", provisional=self._story_tool)
            if self.auxiliary_capture and not self._story_tool:
                self.auxiliary_capture([])
            return transport_tools(tools, config, choice)
        return tools

    def _finish(self, payload):
        try:
            return self._finish_payload(payload)
        except ValueError as error:
            if self._kemini:
                self.last_response_record = {
                    "api_mode": self.settings.api_mode,
                    "raw_response": copy.deepcopy(payload),
                    "error": str(error),
                }
                if self.response_capture:
                    self.response_capture(copy.deepcopy(self.last_response_record))
            raise

    def _finish_payload(self, payload):
        api = self.settings.api_mode
        self._last_tool_calls, self._last_assistant_extra_fields = [], {}
        model = payload.get("model", payload.get("modelVersion")) or self.settings.model
        finish, text, raw = "stop", "", payload.get("usage") or {}
        if api == "chat_completions":
            choice = (payload.get("choices") or [{}])[0]
            message = choice.get("message") or {}
            text, finish = message.get("content") or "", choice.get("finish_reason")
            # A successful HTTP response can still contain an unfinished relay
            # reply. Apply the streaming completion boundary before exposing
            # prose or tool calls to any agent.
            if finish not in ("stop", "tool_calls", "length", "content_filter"):
                raise ValueError("Chat Completions 响应未完整结束，本回合未提交")
            self._last_tool_calls = [
                NativeToolCall(id=c["id"], name=c["function"]["name"], arguments=c["function"]["arguments"])
                for c in message.get("tool_calls") or []
            ]
            self._last_assistant_extra_fields = {
                k: message[k] for k in ("reasoning_content", "reasoning_details", "reasoning") if k in message
            }
        elif api == "responses":
            if payload.get("status") != "completed":
                raise ValueError("Responses 请求未完整结束，本回合未提交")
            reasoning = []
            for item in payload.get("output", []):
                if item["type"] == "function_call":
                    self._last_tool_calls.append(
                        NativeToolCall(id=item["call_id"], name=item["name"], arguments=item["arguments"])
                    )
                elif item["type"] == "message":
                    text += "".join(p.get("text", "") for p in item.get("content", []))
                elif item["type"] == "reasoning" and item.get("encrypted_content"):
                    reasoning.append(copy.deepcopy(item))
            if reasoning:
                self._last_assistant_extra_fields = {"reasoning": reasoning}
        elif api == "anthropic":
            thinking = []
            for part in payload.get("content", []):
                if part["type"] == "text":
                    text += part["text"]
                elif part["type"] == "tool_use":
                    self._last_tool_calls.append(
                        NativeToolCall(
                            id=part["id"],
                            name=part["name"],
                            arguments=json.dumps(part["input"], ensure_ascii=False),
                        )
                    )
                elif part["type"] in ("thinking", "redacted_thinking"):
                    thinking.append(copy.deepcopy(part))
            if thinking:
                self._last_assistant_extra_fields = {
                    "reasoning_details": [{"provider": api, "thinking": thinking}]
                }
            finish = {"max_tokens": "length", "refusal": "content_filter"}.get(
                payload.get("stop_reason"), "stop"
            )
            if not payload.get("stop_reason"):
                raise ValueError("Anthropic 响应未完整结束")
        else:
            raw = payload.get("usageMetadata") or {}
            candidate = (payload.get("candidates") or [{}])[0]
            parts = candidate.get("content", {}).get("parts", [])
            for part in parts:
                if "functionCall" in part:
                    call = part["functionCall"]
                    self._last_tool_calls.append(
                        NativeToolCall(
                            id=call.get("id") or "gemini_" + uuid.uuid4().hex,
                            name=call["name"],
                            arguments=json.dumps(call.get("args", {}), ensure_ascii=False),
                        )
                    )
                elif not part.get("thought"):
                    text += part.get("text", "")
            self._last_assistant_extra_fields = {
                "reasoning_details": [{"provider": api, "parts": copy.deepcopy(parts)}]
            }
            reason = candidate.get("finishReason")
            finish = "stop" if reason == "STOP" else "length" if reason == "MAX_TOKENS" else "content_filter"
            if not reason:
                raise ValueError("Gemini 响应未完整结束")
        self._last_usage = normalize_usage(raw, api)
        self.usage_history.append(copy.deepcopy(self._last_usage))
        self.last_completion = response_completion(api, payload, self._last_tool_calls)
        original_text, rewritten = text, False
        if self._kemini:
            text, self._last_tool_calls = self._kemini.finish(
                text, self._last_tool_calls, preserve_story_tool=self._story_tool
            )
            rewritten = self._kemini.transported or text != original_text
            self.last_auxiliary = self._kemini.parts
            if self.auxiliary_capture and not self._story_tool:
                self.auxiliary_capture(copy.deepcopy(self.last_auxiliary))
            if self._kemini.transported and finish == "tool_calls":
                finish = "stop"
            if rewritten:
                # Opaque reasoning may contain the old tool/text representation.
                # It remains diagnostic evidence, never an alternate history source.
                self._last_assistant_extra_fields = {}
        canonical = {
            "role": "assistant",
            "content": text,
            "tool_calls": [
                {"id": c.id, "function": {"name": c.name, "arguments": c.arguments}}
                for c in self._last_tool_calls
            ],
        }
        self.last_native_response = (
            None if rewritten else attachment(payload, response_origin(self.settings, self.key), canonical)
        )
        if self.last_native_response is not None and self.request_context is not None:
            self._last_assistant_extra_fields["_native_response"] = copy.deepcopy(self.last_native_response)
        self.last_response_record = {
            "api_mode": api,
            "response_id": payload.get("id"),
            "model": model,
            "headers": dict(self._response_headers),
            "raw_usage": copy.deepcopy(raw),
            "diagnostics": copy.deepcopy(payload.get("diagnostics")),
            "native": copy.deepcopy(self.last_native_response),
            "completion": {"action": self.last_completion.action, "reason": self.last_completion.reason},
        }
        if self._kemini:
            self.last_response_record.update(
                raw_response=copy.deepcopy(payload),
                kemini={
                    "transported": self._kemini.transported,
                    "auxiliary": copy.deepcopy(self.last_auxiliary),
                    "status": "函数参数传输"
                    if self._kemini.transported
                    else "业务工具续答"
                    if self._last_tool_calls
                    else "本轮未使用函数传输"
                    if self._kemini.transport
                    else "普通文本",
                },
            )
        if self.response_capture:
            self.response_capture(copy.deepcopy(self.last_response_record))
        return ChatResponse(content=text, usage=self._last_usage, finish_reason=finish, model=model)

    async def chat_complete_with_preview(
        self, messages, on_text: Callable[[str], None] | None, **kwargs
    ) -> ChatResponse:
        """Preview deltas without losing the authoritative response and metadata."""
        return await self.chat_complete(messages, on_text=on_text, **kwargs)

    async def _complete_chat(self, messages, *, on_text=None, **kwargs):
        if self.settings.model_request_mode == "stream":
            # Complete callers still need the full response (including tool calls),
            # but may receive it incrementally over HTTP before validation.
            async with aclosing(self._consume_stream(messages, **kwargs)) as chunks:
                async for chunk in chunks:
                    if on_text:
                        on_text(chunk)
            return self._completed_response
        return await self._complete_request(messages, **kwargs)

    async def _complete_request(self, messages, **kwargs):
        self._last_tool_calls, self._last_assistant_extra_fields, self._last_usage = [], {}, {}
        self.last_native_response, self.last_response_record, self._response_headers = None, {}, {}
        self.last_completion = None
        tools = kwargs.get("tools") or (kwargs.get("extra_body") or {}).get("tools")
        tools = self._begin_kemini(tools, kwargs)
        if self.settings.api_mode == "gemini":
            async with aclosing(self._gemini_events(messages, tools, False, kwargs)) as events:
                async for payload in events:
                    return self._finish(payload)
        response = await self._send(messages, tools, False, kwargs)
        try:
            return self._finish(response.json())
        finally:
            await response.aclose()

    async def _stream_chat(self, messages, *, tools=None, **kwargs):
        if self.settings.model_request_mode == "non_stream":
            response = await self._complete_request(messages, tools=tools, **kwargs)
            if response.finish_reason in ("length", "content_filter"):
                raise ValueError("模型回复未完整结束，本回合未提交")
            yield response.content
            return
        async with aclosing(self._consume_stream(messages, tools=tools, **kwargs)) as chunks:
            async for chunk in chunks:
                yield chunk

    async def _consume_stream(self, messages, *, tools=None, **kwargs):
        self._completed_response = None
        tools = tools or (kwargs.get("extra_body") or {}).get("tools")
        tools = self._begin_kemini(tools, kwargs)
        self._last_tool_calls, self._last_assistant_extra_fields, self._last_usage = [], {}, {}
        self.last_native_response, self.last_response_record, self._response_headers = None, {}, {}
        self.last_completion = None
        state = StreamState(self.settings.api_mode, secret=self.key)
        if self.settings.api_mode == "gemini":
            async with aclosing(self._gemini_events(messages, tools, True, kwargs)) as events:
                async for event in events:
                    delta = state.feed(event)
                    if self._kemini:
                        delta = self._kemini.feed(event, delta, self.settings.api_mode)
                    if delta:
                        yield delta
                result = self._finish(state.finish())
                if result.finish_reason in ("length", "content_filter"):
                    raise ValueError("模型流未完整结束，本回合未提交")
                self._completed_response = result
                if self._kemini and not self._last_tool_calls and not self._story_tool:
                    yield self._kemini.tail(result.content)
            return
        response = await self._send(messages, tools, True, kwargs)
        try:
            async for event in stream_events(response):
                delta = state.feed(event)
                if self._kemini:
                    delta = self._kemini.feed(event, delta, self.settings.api_mode)
                if delta:
                    yield delta
            result = self._finish(state.finish())
            if result.finish_reason in ("length", "content_filter"):
                raise ValueError("模型流未完整结束，本回合未提交")
            self._completed_response = result
            if self._kemini and not self._last_tool_calls and not self._story_tool:
                yield self._kemini.tail(result.content)
        finally:
            await response.aclose()
