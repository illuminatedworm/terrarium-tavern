"""Images reach each HTTP protocol unchanged, including the separate subscription route."""
import base64
import copy
import json

import httpx
import pytest
from google.genai import types as genai_types

from tavern.core.message_adapter import adapt_messages, runtime_messages
from tavern.core.models import Settings
from tavern.core.provider import make_provider
from tavern.core.tokens import request_tokens
from tavern.scripting.model_transport import ModelTransport
from tests.backend.chatgpt_fixtures import IdentityFixture, seed_account
from tests.backend.test_chatgpt_provider import configuration
from tests.backend.test_half_agent_alignment import sse
from tests.backend.test_unified_cache import reply

IMAGES = [("image/png", base64.b64encode(b"first-image-bytes").decode()),
          ("image/webp", base64.b64encode(b"second-image-bytes").decode())]
TOOLS = [{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object", "properties": {}}}}]


def messages():
    content = [{"type": "text", "text": "第一张图片"},
               {"type": "image_url", "image_url": {"url": f"data:{IMAGES[0][0]};base64,{IMAGES[0][1]}"}},
               {"type": "text", "text": "第二张图片"},
               {"type": "image_url", "image_url": {"url": f"data:{IMAGES[1][0]};base64,{IMAGES[1][1]}"}}]
    return runtime_messages([
        {"role": "system", "content": "固定规则"}, {"role": "user", "content": content},
        {"role": "assistant", "content": "读取资料", "tool_calls": [
            {"id": "call1", "type": "function", "function": {"name": "lookup", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "call1", "content": '{"result":"完整资料尾部"}'},
    ], "card_author")


def response(api, stream):
    payload = reply(api)
    if not stream:
        return httpx.Response(200, json=payload)
    if api in ("anthropic", "responses"):
        return sse(api, payload)
    events = ([{"choices": [{"delta": {"content": "OK"}, "finish_reason": "stop"}], "usage": payload["usage"]}]
              if api == "chat_completions" else [payload])
    data = "".join("data: " + json.dumps(event) + "\n\n" for event in events)
    if api == "chat_completions":
        data += "data: [DONE]\n\n"
    return httpx.Response(200, content=data, headers={"content-type": "text/event-stream"})


def assert_images(body, api, expected=IMAGES):
    key = {"responses": "input", "gemini": "contents"}.get(api, "messages")
    found, text = [], []
    for message in body[key]:
        content = message.get("parts", message.get("content", []))
        if not isinstance(content, list):
            continue
        for part in content:
            if "text" in part:
                text.append(part["text"])
            if part.get("type") == "image_url":
                header, data = part["image_url"]["url"].split(",", 1)
                found.append((header[5:].split(";")[0], data))
            elif part.get("type") == "input_image":
                header, data = part["image_url"].split(",", 1)
                found.append((header[5:].split(";")[0], data))
            elif part.get("type") == "image":
                assert part["source"]["type"] == "base64"
                found.append((part["source"]["media_type"], part["source"]["data"]))
            elif "inlineData" in part:
                blob = genai_types.Blob.model_validate(part["inlineData"])
                found.append((blob.mime_type, base64.b64encode(blob.data).decode()))
    assert found == expected
    assert all(data not in "".join(text) for _, data in expected)
    return body[key]


@pytest.mark.parametrize("api", ["chat_completions", "responses", "anthropic", "gemini"])
@pytest.mark.parametrize("mode", ["stream", "non_stream"])
async def test_real_wire_preserves_images_and_tool_history(api, mode):
    cfg = Settings(api_mode=api, model="fixture", api_key="fixture-key", base_url="https://fixture.test",
                   use_proxy=False, model_request_mode=mode, cache={"mode": "cloud"})
    cfg._adapter_lane = "card_author"
    wire = []

    def route(request):
        wire.append(json.loads(request.content))
        return response(api, mode == "stream")

    provider = ModelTransport(cfg, cfg.api_key, http_client=httpx.AsyncClient(transport=httpx.MockTransport(route)))
    original = messages()
    before = copy.deepcopy(original)
    try:
        assert (await provider.chat_complete(original, tools=TOOLS)).content == "OK"
        rows = assert_images(wire[0], api)
        serialized = json.dumps(rows, ensure_ascii=False)
        assert "完整资料尾部" in serialized and "lookup" in serialized
        assert "_editor_image_refs" not in serialized and "runtime_content" not in serialized
        assert original == before
        if api == "responses":
            assert [p["type"] for p in rows[1]["content"]] == ["input_text", "input_image", "input_text", "input_image"]
        if api == "gemini":
            assert rows[0]["role"] == "user"
    finally:
        await provider.close()


@pytest.mark.parametrize("mode", ["stream", "non_stream"])
async def test_subscription_has_its_own_image_wire_and_retains_namespace(tmp_path, mode):
    accounts = IdentityFixture().service(tmp_path)
    account_id, account = seed_account(accounts)
    cfg = configuration(accounts, account_id, account)
    cfg.model_request_mode = mode
    cfg._adapter_lane = "card_author"
    requests = []

    def route(request):
        requests.append(request)
        return response("responses", True)

    accounts.http_transport = httpx.MockTransport(route)
    provider = await make_provider(cfg)
    try:
        assert (await provider.chat_complete(messages(), tools=TOOLS)).content == "OK"
        body = json.loads(requests[0].content)
        rows = assert_images(body, "responses")
        assert rows[0]["role"] == "developer"
        assert body["stream"] is True
        assert body["tools"][0]["type"] == "namespace"
        assert body["tools"][0]["name"] == "tavern"
        assert all(item["namespace"] == "tavern" for item in rows if item.get("type") in ("function_call", "function_call_output"))
        assert not any(key in body for key in ("max_output_tokens", "temperature", "prompt_cache_breakpoint"))
    finally:
        await provider.close()


def test_multimodal_wrapping_is_lossless_and_base64_is_not_text_tokens():
    cfg = Settings(message_adapter={"mappings": {"responses": {"custom": {"role": "user", "wrap": True}}}}, api_mode="responses")
    original = messages()[1]
    original["message_type"] = "custom"
    adapted = adapt_messages([original], cfg)[0][0]
    assert adapted["content"][1:-1] == original["content"]
    assert adapted["content"][0]["text"] == "<tavern_custom_context>\n"
    baseline = request_tokens([original], cfg)
    larger = copy.deepcopy(original)
    larger["content"][1]["image_url"]["url"] += "long-base64-data" * 10000
    assert request_tokens([larger], cfg) == baseline


@pytest.mark.parametrize("api", ["responses", "anthropic", "gemini"])
async def test_unknown_nontext_blocks_are_rejected_instead_of_dropped(api):
    cfg = Settings(api_mode=api, model="fixture", api_key="fixture", use_proxy=False)
    def no_request(request):
        raise AssertionError("unsupported content must fail before HTTP")

    provider = ModelTransport(cfg, cfg.api_key, http_client=httpx.AsyncClient(transport=httpx.MockTransport(no_request)))
    try:
        with pytest.raises(ValueError, match="不能丢弃非文本"):
            provider._request([{"role": "user", "content": [{"type": "unhandled_media", "data": "must-not-disappear"}]}])
    finally:
        await provider.close()
