"""Conversation environment is frozen at first submit, not at preview or rollover."""
import copy
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from tavern.application.api import create_app
from tavern.character import editor_environment as environment
from tavern.character.card_author import CardAuthorService
from tavern.core.message_adapter import APIS, default_rule
from tavern.core.models import CardAuthorRequest
from tavern.storage.store import Store, dump
from tests.backend.test_card_author import ToolLLM
from tests.backend.test_card_author_alignment import Editor
from tests.backend.test_card_author_prompt import strings
from tests.backend.test_card_authoring import run
from tests.backend.test_half_agent_alignment import blocks

SNAPSHOT = {"system": "Windows 11", "local_time": "2026-10-15 19:30:45", "utc_offset": "UTC+8"}


@pytest.mark.parametrize("system,version,expected", [
    ("Windows", 19045, "Windows 10"), ("Windows", 26100, "Windows 11"),
    ("Darwin", "15.7.1", "macOS 15.7.1"), ("Linux", "Ubuntu 24.04.3 LTS", "Ubuntu 24.04.3 LTS"),
])
def test_system_versions(system, version, expected, monkeypatch):
    monkeypatch.setattr(environment.platform, "system", lambda: system)
    monkeypatch.setattr(environment.sys, "getwindowsversion",
                        lambda: SimpleNamespace(product_type=1, major=10, build=version), raising=False)
    monkeypatch.setattr(environment.platform, "mac_ver", lambda: (version, (), ""))
    monkeypatch.setattr(environment.platform, "freedesktop_os_release", lambda: {"PRETTY_NAME": version})
    assert environment.system_version() == expected


def test_windows_server_is_not_mislabeled_as_windows_11(monkeypatch):
    monkeypatch.setattr(environment.platform, "system", lambda: "Windows")
    monkeypatch.setattr(environment.sys, "getwindowsversion",
                        lambda: SimpleNamespace(product_type=3, major=10, build=26100), raising=False)
    monkeypatch.setattr(environment.platform, "win32_ver", lambda: ("2025Server", "", "", ""))
    assert environment.system_version() == "Windows 2025Server"


@pytest.mark.parametrize("minutes,offset", [(480, "UTC+8"), (345, "UTC+5:45"), (-210, "UTC-3:30"), (0, "UTC+0")])
def test_local_seconds_and_numeric_offset(minutes, offset, monkeypatch):
    local = datetime(2026, 10, 15, 19, 30, 45, tzinfo=timezone(timedelta(minutes=minutes)))
    monkeypatch.setattr(environment, "datetime", SimpleNamespace(
        fromtimestamp=lambda timestamp: SimpleNamespace(astimezone=lambda: local)))
    monkeypatch.setattr(environment, "system_version", lambda: "Example OS")
    assert environment.capture_environment(0) == {
        "system": "Example OS", "local_time": "2026-10-15 19:30:45", "utc_offset": offset}
    for api in APIS:
        assert default_rule("editor_environment", api, "card_author") == {"role": "system", "wrap": False}


async def test_preview_does_not_freeze_and_first_submit_survives_restart_rebuild_and_rollover(tmp_path, monkeypatch):
    app = create_app(tmp_path)
    editor = app.state.editor
    workspace = editor.edits.create_conversation()
    wid = workspace["id"]
    monkeypatch.setattr("tavern.character.editor_storage.capture_environment", lambda timestamp=None: copy.deepcopy(SNAPSHOT))
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
            preview = (await client.post(f"/api/card-author/conversations/{wid}/preview", json={"text": "讨论"})).json()
            assert preview["messages"][1]["role"] == "system"
            assert SNAPSHOT["local_time"] in preview["messages"][1]["content"]
            assert "environment" not in editor.edits.conversation(wid)
            frozen = {**SNAPSHOT, "local_time": "2026-10-15 20:01:02"}
            monkeypatch.setattr("tavern.character.editor_storage.capture_environment", lambda timestamp=None: copy.deepcopy(frozen))
            provider = ToolLLM([("new_context", {"content": "保持本轮方向"})])
            assert (await run(editor, wid, provider))["status"] == "completed"
            assert editor.edits.conversation(wid)["environment"] == frozen
            content = environment.environment_message(frozen)["content"]
            for messages in provider.call_log:
                assert messages[1]["role"] == "system"
                assert messages[1]["content"] == content
                assert sum(m.get("content") == content for m in messages) == 1
                assert next(i for i, m in enumerate(messages) if "当前草稿概览" in m.get("content", "")) > 1

            def unexpected_capture(timestamp=None):
                pytest.fail("A saved conversation must never recapture its environment")
            monkeypatch.setattr("tavern.character.editor_storage.capture_environment", unexpected_capture)
            preview = (await client.post(f"/api/card-author/conversations/{wid}/preview", json={})).json()
            assert preview["messages"][1]["content"] == content
            reopened = CardAuthorService(Store(tmp_path))
            try:
                settings = reopened.store.settings_document()
                settings.shared.card_author_config.system_prompt = "新的系统规则"
                reopened.store.save_settings(settings)
                second = ToolLLM([])
                assert (await run(reopened, wid, second))["status"] == "completed"
                assert second.call_log[0][0]["content"] == "新的系统规则"
                assert second.call_log[0][1]["content"] == content
                assert reopened.edits.conversation(wid)["environment"] == frozen
            finally:
                await reopened.close()
    finally:
        await editor.close()
        await app.state.service.close()


