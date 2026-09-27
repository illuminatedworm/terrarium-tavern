"""Stateless Responses API transport for the existing Terrarium chat contract."""

from kohakuterrarium.llm.base import ChatResponse, NativeToolCall
from kohakuterrarium.llm.openai import OpenAIProvider

from tavern.scripting.prompt_cache import mark_instruction_breakpoint


def _input_items(messages, *, session_breakpoint=False):
    items = []
    for message in messages:
        reasoning = message.get("reasoning")
        if isinstance(reasoning, list):
            items.extend(item for item in reasoning if isinstance(item, dict) and item.get("type") == "reasoning")
        role = message.get("role")
        if role == "tool":
            items.append({"type": "function_call_output", "call_id": message["tool_call_id"],
                          "output": message.get("content") or ""})
            continue
        content = message.get("content")
        if content:
            if session_breakpoint and message.get("_cache_breakpoint"):
                content = [{"type": "input_text", "text": content,
                            "prompt_cache_breakpoint": {"mode": "explicit"}}]
            items.append({"role": role, "content": content})
        for call in message.get("tool_calls", []):
            function = call.get("function", {})
            items.append({"type": "function_call", "call_id": call["id"],
                          "name": function.get("name", ""), "arguments": function.get("arguments", "")})
    return items


def _tools(tools):
    result = []
    for tool in tools:
        wire = tool.to_api_format() if hasattr(tool, "to_api_format") else tool
        function = wire.get("function", wire)
        result.append({"type": "function", "name": function["name"],
                       "description": function.get("description", ""),
                       "parameters": function.get("parameters", {"type": "object", "properties": {}})})
    return result


class ResponsesProvider(OpenAIProvider):
    sampling = None
    reasoning_effort = "default"
    prompt_cache_mode = "default"
    prompt_cache_retention = "default"
    user_prompt_cache_key = None
    session_breakpoint = False

    def with_model(self, name):
        clone = super().with_model(name)
        if clone is self:
            return self
        clone.__class__ = ResponsesProvider
        clone.sampling = dict(self.sampling or {})
        clone.reasoning_effort = self.reasoning_effort
        clone.prompt_cache_mode = self.prompt_cache_mode
        clone.prompt_cache_retention = self.prompt_cache_retention
        clone.user_prompt_cache_key = self.user_prompt_cache_key
        clone.session_breakpoint = self.session_breakpoint
        return clone

    def _request(self, messages, *, tools=None, **kwargs):
        extra = kwargs.get("extra_body") or {}
        inputs = _input_items(messages, session_breakpoint=self.session_breakpoint)
        if not self.session_breakpoint and self.prompt_cache_mode == "instructions":
            inputs = mark_instruction_breakpoint(inputs, text_type="input_text")
        request = {"model": kwargs.get("model", self.config.model),
                   "input": inputs, "store": False,
                   "max_output_tokens": kwargs.get("max_tokens", self.config.max_tokens)}
        temperature = kwargs.get("temperature", self.config.temperature)
        if temperature is not None:
            request["temperature"] = temperature
        sampling = self.sampling or {}
        if sampling.get("top_p") is not None:
            request["top_p"] = sampling["top_p"]
        cache_key = self.user_prompt_cache_key or self.prompt_cache_key
        if cache_key:
            request["prompt_cache_key"] = cache_key
        if self.session_breakpoint or self.prompt_cache_mode != "default":
            request["prompt_cache_options"] = {"mode": "explicit"}
        if self.prompt_cache_retention != "default":
            request["prompt_cache_retention"] = self.prompt_cache_retention
        if self.reasoning_effort != "default":
            request["reasoning"] = {"effort": self.reasoning_effort}
            request["include"] = ["reasoning.encrypted_content"]
        selected_tools = tools if tools is not None else extra.get("tools")
        if selected_tools:
            request["tools"] = _tools(selected_tools)
        for key in ("tool_choice", "parallel_tool_calls"):
            if key in extra:
                request[key] = extra[key]
        choice = request.get("tool_choice")
        if isinstance(choice, dict) and choice.get("type") == "function" and "function" in choice:
            request["tool_choice"] = {"type": "function", "name": choice["function"]["name"]}
        return request

    def _finish(self, response):
        if response.status != "completed":
            raise ValueError(f"Responses 请求未完成：{response.error or response.incomplete_details or response.status}")
        self._last_tool_calls = [
            NativeToolCall(id=item.call_id, name=item.name, arguments=item.arguments)
            for item in response.output if item.type == "function_call"
        ]
        reasoning = [item.model_dump(exclude_none=True) for item in response.output
                     if item.type == "reasoning" and getattr(item, "encrypted_content", None)]
        self._last_assistant_extra_fields = {"reasoning": reasoning} if reasoning else {}
        usage = response.usage
        self._last_usage = ({"prompt_tokens": usage.input_tokens,
                             "completion_tokens": usage.output_tokens,
                             "total_tokens": usage.total_tokens} if usage else {})
        return ChatResponse(content=response.output_text or "", finish_reason=response.status,
                            usage=self._last_usage, model=response.model)

    async def _raw_complete_chat(self, messages, **kwargs):
        response = await self._client.responses.create(**self._request(messages, **kwargs))
        return self._finish(response)

    async def _raw_stream_chat(self, messages, *, tools=None, **kwargs):
        self._last_tool_calls = []
        self._last_assistant_extra_fields = {}
        self._last_usage = {}
        stream = await self._client.responses.create(
            **self._request(messages, tools=tools, **kwargs), stream=True)
        completed = False
        async for event in stream:
            if event.type == "response.output_text.delta":
                yield event.delta
            elif event.type == "response.completed":
                self._finish(event.response)
                completed = True
            elif event.type == "response.failed":
                raise ValueError(f"Responses 请求失败：{event.response.error}")
            elif event.type == "response.incomplete":
                raise ValueError(f"Responses 输出未完成：{event.response.incomplete_details}")
            elif event.type == "error":
                raise ValueError(f"Responses 流错误：{event}")
        if not completed:
            raise ValueError("Responses 流未完成")
