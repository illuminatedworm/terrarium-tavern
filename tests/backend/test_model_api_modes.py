import json

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from openai import AsyncOpenAI, BadRequestError
from pydantic import ValidationError

from tavern.application.api import create_app
from tavern.core.models import Settings
from tavern.scripting.agent import make_provider
from tavern.search.retrieval_config import ModelProfile, Provider


def response(output, *, status="completed"):
    return {"id": "resp_test", "object": "response", "created_at": 0, "status": status,
            "model": "fixture", "output": output,
            "usage": {"input_tokens": 12, "output_tokens": 3, "total_tokens": 15}}


def test_nullable_sampling_and_per_connection_mode():
    profile = ModelProfile(temperature=None, top_p=None, top_k=None,
                           frequency_penalty=None, presence_penalty=None)
    assert profile.model_dump()["temperature"] is None
    config = Settings(providers=[Provider(id="default", api_mode="responses")], foreground=profile)
    assert config.for_role("foreground").api_mode == "responses"
    with pytest.raises(ValidationError):
        ModelProfile(top_p=1.1)
    with pytest.raises(ValidationError):
        ModelProfile(top_k=-1)
    with pytest.raises(ValidationError):
        ModelProfile(prompt_cache_mode="invalid")


@pytest.mark.parametrize("role,temperature,mode", [
    ("foreground", 0.3, "default"),
    ("background", None, "no_write"),
    ("card_editor", 0.6, "instructions"),
])
@pytest.mark.parametrize("api_mode", ["chat_completions", "responses"])
async def test_all_roles_send_their_own_model_parameters(role, temperature, mode, api_mode):
    profiles = {
        "foreground": ModelProfile(model="writer", temperature=0.3, top_p=0.7,
                                   prompt_cache_key="writer", prompt_cache_mode="default"),
        "background": ModelProfile(model="tracker", temperature=None, top_p=0.4,
                                   prompt_cache_key="tracker", prompt_cache_mode="no_write"),
        "card_editor": ModelProfile(model="editor", temperature=0.6, top_p=0.9,
                                    prompt_cache_key="editor", prompt_cache_mode="instructions"),
    }
    settings = Settings(providers=[Provider(id="default", api_mode=api_mode, api_key="test",
                                           use_proxy=False)], **profiles)
    assert settings.for_role("card_editor").model == "editor"
    assert Settings(background=profiles["background"]).for_role("card_editor").model == "tracker"
    config = settings.for_role(role)
    provider = await make_provider(config)
    try:
        assert provider.config.model == config.model
        assert provider.config.temperature == temperature
        assert provider.user_prompt_cache_key == {"foreground": "writer", "background": "tracker",
                                                  "card_editor": "editor"}[role]
        assert provider.prompt_cache_mode == mode
        if api_mode == "chat_completions":
            assert provider.extra_body["top_p"] == config.top_p
            assert (provider.extra_body.get("prompt_cache_options") == {"mode": "explicit"}) is (
                mode != "default")
            return
        request = provider._request([{"role": "system", "content": "stable instructions"},
                                     {"role": "user", "content": "hello"}])
        assert request["model"] == config.model
        assert request.get("temperature") == temperature
        assert request["top_p"] == config.top_p
        assert request["prompt_cache_key"] == provider.user_prompt_cache_key
        assert (request.get("prompt_cache_options") == {"mode": "explicit"}) is (mode != "default")
        if mode == "instructions":
            assert request["input"][0]["content"][0]["prompt_cache_breakpoint"] == {"mode": "explicit"}
        else:
            assert request["input"][0]["content"] == "stable instructions"
    finally:
        await provider.close()


async def test_chat_completions_omits_null_sampling_and_does_not_retry_400():
    settings = Settings(api_key="test", model="fixture", use_proxy=False, temperature=None,
                        top_p=None, top_k=None, frequency_penalty=None, presence_penalty=None)
    provider = await make_provider(settings)
    await provider._client.close()
    requests = []

    def route(request):
        requests.append(json.loads(request.content))
        return httpx.Response(400, json={"error": {
            "message": "Unsupported parameter: 'temperature' is not supported with this model.",
            "type": "invalid_request_error"}})

    provider._client = AsyncOpenAI(api_key="test", max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(route)))
    try:
        with pytest.raises(BadRequestError):
            await provider.chat_complete([{"role": "user", "content": "hello"}])
        assert len(requests) == 1
        assert not {"temperature", "top_p", "top_k", "frequency_penalty", "presence_penalty"} & requests[0].keys()
    finally:
        await provider.close()


