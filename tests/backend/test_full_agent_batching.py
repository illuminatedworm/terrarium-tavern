"""Batch policy from saved settings through model messages, wire and draft execution."""

import json

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from tavern.application.api import create_app
from tavern.core.settings import SettingsDocument, migrate_settings
from tavern.scripting.model_transport import ModelTransport
from tavern.storage.store import Store
from tests.backend import test_full_agent
from tests.backend.test_full_agent import current_text, handoff, native_reply, runtime, story_reply, turn

story = test_full_agent.story


async def test_batch_setting_defaults_migrates_and_persists_both_values(tmp_path):
    old = SettingsDocument().model_dump()
    old["full_agent"]["tools"].pop("parallel_tool_calls")
    assert migrate_settings(old).full_agent.tools.parallel_tool_calls is True
    app = create_app(tmp_path)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        before = (await client.get("/api/settings")).json()
        assert before["full_agent"]["tools"]["parallel_tool_calls"] is True
        assert before["full_agent"]["tools"]["max_duration_seconds"] == 900
        changed = await client.patch(
            "/api/settings/full_agent",
            json={
                "expected_revision": before["revisions"]["full_agent"],
                "changes": {"tools.parallel_tool_calls": False, "tools.max_duration_seconds": 600},
            },
        )
        assert changed.status_code == 200, changed.text
        after = changed.json()
        assert after["full_agent"]["tools"]["parallel_tool_calls"] is False
        assert after["half_agent"] == before["half_agent"]
        assert after["shared"] == before["shared"]
        reopened = Store(tmp_path)
        assert reopened.settings_document().full_agent.tools.parallel_tool_calls is False
        assert reopened.settings("full_agent").full_agent_config.parallel_tool_calls is False
        assert reopened.settings("full_agent").full_agent_config.max_duration_seconds == 600
        invalid = await client.patch(
            "/api/settings/full_agent",
            json={
                "expected_revision": after["revisions"]["full_agent"],
                "changes": {"tools.parallel_tool_calls": "false"},
            },
        )
        assert invalid.status_code == 400
        enabled = await client.patch(
            "/api/settings/full_agent",
            json={
                "expected_revision": after["revisions"]["full_agent"],
                "changes": {"tools.parallel_tool_calls": True},
            },
        )
        assert enabled.status_code == 200, enabled.text
        assert Store(tmp_path).settings("full_agent").full_agent_config.parallel_tool_calls is True


@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.parametrize("api_mode", ["chat_completions", "responses", "anthropic", "gemini"])
async def test_batch_policy_over_real_wire_and_complete_result_roundtrip(story, api_mode, enabled):
    store, session = story
    config = store.settings_document()
    config.full_agent.tools.parallel_tool_calls = enabled
    config.full_agent.model.api_mode = api_mode
    config.full_agent.model.model = "fixture"
    # The handoff schema makes this prefix long enough for Gemini explicit caching;
    # this test covers tool batching, not cache creation requests.
    config.full_agent.model.cache.mode = "cloud"
    config.shared.providers[0].api_key = "fixture"
    store.save_settings(config)
    settings = store.settings("full_agent")
    service = runtime(store)
    requests = []
    actions = [
        ("write_handoff", handoff("Keep the harbor appointment.", through=None)),
        ("write_note", {"key": "appointment", "content": "Visit harbor", "kind": "plan"}),
    ]

    def transport(request):
        payload = json.loads(request.content)
        requests.append(payload)
        first = len(requests) == 1
        if first:
            result = native_reply(
                api_mode, [(f"call_{i}", name, args) for i, (name, args) in enumerate(actions)]
            )
        elif len(requests) == 2:
            result = story_reply(api_mode, "Narrative")
        else:
            result = native_reply(api_mode, [])
        if api_mode == "gemini" and first:
            result["candidates"][0]["content"]["parts"][0]["thoughtSignature"] = "Zml4dHVyZS1zaWduYXR1cmU="
        return httpx.Response(200, json=result)

    provider = ModelTransport(
        settings,
        "fixture",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(transport), trust_env=False),
    )
    try:
        result, current = await turn(service, session, provider)
        assert result["status"] == "committed", result["error"]
        assert len(requests) == (3 if api_mode in ("responses", "gemini") else 2)
        if api_mode in ("chat_completions", "responses"):
            assert requests[0]["parallel_tool_calls"] is enabled
            if api_mode == "chat_completions":
                results = [m for m in requests[1]["messages"] if m["role"] == "tool"]
                assert [r["tool_call_id"] for r in results] == ["call_0", "call_1"]
                envelopes = [json.loads(r["content"]) for r in results]
            else:
                results = [m for m in requests[1]["input"] if m.get("type") == "function_call_output"]
                assert [r["call_id"] for r in results] == ["call_0", "call_1"]
                envelopes = [json.loads(r["output"]) for r in results]
        elif api_mode == "anthropic":
            assert requests[0]["tool_choice"]["disable_parallel_tool_use"] is (not enabled)
            results = [
                m
                for m in requests[1]["messages"]
                if any(p.get("type") == "tool_result" for p in m["content"])
            ]
            assert len(results) == 1 and results[0]["role"] == "user"
            assert [p["tool_use_id"] for p in results[0]["content"]] == ["call_0", "call_1"]
            assert all(p.get("is_error", False) is (not enabled) for p in results[0]["content"])
            envelopes = [json.loads(p["content"]) for p in results[0]["content"]]
        else:
            # GenerateContent has no parallel toggle: never send invented fields.
            assert requests[0]["toolConfig"]["functionCallingConfig"] == {"mode": "AUTO"}
            result_messages = [
                m for m in requests[1]["contents"] if any("functionResponse" in p for p in m["parts"])
            ]
            assert len(result_messages) == 1 and result_messages[0]["role"] == "user"
            parts = [p for m in requests[1]["contents"] for p in m["parts"]]
            results = [p["functionResponse"] for p in parts if "functionResponse" in p]
            assert [r["id"] for r in results] == ["call_0", "call_1"]
            assert (
                next(p for p in parts if "functionCall" in p)["thoughtSignature"]
                == "Zml4dHVyZS1zaWduYXR1cmU="
            )
            envelopes = [r["response"] for r in results]
        assert len(envelopes) == 2 and all(r["ok"] is enabled for r in envelopes)
        assert "_tool_error" not in json.dumps(requests)
        if api_mode != "anthropic":
            assert "is_error" not in json.dumps(requests)
        state = current["snapshot"]["full_agent"]
        if enabled:
            assert state["handoff"]["current"][0]["text"] == actions[0][1]["current"][0]["text"]
            assert state["notes"]["appointment"]["content"] == "Visit harbor"
        else:
            assert all(r["error"]["code"] == "PARALLEL_TOOL_CALLS_DISABLED" for r in envelopes)
            assert not current_text(state) and not state["notes"]
    finally:
        await provider.close()
        await service.close()
