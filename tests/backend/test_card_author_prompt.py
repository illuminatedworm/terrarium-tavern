"""Editable Card Author rules use one saved source across requests, previews and recovery."""
import json
import sqlite3

import pytest
from httpx import ASGITransport, AsyncClient

from tavern.application.api import create_app
from tavern.character.card_author import CardAuthorService
from tavern.character.editor_config import SYSTEM
from tavern.storage.store import Store, dump
from tavern.storage.transfer import ExportSelection, export_package, import_package, read_package
from tests.backend.test_card_author import ToolLLM, import_fixture
from tests.backend.test_card_author_alignment import Editor
from tests.backend.test_card_authoring import Batches, run
from tests.backend.test_half_agent_alignment import blocks


def strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from strings(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from strings(item)


def test_old_configuration_keeps_existing_rules_and_is_backed_up_once(tmp_path):
    store = Store(tmp_path)
    raw = store.settings_document().model_dump()
    raw["shared"]["card_author_config"].pop("system_prompt")
    with store.connection() as db:
        db.execute("UPDATE meta SET value=? WHERE key='settings'", (dump(raw),))
    reopened = Store(tmp_path)
    assert reopened.settings_document().shared.card_author_config.system_prompt == SYSTEM
    backups = list(tmp_path.glob("settings-before-editor-prompt-*.sqlite3"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as db:
        assert json.loads(db.execute("SELECT value FROM meta WHERE key='settings'").fetchone()[0]) == raw
    Store(tmp_path)
    assert len(list(tmp_path.glob("settings-before-editor-prompt-*.sqlite3"))) == 1


async def test_prompt_save_preview_default_and_page_undo_share_one_field(tmp_path):
    app = create_app(tmp_path)
    editor = app.state.editor
    workspace = editor.edits.create_conversation("提示词")
    custom = "自定义制卡规则\n保留 {{user}} 与 {{char}}，不要自动展开。\n完整结尾"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        opened = (await client.post("/api/settings/edit-sessions", json={})).json()
        old = opened["settings"]
        definition = next(p for p in old["prompt_definitions"] if p["path"] == "card_author_config.system_prompt")
        assert definition["default"] == SYSTEM
        saved = await client.patch("/api/settings/shared", json={"expected_revision": old["revisions"]["shared"],
            "edit_session": opened["id"], "changes": {"card_author_config.system_prompt": custom}})
        assert saved.status_code == 200, saved.text
        assert saved.json()["shared"]["advisor"] == old["shared"]["advisor"]
        preview = await client.post(f"/api/card-author/conversations/{workspace['id']}/preview", json={"text": "讨论角色"})
        assert preview.status_code == 200, preview.text
        assert custom in list(strings(preview.json()["messages"]))
        assert SYSTEM not in list(strings(preview.json()["messages"]))
        provider = ToolLLM([])
        assert (await run(editor, workspace["id"], provider))["status"] == "completed"
        assert custom in list(strings(provider.call_log[0]))
        assert Store(tmp_path).settings_document().shared.card_author_config.system_prompt == custom
        undone = await client.post(f"/api/settings/edit-sessions/{opened['id']}/undo", json={
            "page": "shared.editor", "expected_revision": saved.json()["revisions"]["shared"]})
        assert undone.status_code == 200, undone.text
        assert undone.json()["shared"]["card_author_config"]["system_prompt"] == SYSTEM
    await editor.close()
    await app.state.service.close()


@pytest.mark.parametrize("api", ["responses", "anthropic"])
async def test_complete_custom_prompt_reaches_wire_and_rollover_and_changes_next_turn(tmp_path, monkeypatch, api):
    editor = Editor(tmp_path, monkeypatch, api)
    doc = editor.store.settings_document()
    prompt = "自定义规则开始\n" + "保留用户原文与资料。" * 5000 + "\n规则尾部 {{char}} {{user}}"
    doc.shared.card_author.context_tokens = 200000
    doc.shared.card_author_config.system_prompt = prompt
    editor.store.save_settings(doc)
    try:
        await editor.run([editor.tool(1, "new_context", {"content": "已讨论方向，继续任务。"}), blocks(api, "完成")])
        for request in editor.requests:
            body = json.loads(request.content)
            assert prompt in list(strings(body))
            assert SYSTEM not in list(strings(body))
        doc.shared.card_author_config.system_prompt = "更新后的完整系统规则"
        editor.store.save_settings(doc)
        before = len(editor.requests)
        await editor.run([blocks(api, "新的回复")])
        assert "更新后的完整系统规则" in list(strings(json.loads(editor.requests[before].content)))
        assert prompt not in list(strings(json.loads(editor.requests[before].content)))
    finally:
        await editor.service.close()


async def test_empty_prompt_is_not_silently_replaced_by_default(tmp_path):
    store = Store(tmp_path)
    doc = store.settings_document()
    doc.shared.card_author_config.system_prompt = ""
    store.save_settings(doc)
    editor = CardAuthorService(store)
    workspace = editor.edits.create_conversation()
    provider = ToolLLM([("new_context", {"content": "继续本次讨论"})])
    assert (await run(editor, workspace["id"], provider))["status"] == "completed"
    assert SYSTEM not in dump(provider.call_log)
    assert editor.edits.context(workspace["id"])["messages"][0]["content"] == ""
    await editor.close()


async def test_prompt_change_on_resume_preserves_staged_edit_without_repeating_write(tmp_path):
    store = Store(tmp_path)
    editor = CardAuthorService(store)
    card = import_fixture(store)
    workspace = editor.edits.create(card["id"])
    class Interrupted(Batches):
        async def chat(self, messages, **kwargs):
            if self.call_count:
                raise RuntimeError("模拟断线")
            async for chunk in super().chat(messages, **kwargs):
                yield chunk
    first = Interrupted([[("read_field", {"card_ref": "C1", "path": "/description"}),
        ("set_text_field", {"card_ref": "C1", "path": "/description", "text": "待提交的设定", "comment": "用户要求"})]])
    failed = await run(editor, workspace["id"], first)
    assert failed["status"] == "error"
    assert editor.edits.get(workspace["id"])["document"]["description"] == "原人物设定"
    doc = store.settings_document()
    doc.shared.card_author_config.system_prompt = "续接使用的完整新规则"
    store.save_settings(doc)
    second = ToolLLM([])
    result = await run(editor, workspace["id"], second, "", resume=failed["id"])
    assert result["status"] == "completed"
    assert "续接使用的完整新规则" in list(strings(second.call_log[0]))
    assert SYSTEM not in list(strings(second.call_log[0]))
    assert editor.edits.get(workspace["id"])["document"]["description"] == "待提交的设定"
    assert store.card(card["id"])["document"]["description"] == "原人物设定"
    assert not any(e["kind"] == "tool_started" for e in editor.edits.events(result["id"]))
    await editor.close()


def test_prompt_survives_settings_export_and_import(tmp_path):
    source, target = Store(tmp_path / "source"), Store(tmp_path / "target")
    doc = source.settings_document()
    doc.shared.card_author_config.system_prompt = "需要迁移的自定义提示词"
    source.save_settings(doc)
    package, blobs = read_package(export_package(source, ExportSelection(settings=True)), target)
    import_package(target, package, blobs, restore_settings=True, expected_revisions=target.settings_document().revisions)
    assert target.settings_document().shared.card_author_config.system_prompt == "需要迁移的自定义提示词"
