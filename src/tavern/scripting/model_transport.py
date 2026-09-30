"""Four HTTP protocols behind the same Terrarium chat/tool contract."""
import copy
import json
import uuid

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
from tavern.core.tokens import counting_info, request_tokens
from tavern.scripting.cloud_cache import (
    DISABLED,
    cache_rejection,
    capability_key,
    gemini_object,
    invalidate_object,
)
from tavern.scripting.model_stream import StreamState
from tavern.scripting.protocols import (
    definitions,
    native_messages,
    native_tools,
    openai_messages,
    response_input,
    response_tools,
)
from tavern.scripting.reasoning import anthropic_thinking, gemini_effort, gemini_thinking


class ModelTransport(BaseLLMProvider):
    def __init__(self, settings, key, capture=None, *, http_client=None):
        super().__init__(LLMConfig(model=settings.model, temperature=settings.temperature,
                                   max_tokens=settings.reply_tokens))
        self.settings, self.key, self.capture = settings, key, capture
        self._client = AsyncOpenAI(api_key=key, base_url=settings.base_url, max_retries=0, http_client=http_client)
        self.capability_key = capability_key(settings, key)
        self._last_usage, self._last_assistant_extra_fields = {}, {}
        self.usage_history = []
        self.echo_reasoning = True

    @property
    def http(self):
        return self._client._client

    async def _recover_from_overflow(self, current, state):
        return None

    @property
    def last_usage(self):
        # Terrarium's numeric accounting cannot add None. Preserve the real values
        # separately for the application's usage event and keep raw evidence.
        return {**self._last_usage, "cached_tokens": self._last_usage.get("cached_tokens") or 0,
                "cache_write_tokens": self._last_usage.get("cache_write_tokens") or 0}

    @property
    def reported_usage(self):
        result = {key: sum(u[key] for u in self.usage_history) for key in
                  ("prompt_tokens", "completion_tokens", "total_tokens")}
        for key in ("cached_tokens", "cache_write_tokens"):
            result[key] = (sum(u[key] for u in self.usage_history)
                           if self.usage_history and all(u[key] is not None for u in self.usage_history) else None)
        result["raw_usage"] = [u["raw_usage"] for u in self.usage_history]
        return result

    @property
    def gemini_headers(self):
        return {"x-goog-api-key": self.key}

    def with_model(self, name):
        if not name or name == self.config.model:
            return self
        clone = copy.copy(self)
        clone.settings = self.settings.model_copy(deep=True, update={"model": name})
        clone.config = self.config.model_copy(update={"model": name}) if hasattr(self.config, "model_copy") else copy.copy(self.config)
        clone.config.model = name
        clone.capability_key = capability_key(clone.settings, self.key)
        clone._last_tool_calls, clone._last_usage, clone._last_assistant_extra_fields, clone.usage_history = [], {}, {}, []
        return clone

    async def close(self):
        await self._client.close()

    def _request(self, messages, *, tools=None, **kwargs):
        selected = preset(self.settings)
        enabled = self.settings.cache.mode == "runtime" and any(m.get("_cache_breakpoint") for m in messages)
        marker = selected if enabled and selected["kind"] == "block" else None
        tools = tools or (kwargs.get("extra_body") or {}).get("tools")
        return self._body(messages, tools, False, marker,
                          selected["request"] if marker or selected["kind"] == "implicit" else {}, kwargs)

    def _body(self, messages, tools, stream, marker, params, kwargs):
        cfg, api = self.settings, self.settings.api_mode
        maximum = kwargs.get("max_tokens", self.config.max_tokens)
        temperature = kwargs.get("temperature", self.config.temperature)
        extra = kwargs.get("extra_body") or {}
        choice = extra.get("tool_choice", "auto")
        if api == "chat_completions":
            body = {"model": cfg.model, "messages": openai_messages(messages, marker), "max_tokens": maximum}
            if tools:
                body.update(tools=definitions(tools), tool_choice=choice,
                            parallel_tool_calls=extra.get("parallel_tool_calls", False))
            for field in ("top_p", "top_k", "frequency_penalty", "presence_penalty"):
                if getattr(cfg, field) is not None:
                    body[field] = getattr(cfg, field)
            if cfg.reasoning_effort != "default":
                effort = gemini_effort(cfg.model, cfg.reasoning_effort)
                body.update({"reasoning_effort": effort} if cfg.reasoning_format == "reasoning_effort"
                            else {"reasoning": {"effort": effort}})
            if stream:
                body.update(stream=True, stream_options={"include_usage": True})
        elif api == "responses":
            body = {"model": cfg.model, "input": response_input(messages, marker), "store": False,
                    "max_output_tokens": maximum}
            if tools:
                if isinstance(choice, dict) and choice.get("type") == "function":
                    choice = {"type": "function", "name": choice.get("function", choice)["name"]}
                body.update(tools=response_tools(tools), tool_choice=choice,
                            parallel_tool_calls=extra.get("parallel_tool_calls", False))
            if cfg.top_p is not None:
                body["top_p"] = cfg.top_p
            if cfg.reasoning_effort != "default":
                body.update(reasoning={"effort": cfg.reasoning_effort}, include=["reasoning.encrypted_content"])
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
                    selection = {"type": "auto", "disable_parallel_tool_use": True}
                    selected_tools = tools
                    if isinstance(choice, dict):
                        name = choice.get("function", choice)["name"]
                        if not policy.forced_tools:
                            # Full Agent still enforces new_context during forced
                            # rollover. Expose only the requested tool, using the
                            # auto mode accepted by the model's native API.
                            selected_tools = [t for t in definitions(tools) if t.get("function", t)["name"] == name]
                            if not selected_tools:
                                raise ValueError("指定工具不在当前允许的工具列表中")
                        else:
                            selection = {"type": "tool", "name": name, "disable_parallel_tool_use": True}
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
                if cfg.top_p is not None:
                    generation["topP"] = cfg.top_p
                if cfg.top_k is not None:
                    generation["topK"] = cfg.top_k
                thinking = gemini_thinking(cfg.model, cfg.reasoning_effort)
                if thinking:
                    generation["thinkingConfig"] = thinking
                body = {"systemInstruction": {"parts": system}, "contents": contents, "generationConfig": generation}
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
        return body

    async def _prepare(self, messages, tools, stream, kwargs, *, fallback=None):
        cfg, api = self.settings, self.settings.api_mode
        if cfg.cache.mode == "runtime" and not any(m.get("_cache_breakpoint") for m in messages):
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
        planned = next((i for i, m in enumerate(messages) if m.get("_cache_breakpoint")), None)
        diagnostic = {"mode": cfg.cache.mode, "status": "cloud", "planned_index": planned,
                      "sent_index": None, "reason": fallback, "kind": selected["kind"]}
        if planned is not None:
            diagnostic.update(source=messages[planned].get("_cache_source"),
                              message_id=messages[planned].get("_cache_message_id"))
        marker, params, resource = None, (retention_request(cfg) if cfg.cache.mode == "cloud" and not fallback else {}), None
        diagnostic["retention"] = effective_retention(cfg)[1]
        if cfg.cache.mode == "runtime":
            reason = fallback or DISABLED.get(self.capability_key)
            if not reason and planned is None and selected["kind"] != "implicit":
                reason = "没有可用的固定前缀断点"
            if not reason and selected["kind"] == "unknown":
                reason = "模型没有已匹配的缓存参数，请手动配置"
            if not reason and selected["kind"] == "gemini":
                if api not in ("gemini", "chat_completions"):
                    reason = "当前协议不支持 Gemini 缓存对象引用"
                else:
                    resource, reason = await gemini_object(self, messages, tools,
                        (kwargs.get("extra_body") or {}).get("tool_choice", "auto"))
            if reason:
                diagnostic.update(status="fallback", reason=reason)
            else:
                diagnostic.update(status="sent", sent_index=planned)
                params = selected["request"]
                if selected["kind"] == "block":
                    marker = selected
                elif selected["kind"] == "implicit":
                    diagnostic.update(status="cloud", sent_index=None, reason="该模型由服务端管理缓存前缀，仅发送时效参数")
        body = self._body(messages, tools, stream, marker, params, kwargs)
        if resource:
            if api == "gemini":
                body["contents"] = body["contents"][resource["boundary"]:]
                for field in ("systemInstruction", "tools", "toolConfig"):
                    body.pop(field, None)
                body["cachedContent"] = resource["name"]
            else:
                # Google-compatible Chat can reference an object created through the native endpoint.
                suffix = copy.deepcopy(messages[planned + 1:])
                for message in suffix:
                    if message["role"] in ("system", "developer"):
                        message["role"] = "user"
                        message["content"] = "<tavern_runtime_context>\n" + str(message.get("content", "")) + "\n</tavern_runtime_context>"
                body["messages"] = openai_messages(suffix)
                body.pop("tools", None)
                body.pop("tool_choice", None)
                body.pop("parallel_tool_calls", None)
                body["extra_body"] = {"google": {"cached_content": resource["name"]}}
            diagnostic["resource"] = resource["name"]
        diagnostic["parameters"] = ({marker["field"]: marker["value"], **params} if marker else params)
        diagnostic["prefix_sha256"] = fingerprint(messages[:planned + 1]) if planned is not None else None
        return body, diagnostic, resource

    def _capture(self, messages, tools, body, diagnostic, url):
        if not self.capture:
            return
        cfg = self.settings
        wire_messages = body.get("messages", body.get("input", body.get("contents", [])))
        cost = request_tokens(openai_messages(messages), cfg, definitions(tools))
        payload = {"model": cfg.model, "api_mode": cfg.api_mode, "url": url,
                      "messages": wire_messages, "wire": copy.deepcopy(body), "cache": diagnostic,
                      "tools": [t.get("function", t).get("name") for t in definitions(tools)],
                      "tools_sha256": fingerprint(definitions(tools)),
                      "reasoning": cfg.reasoning_effort, "estimated_input_tokens": cost,
                      "context_warning": "估算超出模型上下文，完整发送" if cost + cfg.reply_tokens + 2048 > cfg.context_tokens else None,
                      "token_counting": counting_info(cfg)}
        serialized = json.dumps(payload, ensure_ascii=False)
        if len(self.key) >= 8:
            serialized = serialized.replace(self.key, "[REDACTED]")
        self.capture(json.loads(serialized))

    async def _send(self, messages, tools, stream, kwargs):
        cfg, api = self.settings, self.settings.api_mode
        url = endpoint(cfg.base_url, api, cfg.model, stream=stream)
        headers = ({"x-api-key": self.key, "anthropic-version": "2023-06-01"} if api == "anthropic" else
                   self.gemini_headers if api == "gemini" else {"Authorization": "Bearer " + self.key})
        fallback = None
        for attempt in range(2):
            body, diagnostic, resource = await self._prepare(messages, tools, stream, kwargs, fallback=fallback)
            self._capture(messages, tools, body, diagnostic, url)
            request = self.http.build_request("POST", url, headers=headers, json=body)
            response = await self.http.send(request, stream=stream)
            if response.status_code < 400:
                return response
            await response.aread()
            try:
                payload = response.json()
            except ValueError:
                payload = {}
            selected = preset(cfg)
            fields = [selected.get("field", "cachedContent"), *selected["request"]]
            retry = (attempt == 0 and diagnostic["status"] == "sent"
                     and cache_rejection(response.status_code, payload, fields, resource=bool(resource)))
            if not retry:
                await response.aclose()
                response.raise_for_status()
            await response.aclose()
            fallback = "服务商拒绝缓存参数或缓存对象已过期；已完整发送上下文"
            if resource:
                await invalidate_object(self, resource)
            if not resource or response.status_code != 404:
                DISABLED[self.capability_key] = fallback
        raise ValueError("缓存降级请求失败")

    def _finish(self, payload):
        api = self.settings.api_mode
        self._last_tool_calls, self._last_assistant_extra_fields = [], {}
        model = payload.get("model", payload.get("modelVersion")) or self.settings.model
        finish, text, raw = "stop", "", payload.get("usage") or {}
        if api == "chat_completions":
            choice = (payload.get("choices") or [{}])[0]
            message = choice.get("message") or {}
            text, finish = message.get("content") or "", choice.get("finish_reason")
            self._last_tool_calls = [NativeToolCall(id=c["id"], name=c["function"]["name"],
                arguments=c["function"]["arguments"]) for c in message.get("tool_calls") or []]
            self._last_assistant_extra_fields = {k: message[k] for k in ("reasoning_content", "reasoning_details", "reasoning") if k in message}
        elif api == "responses":
            if payload.get("status") != "completed":
                raise ValueError("Responses 请求未完整结束，本回合未提交")
            reasoning = []
            for item in payload.get("output", []):
                if item["type"] == "function_call":
                    self._last_tool_calls.append(NativeToolCall(id=item["call_id"], name=item["name"], arguments=item["arguments"]))
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
                    self._last_tool_calls.append(NativeToolCall(id=part["id"], name=part["name"],
                                                               arguments=json.dumps(part["input"], ensure_ascii=False)))
                elif part["type"] in ("thinking", "redacted_thinking"):
                    thinking.append(copy.deepcopy(part))
            if thinking:
                self._last_assistant_extra_fields = {"reasoning_details": [{"provider": api, "thinking": thinking}]}
            finish = {"max_tokens": "length", "refusal": "content_filter"}.get(payload.get("stop_reason"), "stop")
            if not payload.get("stop_reason"):
                raise ValueError("Anthropic 响应未完整结束")
        else:
            raw = payload.get("usageMetadata") or {}
            candidate = (payload.get("candidates") or [{}])[0]
            parts = candidate.get("content", {}).get("parts", [])
            for part in parts:
                if "functionCall" in part:
                    call = part["functionCall"]
                    self._last_tool_calls.append(NativeToolCall(id=call.get("id") or "gemini_" + uuid.uuid4().hex,
                        name=call["name"], arguments=json.dumps(call.get("args", {}), ensure_ascii=False)))
                elif not part.get("thought"):
                    text += part.get("text", "")
            self._last_assistant_extra_fields = {"reasoning_details": [{"provider": api, "parts": copy.deepcopy(parts)}]}
            reason = candidate.get("finishReason")
            finish = "stop" if reason == "STOP" else "length" if reason == "MAX_TOKENS" else "content_filter"
            if not reason:
                raise ValueError("Gemini 响应未完整结束")
        self._last_usage = normalize_usage(raw, api)
        self.usage_history.append(copy.deepcopy(self._last_usage))
        return ChatResponse(content=text, usage=self._last_usage, finish_reason=finish, model=model)

    async def _complete_chat(self, messages, **kwargs):
        self._last_tool_calls, self._last_assistant_extra_fields, self._last_usage = [], {}, {}
        tools = kwargs.get("tools") or (kwargs.get("extra_body") or {}).get("tools")
        response = await self._send(messages, tools, False, kwargs)
        try:
            return self._finish(response.json())
        finally:
            await response.aclose()

    async def _stream_chat(self, messages, *, tools=None, **kwargs):
        tools = tools or (kwargs.get("extra_body") or {}).get("tools")
        self._last_tool_calls, self._last_assistant_extra_fields, self._last_usage = [], {}, {}
        response = await self._send(messages, tools, True, kwargs)
        state = StreamState(self.settings.api_mode)
        try:
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if not data or data == "[DONE]":
                    continue
                event = json.loads(data)
                delta = state.feed(event)
                if delta:
                    yield delta
            result = self._finish(state.finish())
            if result.finish_reason in ("length", "content_filter"):
                raise ValueError("模型流未完整结束，本回合未提交")
        finally:
            await response.aclose()
