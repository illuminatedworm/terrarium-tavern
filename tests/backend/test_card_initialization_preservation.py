"""Keep hidden opening data while testing the real initialization/context pipeline."""

import copy

import pytest

from tavern.application.service import RoleplayRuntime
from tavern.character.card_author import CardAuthorService
from tavern.character.card_workflow import validation_report
from tavern.conversation.context import compile_context
from tavern.core.models import SessionCreate
from tavern.full_agent.context import compile_window
from tavern.storage.store import Store, dump
from tests.backend.test_card_author import ToolLLM
from tests.backend.test_card_authoring import run


def fixture():
    return {"name": "初值保留", "first_mes": '清晨开门。\n<UpdateVariable><initvar>hp: 8\nplace: morning</initvar></UpdateVariable>',
            "alternate_greetings": ['夜间开门。\n<initvar>hp: 3\nplace: night</initvar>', '普通开场。'],
            "character_book": {"entries": [{"comment": "[InitVar] 默认初值", "enabled": False,
                                             "content": '<initvar>hp: 100\nplace: default\nextra: true</initvar>'}]},
            "extensions": {"regex_scripts": [{
                "scriptName": "隐藏开场变量", "findRegex": r"/<(initvar|UpdateVariable)\b[^>]*>[\s\S]*?<\/\1\s*>/gi",
                "replaceString": "", "placement": [2], "markdownOnly": True, "promptOnly": True,
            }]}}


async def test_inline_overrides_survive_editor_and_are_hidden_from_both_model_contexts(tmp_path):
    store = Store(tmp_path)
    original = fixture()
    card = store.import_card(dump(original).encode(), "fixture.json")
    editor = CardAuthorService(store)
    service = RoleplayRuntime(store)
    wid = editor.edits.create(card["id"])["id"]
    try:
        provider = ToolLLM([
            ("read_skill", {"resource": "references/workflow.md"}),
            ("read_field", {"card_ref": "C1", "path": "/first_mes"}),
            ("set_opening", {"card_ref": "C1", "index": 0,
                             "narrative": original["first_mes"].replace("清晨开门。", "清晨，店门打开。")}),
            ("validate_draft", {"card_ref": "C1"}),
        ])
        result = await run(editor, wid, provider)
        assert result["status"] == "completed", result["error"]
        doc = editor.edits.conversation(wid)["cards"][0]["document"]
        assert "workflow" not in doc["extensions"].get("terrarium", {})
        assert doc["extensions"] == original["extensions"]
        assert doc["alternate_greetings"] == original["alternate_greetings"]
        assert doc["first_mes"].split("\n", 1)[1] == original["first_mes"].split("\n", 1)[1]
        assert validation_report(doc)["warnings"] == []
        imported = store.import_card(dump(doc).encode(), "edited.json")
        openings = [doc["first_mes"], *doc["alternate_greetings"]]
        expected = [{"hp": 8, "place": "morning"}, {"hp": 3, "place": "night"},
                    {"hp": 100, "place": "default", "extra": True}]
        for index, initial in enumerate(expected):
            session = store.new_session(SessionCreate(card_id=imported["id"], greeting=index))
            session = await service.initialize_session(session["id"])
            assert session["snapshot"]["state"]["stat_data"] == initial
            message = session["messages"][0]
            assert message["raw"] == openings[index]
            assert message["model"].strip() == openings[index].split("\n")[0]
            assert message["display"] == message["model"]
            for compiled in (
                compile_context(doc, session, session["messages"], session["snapshot"], "继续", store.settings(), "test"),
                compile_window(doc, session, session["messages"], session["snapshot"], "继续", store.settings(), []),
            ):
                wire = dump(compiled["messages"])
                assert "<initvar>" not in wire.lower() and "<updatevariable>" not in wire.lower()
                assert "hp: 100" not in wire and message["model"].strip() in wire
        assert store.card(card["id"])["document"] == original
    finally:
        await editor.close()
        await service.close()


@pytest.mark.parametrize("problem,warning", [
    ("unclosed", "标签未成对"),
    ("legacy_update", "不会自动执行"),
])
def test_ambiguous_sources_are_reported_without_deleting_data(problem, warning):
    doc = fixture()
    if problem == "unclosed":
        doc["first_mes"] = doc["first_mes"].replace("</initvar>", "")
    elif problem == "legacy_update":
        doc["first_mes"] = "开场。<UpdateVariable>_.set('hp', 8);</UpdateVariable>"
    original = copy.deepcopy(doc)
    result = validation_report(doc)
    assert result["valid"] and any(warning in text for text in result["warnings"])
    assert doc == original


@pytest.mark.parametrize("mode", ["half_agent", "full_agent"])
async def test_new_story_uses_saved_worldbook_money_without_resetting_existing_story(tmp_path, mode):
    store = Store(tmp_path)
    doc = {"name": "初值更新", "first_mes": "开门。", "extensions": {},
           "character_book": {"entries": [{"comment": "[InitVar]", "enabled": False, "content": "Money: 0"}]}}
    card = store.import_card(dump(doc).encode(), "card.json")
    service = RoleplayRuntime(store)
    try:
        previous = store.new_session(SessionCreate(card_id=card["id"], agent_mode=mode))
        previous = await service.initialize_session(previous["id"])
        assert previous["snapshot"]["state"]["stat_data"]["Money"] == 0
        doc["character_book"]["entries"][0]["content"] = "Money: 500000"
        store.edit_card(card["id"], doc, card["revision"])
        current = store.new_session(SessionCreate(card_id=card["id"], agent_mode=mode))
        current = await service.initialize_session(current["id"])
        assert current["snapshot"]["state"]["stat_data"]["Money"] == 500000
        assert store.session(previous["id"])["snapshot"]["state"]["stat_data"]["Money"] == 0
        assert current["card_data"]["document"]["extensions"] == {}
    finally:
        await service.close()


def test_removed_initialization_channel_is_rejected_in_tools_and_card_import(tmp_path):
    from pydantic import ValidationError

    from tavern.character.editor_tools import Opening
    assert "initialization" not in Opening.model_json_schema()["properties"]
    with pytest.raises(ValidationError, match="Extra inputs"):
        Opening(card_ref="C1", index=0, narrative="开门。", initialization="<initvar>Money: 0</initvar>")
    doc = fixture()
    doc["extensions"]["terrarium"] = {"workflow": {"version": 1, "greetings": [
        {"id": "old", "initialization": "<initvar>Money: 0</initvar>", "greeting_hash": "obsolete"}]}}
    report = validation_report(doc)
    assert not report["valid"] and "已移除" in report["errors"][0]
    store = Store(tmp_path)
    with pytest.raises(ValueError, match="已移除"):
        store.import_card(dump(doc).encode(), "old.json")
    assert store.cards() == []