async def test_immediate_cancel_resume_and_discard_do_not_reset_first_submit(tmp_path, monkeypatch):
    editor = CardAuthorService(Store(tmp_path))
    workspace = editor.edits.create_conversation()
    wid = workspace["id"]
    editor.provider_factory = lambda: ToolLLM([])
    monkeypatch.setattr("tavern.character.editor_storage.capture_environment", lambda timestamp=None: copy.deepcopy(SNAPSHOT))
    first = editor.submit(wid, CardAuthorRequest(text="开始", operation_id="first", expected_revision=workspace["revision"]), legacy=False)
    await editor.cancel(first["id"])
    assert editor.edits.conversation(wid)["environment"] == SNAPSHOT
    monkeypatch.setattr("tavern.character.editor_storage.capture_environment", lambda timestamp=None: pytest.fail("must not recapture"))
    resumed = ToolLLM([])
    assert (await run(editor, wid, resumed, "", resume=first["id"]))["status"] == "completed"
    assert resumed.call_log[0][1]["content"] == environment.environment_message(SNAPSHOT)["content"]
    workspace = editor.edits.conversation(wid)
    next_run = editor.submit(wid, CardAuthorRequest(text="继续", operation_id="second", expected_revision=workspace["revision"]), legacy=False)
    await editor.cancel(next_run["id"])
    editor.edits.discard(wid, editor.edits.conversation(wid)["revision"])
    assert editor.edits.conversation(wid)["environment"] == SNAPSHOT
    await editor.close()


@pytest.mark.parametrize("api", ["responses", "anthropic"])
async def test_environment_reaches_final_transport_after_rules_before_references(tmp_path, monkeypatch, api):
    monkeypatch.setattr("tavern.character.editor_storage.capture_environment", lambda timestamp=None: copy.deepcopy(SNAPSHOT))
    editor = Editor(tmp_path, monkeypatch, api)
    content = environment.environment_message(SNAPSHOT)["content"]
    try:
        await editor.run([editor.tool(1, "new_context", {"content": "继续制作"}), blocks(api, "完成")])
        for request in editor.requests:
            body = json.loads(request.content)
            serialized = list(strings(body))
            assert serialized.count(content) == 1
            assert serialized.index(editor.store.settings_document().shared.card_author_config.system_prompt) < serialized.index(content)
            if api == "anthropic":
                system_texts = [part["text"] for part in body["system"]]
                prompt = editor.store.settings_document().shared.card_author_config.system_prompt
                assert system_texts.index(content) == system_texts.index(prompt) + 1
            else:
                assert body["input"][1]["role"] == "system"
                assert content in list(strings(body["input"][1]))
    finally:
        await editor.service.close()


async def test_existing_conversation_recovers_first_request_time_without_rewriting_history(tmp_path):
    editor = CardAuthorService(Store(tmp_path))
    wid = editor.edits.create_conversation()["id"]
    first = await run(editor, wid, ToolLLM([]))
    with editor.store.connection() as db:
        workspace = editor.edits.load(db, wid)
        original = workspace.pop("environment")
        # Reproduce an older saved conversation without an environment field.
        db.execute("UPDATE card_author_conversations SET payload=? WHERE id=?",
                   (dump({"cards": workspace["cards"], "state": workspace["state"]}), wid))
    provider = ToolLLM([])
    assert (await run(editor, wid, provider))["status"] == "completed"
    saved = editor.edits.conversation(wid)["environment"]
    assert saved["local_time"] == original["local_time"]
    assert saved["legacy"] is True
    assert "旧对话补录" in provider.call_log[0][1]["content"]
    assert editor.edits.run(first["id"])["created"] == first["created"]
    await editor.close()
