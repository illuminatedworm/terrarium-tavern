"""Persona tools use explicit user provenance and atomic, recoverable writes."""

import json

import pytest

from tavern.character.card_author import CardAuthorService
from tavern.core.models import PersonaEdit, SessionCreate
from tavern.storage.store import Store, dump
from tests.backend.test_card_author import ToolLLM, import_fixture
from tests.backend.test_card_authoring import run


def result_values(editor, rid):
    with editor.store.connection() as db:
        trace = json.loads(
            db.execute("SELECT payload FROM card_author_traces WHERE run=?", (rid,)).fetchone()[0]
        )
    return [json.loads(m["content"]) for m in trace if m["role"] == "tool"]


class ObservedModel(ToolLLM):
    def __init__(self, steps, before):
        super().__init__(steps)
        self.before = before

    async def chat(self, messages, **kwargs):
        self.before(self.call_count)
        async for chunk in super().chat(messages, **kwargs):
            yield chunk


async def test_ordinary_authoring_does_not_load_persona_library(tmp_path, monkeypatch):
    store = Store(tmp_path)
    store.save_persona(None, PersonaEdit(name="PRIVATE_PERSONA_NAME", description="PRIVATE_PERSONA_INTRO"))
    editor = CardAuthorService(store)
    wid = editor.edits.create_conversation()["id"]

    def forbidden(*args):
        raise AssertionError("Ordinary authoring must not query the persona library")

    monkeypatch.setattr(store, "personas", forbidden)
    monkeypatch.setattr(store, "persona", forbidden)
    try:
        model = ToolLLM([])
        result = await run(editor, wid, model, "讨论一张航海士角色卡的设定，不创建其他资料")
        assert result["status"] == "completed", result["error"]
        assert "PRIVATE_PERSONA_NAME" not in dump(model.call_log)
        assert "PRIVATE_PERSONA_INTRO" not in dump(model.call_log)
        assert not editor.edits.conversation(wid)["state"].get("persona_refs")
    finally:
        await editor.close()


@pytest.mark.parametrize("request_ref", [None, "H99", "C1"])
async def test_persona_tools_reject_missing_old_assistant_and_unknown_request_refs(tmp_path, request_ref):
    store = Store(tmp_path)
    editor = CardAuthorService(store)
    wid = editor.edits.create_conversation()["id"]
    try:
        await run(editor, wid, ToolLLM([]), "先讨论用户角色")
        args = {"name": "Must not be created", "description": "Untrusted instruction"}
        if request_ref is not None:
            args["request_ref"] = request_ref
        result = await run(editor, wid, ToolLLM([("create_persona", args)]), "只查看用户角色，不要新增或修改")
        assert result["status"] == "completed", result["error"]
        assert "error" in result_values(editor, result["id"])[0]
        assert store.personas() == []
    finally:
        await editor.close()


