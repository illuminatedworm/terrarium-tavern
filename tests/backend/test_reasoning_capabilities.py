"""Advertised ordinary API tiers must compile into the matching wire protocol."""
import json

import httpx
import pytest
from google.genai import types as genai_types

from tavern.application.api import create_app
from tavern.scripting.reasoning import anthropic_thinking, gemini_thinking, reasoning_capabilities
from tests.backend.test_unified_cache import config, provider, reply


@pytest.mark.parametrize("api,model,levels", [
    ("chat_completions", "relay-alias", ["none", "minimal", "low", "medium", "high", "xhigh", "max"]),
    ("responses", "gemini-3.8-flash", ["none", "minimal", "low", "medium", "high", "xhigh", "max"]),
    ("anthropic", "claude-opus-4-6", ["none", "low", "medium", "high", "max"]),
    ("anthropic", "claude-opus-5-5", ["low", "medium", "high", "xhigh", "max"]),
    ("anthropic", "claude-sonnet-4-5", ["none"]),
    ("gemini", "gemini-3.8-flash-high", ["low", "medium", "high"]),
    ("gemini", "gemini-3.1-flash-lite-image", ["minimal", "high"]),
    ("gemini", "gemini-2.5-flash-lite", ["none", "low", "medium", "high", "xhigh"]),
    ("chat_completions", "gemini-2.5-flash", ["none", "low", "medium", "high"]),
])
async def test_advertised_tiers_compile_without_aliasing(api, model, levels):
    details = reasoning_capabilities(api, model)
    assert [item["effort"] for item in details["supported_reasoning_levels"]] == levels
    cfg = config(api)
    cfg.model = model
    for effort in ["default", *levels]:
        cfg.reasoning_effort = effort

        def route(request):
            body = json.loads(request.content)
            if effort == "default":
                assert not any(key in body for key in ("thinking", "output_config", "reasoning", "reasoning_effort"))
                assert "thinkingConfig" not in body.get("generationConfig", {})
            elif api == "responses":
                assert body["reasoning"] == {"effort": effort}
            elif api == "chat_completions":
                assert body["reasoning_effort"] == effort
            elif api == "anthropic":
                if effort == "none":
                    assert body["thinking"] == {"type": "disabled"}
                else:
                    assert body["output_config"] == {"effort": effort}
            else:
                thinking = genai_types.ThinkingConfig.model_validate(
                    body["generationConfig"]["thinkingConfig"]).model_dump(by_alias=True, exclude_none=True)
                if "thinkingLevel" in thinking:
                    assert thinking["thinkingLevel"] == effort.upper()
                else:
                    expected = {"none": 0, "minimal": 128, "low": 1024, "medium": 4096,
                                "high": 8192, "xhigh": 16384}
                    assert thinking["thinkingBudget"] == expected[effort]
            return httpx.Response(200, json=reply(api))

        async with provider(cfg, route) as transport:
            await transport.chat_complete([{"role": "user", "content": "hello"}])


@pytest.mark.parametrize("model,effort", [
    ("gemini-3.8-flash", "minimal"), ("gemini-2.5-flash", "max"),
])
def test_invalid_native_gemini_tiers_report_actionable_errors(model, effort):
    with pytest.raises(ValueError, match="请选择默认或其他思考档位"):
        gemini_thinking(model, effort)


def test_saved_legacy_aliases_still_compile():
    assert anthropic_thinking("claude-opus-4-6", "xhigh")[1]["output_config"] == {"effort": "max"}
    assert anthropic_thinking("claude-opus-4-5", "minimal")[1]["output_config"] == {"effort": "low"}
    assert gemini_thinking("gemini-3.8-flash", "xhigh")["thinkingLevel"] == "HIGH"


async def test_capabilities_endpoint_is_local_and_does_not_write_settings(tmp_path, monkeypatch):
    app = create_app(tmp_path)
    before = app.state.store.settings_document()

    async def no_provider_request(*args, **kwargs):
        pytest.fail("local capabilities must not contact a model provider")

    monkeypatch.setattr(httpx.AsyncClient, "send", no_provider_request)
    # ASGITransport is invoked directly because provider HTTP is forbidden.
    async with httpx.ASGITransport(app=app) as transport:
        request = httpx.Request("GET", "http://127.0.0.1/api/settings/reasoning?api_mode=gemini&model=gemini-3.8-flash")
        response = await transport.handle_async_request(request)
        await response.aread()
    assert response.status_code == 200
    assert [item["effort"] for item in response.json()["supported_reasoning_levels"]] == ["low", "medium", "high"]
    assert app.state.store.settings_document() == before