async def test_chat_completions_sends_selected_sampling_values():
    provider = await make_provider(Settings(api_key="test", model="fixture", use_proxy=False,
        temperature=0, top_p=0.7, top_k=40, frequency_penalty=-0.5, presence_penalty=0.3,
        prompt_cache_key="story-prefix", prompt_cache_mode="no_write",
        prompt_cache_retention="in_memory"))
    provider.prompt_cache_key = "runtime-key"
    await provider._client.close()
    requests = []

    def route(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "test", "object": "chat.completion", "created": 0,
            "model": "fixture", "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
                                             "finish_reason": "stop"}]})

    provider._client = AsyncOpenAI(api_key="test", max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(route)))
    try:
        assert (await provider.chat_complete([{"role": "user", "content": "hello"}])).content == "ok"
        assert {field: requests[0][field] for field in (
            "temperature", "top_p", "top_k", "frequency_penalty", "presence_penalty")} == {
            "temperature": 0, "top_p": 0.7, "top_k": 40,
            "frequency_penalty": -0.5, "presence_penalty": 0.3}
        assert requests[0]["prompt_cache_key"] == "story-prefix"
        assert requests[0]["prompt_cache_options"] == {"mode": "explicit"}
        assert requests[0]["prompt_cache_retention"] == "in_memory"
    finally:
        await provider.close()


async def test_responses_text_tools_and_sampling_wire():
    settings = Settings(api_key="test", model="fixture", use_proxy=False, api_mode="responses",
                        temperature=None, top_p=0.9, top_k=20, frequency_penalty=0.2,
                        presence_penalty=0.3, reasoning_effort="high",
                        prompt_cache_key="tracker-prefix", prompt_cache_mode="no_write")
    provider = await make_provider(settings)
    provider.prompt_cache_key = "runtime-key"
    await provider._client.close()
    requests = []

    def route(request):
        payload = json.loads(request.content)
        requests.append(payload)
        if len(requests) == 1:
            output = [{"type": "function_call", "id": "fc_1", "call_id": "call_1",
                       "name": "lookup", "arguments": "{}", "status": "completed"}]
        else:
            output = [{"type": "message", "id": "msg_1", "role": "assistant", "status": "completed",
                       "content": [{"type": "output_text", "text": "done", "annotations": []}]}]
        return httpx.Response(200, json=response(output))

    provider._client = AsyncOpenAI(api_key="test", max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(route)))
    tool = {"type": "function", "function": {"name": "lookup", "description": "Lookup",
            "parameters": {"type": "object", "properties": {}}}}
    try:
        first = await provider.chat_complete([{"role": "user", "content": "hello"}],
            extra_body={"tools": [tool], "tool_choice": "auto", "parallel_tool_calls": False})
        assert first.content == ""
        assert [(call.id, call.name) for call in provider.last_tool_calls] == [("call_1", "lookup")]
        second = await provider.chat_complete([
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "call_1", "function": {
                "name": "lookup", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "call_1", "content": "result"},
        ])
        assert second.content == "done"
        assert first.usage == {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15}
        assert requests[0]["store"] is False
        assert "temperature" not in requests[0]
        assert requests[0]["top_p"] == 0.9
        assert not {"top_k", "frequency_penalty", "presence_penalty"} & requests[0].keys()
        assert requests[0]["prompt_cache_key"] == "tracker-prefix"
        assert requests[0]["prompt_cache_options"] == {"mode": "explicit"}
        assert "prompt_cache_retention" not in requests[0]
        assert requests[0]["tools"][0]["name"] == "lookup"
        assert requests[0]["reasoning"] == {"effort": "high"}
        assert requests[0]["include"] == ["reasoning.encrypted_content"]
        assert requests[1]["input"][-2]["type"] == "function_call"
        assert requests[1]["input"][-1] == {"type": "function_call_output", "call_id": "call_1", "output": "result"}
    finally:
        await provider.close()


async def test_responses_stream_emits_text():
    provider = await make_provider(Settings(api_key="test", model="fixture", use_proxy=False,
                                            api_mode="responses", temperature=None))
    sibling = provider.with_model("fixture-alt")
    assert type(sibling) is type(provider)
    assert sibling.config.model == "fixture-alt"
    await provider._client.close()
    final = response([{"type": "message", "id": "msg_1", "role": "assistant", "status": "completed",
                       "content": [{"type": "output_text", "text": "hello", "annotations": []}]}])

    def route(request):
        assert json.loads(request.content)["stream"] is True
        events = [{"type": "response.output_text.delta", "delta": "hello", "item_id": "msg_1",
                   "output_index": 0, "content_index": 0, "sequence_number": 1},
                  {"type": "response.completed", "response": final, "sequence_number": 2}]
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              text="".join(f"data: {json.dumps(event)}\n\n" for event in events))

    provider._client = AsyncOpenAI(api_key="test", max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(route)))
    try:
        assert [part async for part in provider.chat([{"role": "user", "content": "hi"}])] == ["hello"]
        assert provider.last_usage["completion_tokens"] == 3
    finally:
        await provider.close()