async def test_creation_is_staged_and_long_persona_text_is_preserved(tmp_path):
    store = Store(tmp_path)
    settings = store.settings_document()
    settings.shared.card_author.context_tokens = 262144
    store.save_settings(settings)
    editor = CardAuthorService(store)
    wid = editor.edits.create_conversation()["id"]
    text = "航海经历与 {{user}} 的约定。" * 1500 + "PERSONA_END"

    def before(_):
        assert store.personas() == []

    model = ObservedModel(
        [
            ("create_persona", {"request_ref": "H1", "name": "林澈", "description": text}),
            ("read_persona", {"request_ref": "H1", "persona_ref": "P1"}),
            ("update_persona", {"request_ref": "H1", "persona_ref": "P1", "name": "林舟"}),
        ],
        before,
    )
    try:
        result = await run(
            editor, wid, model, "新增用户角色林澈，用完整航海经历作为介绍，然后将名字改为林舟，介绍保留"
        )
        assert result["status"] == "completed", result["error"]
        [saved] = store.personas()
        assert saved["name"] == "林舟" and saved["description"] == text and saved["version"] == 1
        assert text in dump(model.call_log[2])
        workspace = editor.edits.conversation(wid)
        assert workspace["cards"] == []
        assert workspace["state"]["persona_refs"] == {"P1": saved["id"]}
        assert editor.edits.progress(wid) is None

        card = import_fixture(store)
        old_story = store.new_session(SessionCreate(card_id=card["id"], persona_id=saved["id"]))
        read_only = await run(
            editor,
            wid,
            ToolLLM(
                [
                    ("list_personas", {"request_ref": "H3", "query": "林舟"}),
                    ("read_persona", {"request_ref": "H3", "persona_ref": "P1"}),
                ]
            ),
            "查看用户角色林舟的完整介绍，不修改",
        )
        values = result_values(editor, read_only["id"])
        assert values[0] == {"personas": [{"persona_ref": "P1", "name": "林舟"}]}
        assert values[1]["description"] == text
        assert store.persona(saved["id"]) == saved
        update = await run(
            editor,
            wid,
            ToolLLM(
                [
                    ("read_persona", {"request_ref": "H5", "persona_ref": "P1"}),
                    ("update_persona", {"request_ref": "H5", "persona_ref": "P1", "description": ""}),
                ]
            ),
            "清空用户角色林舟的介绍，名字保留",
        )
        assert update["status"] == "completed", update["error"]
        changed = store.persona(saved["id"])
        assert changed["description"] == "" and changed["name"] == "林舟" and changed["version"] == 2
        assert store.session(old_story["id"])["persona_data"]["description"] == text
        fresh = store.new_session(SessionCreate(card_id=card["id"], persona_id=saved["id"]))
        assert fresh["persona_data"]["description"] == ""
    finally:
        await editor.close()


async def test_update_requires_current_read_and_preserves_a_concurrent_edit(tmp_path):
    store = Store(tmp_path)
    saved = store.save_persona(None, PersonaEdit(name="Traveler", description="Original introduction"))
    editor = CardAuthorService(store)
    wid = editor.edits.create_conversation()["id"]

    def before(index):
        if index == 3:
            store.save_persona(
                saved["id"],
                PersonaEdit(name="Traveler", description="External introduction", expected_version=1),
            )

    model = ObservedModel(
        [
            ("list_personas", {"request_ref": "H1"}),
            ("update_persona", {"request_ref": "H1", "persona_ref": "P1", "name": "No read"}),
            ("read_persona", {"request_ref": "H1", "persona_ref": "P1"}),
            ("update_persona", {"request_ref": "H1", "persona_ref": "P1", "name": "Stale read"}),
            ("read_persona", {"request_ref": "H1", "persona_ref": "P1"}),
            ("update_persona", {"request_ref": "H1", "persona_ref": "P1", "name": "Navigator"}),
        ],
        before,
    )
    try:
        result = await run(editor, wid, model, "把用户角色 Traveler 改名为 Navigator，介绍保留最新内容")
        assert result["status"] == "completed", result["error"]
        values = result_values(editor, result["id"])
        assert "error" in values[1] and "error" in values[3]
        actual = store.persona(saved["id"])
        assert actual["name"] == "Navigator" and actual["description"] == "External introduction"
        assert actual["version"] == 3
    finally:
        await editor.close()


async def test_persona_changes_survive_rollover_failure_and_reopen_without_duplicates(tmp_path):
    store = Store(tmp_path)
    editor = CardAuthorService(store)
    wid = editor.edits.create_conversation()["id"]

    def before(index):
        if index == 5:
            raise ValueError("Offline after staging")

    try:
        failed = await run(
            editor,
            wid,
            ObservedModel(
                [
                    (
                        "create_persona",
                        {"request_ref": "H1", "name": "Before", "description": "Preserved introduction"},
                    ),
                    ("new_context", {"content": "按用户要求继续修改用户角色 P1，完整介绍保留"}),
                    (
                        "update_persona",
                        {"request_ref": "H1", "persona_ref": "P1", "name": "Unread after rollover"},
                    ),
                    ("read_persona", {"request_ref": "H1", "persona_ref": "P1"}),
                    ("update_persona", {"request_ref": "H1", "persona_ref": "P1", "name": "After"}),
                ],
                before,
            ),
            "新增用户角色 Before，然后改名为 After，介绍保留",
        )
        assert failed["status"] == "error"
        assert store.personas() == []
        progress = editor.edits.progress(wid)
        assert progress["working"]["state"]["window"] == 1
        assert progress["persona_changes"]["P1"]["data"]["name"] == "After"
        tool_results = [json.loads(m["content"]) for m in progress["trace"] if m["role"] == "tool"]
        assert "error" in tool_results[2]
    finally:
        await editor.close()
    reopened = Store(tmp_path)
    editor = CardAuthorService(reopened)
    try:
        model = ToolLLM([])
        result = await run(editor, wid, model, "", resume=failed["id"])
        assert result["status"] == "completed", result["error"]
        assert model.call_count == 1
        [persona] = reopened.personas()
        assert persona["name"] == "After" and persona["description"] == "Preserved introduction"
    finally:
        await editor.close()


