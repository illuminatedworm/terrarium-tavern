"""Keep hidden opening data while testing the real initialization/context pipeline."""

import copy
import json

import pytest

from tavern.application.service import RoleplayRuntime
from tavern.character.card_editor import CardEditorService
from tavern.character.card_workflow import greeting_hash, validation_report
from tavern.conversation.context import compile_context
from tavern.core.models import SessionCreate
from tavern.full_agent.context import compile_window
from tavern.storage.store import Store, dump
from tests.backend.test_card_authoring import run
from tests.backend.test_card_editor import ToolLLM


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
    editor = CardEditorService(store)
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


async def test_explicit_extraction_of_one_opening_preserves_other_inline_initialization(tmp_path):
    store = Store(tmp_path)
    doc = fixture()
    source = store.import_card(dump(doc).encode(), "fixture.json")
    editor = CardEditorService(store)
    wid = editor.edits.create(source["id"])["id"]
    try:
        provider = ToolLLM([
            ("read_field", {"card_ref": "C1", "path": "/first_mes"}),
            ("set_opening", {"card_ref": "C1", "index": 0, "narrative": "清晨开门。",
                             "initialization": "<initvar>hp: 8\nplace: morning</initvar>"}),
            ("test_draft", {"card_ref": "C1", "index": 1}),
        ])
        result = await run(editor, wid, provider)
        assert result["status"] == "completed", result["error"]
        current = editor.edits.conversation(wid)["cards"][0]["document"]
        assert current["alternate_greetings"] == doc["alternate_greetings"]
        records = current["extensions"]["terrarium"]["workflow"]["greetings"]
        assert records[1]["initialization"] == "<initvar>hp: 3\nplace: night</initvar>"
        probe = json.loads([m for m in provider.call_log[-1] if m.get("role") == "tool"][-1]["content"])
        assert probe["status"] == "passed", probe
        assert probe["checks"][0]["state"]["stat_data"] == {"hp": 3, "place": "night"}
    finally:
        await editor.close()


@pytest.mark.parametrize("problem,warning", [
    ("wrong_audience", "tracker_mvu"), ("unclosed", "标签未成对"),
    ("legacy_update", "不会自动执行"), ("metadata_override", "空串也会覆盖"),
])
def test_ambiguous_sources_are_reported_without_deleting_data(problem, warning):
    doc = fixture()
    if problem == "wrong_audience":
        doc["character_book"]["entries"][0]["extensions"] = {"terrarium": {"audience": "tracker_mvu"}}
    elif problem == "unclosed":
        doc["first_mes"] = doc["first_mes"].replace("</initvar>", "")
    elif problem == "legacy_update":
        doc["first_mes"] = "开场。<UpdateVariable>_.set('hp', 8);</UpdateVariable>"
    else:
        doc["extensions"]["terrarium"] = {"workflow": {"version": 1, "greetings": [
            {"id": str(i), "initialization": "", "greeting_hash": greeting_hash(text)}
            for i, text in enumerate([doc["first_mes"], *doc["alternate_greetings"]])], "unresolved": []}}
    original = copy.deepcopy(doc)
    result = validation_report(doc)
    assert result["valid"] and any(warning in text for text in result["warnings"])
    assert doc == original