async def test_responses_legacy_cache_retention_wire():
    provider = await make_provider(Settings(api_key="test", model="fixture", use_proxy=False,
        api_mode="responses", prompt_cache_retention="24h"))
    try:
        request = provider._request([{"role": "user", "content": "hello"}])
        assert request["prompt_cache_retention"] == "24h"
        assert "prompt_cache_options" not in request
    finally:
        await provider.close()


@pytest.mark.parametrize("api_mode", ["chat_completions", "responses"])
async def test_instruction_breakpoint_marks_only_leading_prefix(api_mode):
    provider = await make_provider(Settings(api_key="test", model="fixture", use_proxy=False,
        api_mode=api_mode, temperature=None, prompt_cache_mode="instructions"))
    await provider._client.close()
    captured = []

    def route(request):
        captured.append(json.loads(request.content))
        if api_mode == "responses":
            output = [{"type": "message", "id": "msg_1", "role": "assistant", "status": "completed",
                       "content": [{"type": "output_text", "text": "ok", "annotations": []}]}]
            return httpx.Response(200, json=response(output))
        return httpx.Response(200, json={"id": "test", "object": "chat.completion", "created": 0,
            "model": "fixture", "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
                                             "finish_reason": "stop"}]})

    provider._client = AsyncOpenAI(api_key="test", max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(route)))
    messages = [{"role": "system", "content": "rules"},
                {"role": "developer", "content": "stable character"},
                {"role": "user", "content": "changing scene"}]
    try:
        assert (await provider.chat_complete(messages)).content == "ok"
        assert messages[1]["content"] == "stable character"
        wire = captured[0]["input" if api_mode == "responses" else "messages"]
        assert captured[0]["prompt_cache_options"] == {"mode": "explicit"}
        assert wire[0]["content"] == "rules"
        assert wire[1]["content"] == [{"type": "input_text" if api_mode == "responses" else "text",
                                        "text": "stable character",
                                        "prompt_cache_breakpoint": {"mode": "explicit"}}]
        assert wire[2]["content"] == "changing scene"
    finally:
        await provider.close()


async def test_responses_stream_preserves_native_tool_calls():
    provider = await make_provider(Settings(api_key="test", model="fixture", use_proxy=False,
                                            api_mode="responses", temperature=None))
    await provider._client.close()
    final = response([{"type": "function_call", "id": "fc_1", "call_id": "call_1",
                       "name": "lookup", "arguments": "{}", "status": "completed"}])

    def route(request):
        event = {"type": "response.completed", "response": final, "sequence_number": 1}
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              text=f"data: {json.dumps(event)}\n\n")

    provider._client = AsyncOpenAI(api_key="test", max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(route)))
    try:
        assert [part async for part in provider.chat([{"role": "user", "content": "hi"}])] == []
        assert [(call.id, call.name) for call in provider.last_tool_calls] == [("call_1", "lookup")]
    finally:
        await provider.close()


async def test_api_saves_null_sampling_and_connection_mode(tmp_path):
    app = create_app(tmp_path)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        settings = (await client.get("/api/settings")).json()
        settings["providers"][0]["api_mode"] = "responses"
        for field in ("temperature", "top_p", "top_k", "frequency_penalty", "presence_penalty"):
            settings["foreground"][field] = None
        settings["background"]["prompt_cache_mode"] = "no_write"
        settings["background"]["prompt_cache_key"] = "tracker"
        settings["background"]["prompt_cache_retention"] = "24h"
        result = await client.put("/api/settings", json=settings)
        assert result.status_code == 200, result.text
        saved = result.json()
        assert saved["providers"][0]["api_mode"] == "responses"
        assert all(saved["foreground"][field] is None for field in
                   ("temperature", "top_p", "top_k", "frequency_penalty", "presence_penalty"))
        assert saved["background"]["prompt_cache_mode"] == "no_write"
        assert saved["background"]["prompt_cache_key"] == "tracker"
        assert saved["background"]["prompt_cache_retention"] == "24h"