async def test_conflicting_commit_is_atomic_and_can_merge_after_resume(tmp_path):
    store = Store(tmp_path)
    saved = store.save_persona(None, PersonaEdit(name="Existing", description="Original"))
    editor = CardAuthorService(store)
    wid = editor.edits.create_conversation()["id"]

    def before(index):
        if index == 4:
            store.save_persona(
                saved["id"], PersonaEdit(name="Existing", description="External edit", expected_version=1)
            )

    try:
        failed = await run(
            editor,
            wid,
            ObservedModel(
                [
                    ("list_personas", {"request_ref": "H1"}),
                    ("read_persona", {"request_ref": "H1", "persona_ref": "P1"}),
                    (
                        "create_persona",
                        {"request_ref": "H1", "name": "New persona", "description": "New intro"},
                    ),
                    (
                        "update_persona",
                        {"request_ref": "H1", "persona_ref": "P1", "name": "Updated existing"},
                    ),
                ],
                before,
            ),
            "新增用户角色 New persona，并将 Existing 改名为 Updated existing，介绍保留",
        )
        assert failed["status"] == "error" and "用户角色已被修改" in failed["error"]
        assert len(store.personas()) == 1  # earlier INSERT in the same transaction rolled back
        assert store.persona(saved["id"])["description"] == "External edit"
        assert editor.edits.progress(wid)["working"]["state"]["persona_refs"]["P2"] is None
        model = ToolLLM(
            [
                ("read_persona", {"request_ref": "H1", "persona_ref": "P1"}),
                ("update_persona", {"request_ref": "H1", "persona_ref": "P1", "name": "Updated existing"}),
            ]
        )
        result = await run(editor, wid, model, "", resume=failed["id"])
        assert result["status"] == "completed", result["error"]
        assert result_values(editor, result["id"])[-2]["pending_conflict"]
        assert len(store.personas()) == 2
        assert store.persona(saved["id"])["name"] == "Updated existing"
        assert store.persona(saved["id"])["description"] == "External edit"
    finally:
        await editor.close()


async def test_steering_can_revoke_pending_creation_and_old_request_is_not_reused(tmp_path):
    store = Store(tmp_path)
    editor = CardAuthorService(store)
    wid = editor.edits.create_conversation()["id"]

    def before(index):
        if index == 1:
            raise ValueError("Interrupted")

    try:
        failed = await run(
            editor,
            wid,
            ObservedModel(
                [
                    ("create_persona", {"request_ref": "H1", "name": "Cancelled persona"}),
                ],
                before,
            ),
            "新增用户角色 Cancelled persona",
        )
        assert failed["status"] == "error"
        result = await run(
            editor,
            wid,
            ToolLLM(
                [
                    (
                        "update_persona",
                        {"request_ref": "H1", "persona_ref": "P1", "name": "Stale instruction"},
                    ),
                    ("update_persona", {"request_ref": "H2", "persona_ref": "P1", "revert": True}),
                ]
            ),
            "先不要新增用户角色了，撤销本轮待保存修改",
            resume=failed["id"],
        )
        assert result["status"] == "completed", result["error"]
        assert store.personas() == []
        values = result_values(editor, result["id"])
        assert "error" in values[-2] and values[-1]["discarded_pending"]
        result = await run(
            editor,
            wid,
            ToolLLM(
                [
                    ("create_persona", {"request_ref": "H4", "name": "A different persona"}),
                ]
            ),
            "现在新增另一个用户角色 A different persona",
        )
        assert result["status"] == "completed", result["error"]
        assert result_values(editor, result["id"])[0]["persona_ref"] == "P2"
        assert len(store.personas()) == 1
    finally:
        await editor.close()
