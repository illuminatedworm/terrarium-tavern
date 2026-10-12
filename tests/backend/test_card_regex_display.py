"""The published authoring recipe must produce a real, state-bound frontend."""

import copy
import json
import re
from pathlib import Path

import pytest

from tavern.character.card_editor import CardEditorService
from tavern.character.card_workflow import validation_report
from tavern.character.editor_sandbox import test_card as probe_card
from tavern.storage.store import Store, dump
from tests.backend.test_card_authoring import run
from tests.backend.test_card_editor import ToolLLM

GUIDE = Path(__file__).parents[2] / "src/tavern/skills/adapt-card-to-tavern/references/regex-display.md"


def recipe():
    return [json.loads(block) for block in re.findall(r"```json\n([\s\S]*?)\n```", GUIDE.read_text(encoding="utf-8"))]


def document():
    rule, _ = recipe()
    return {"name": "正则前端夹具", "first_mes": "客人来到柜台。", "alternate_greetings": [],
            "character_book": {"entries": [{"comment": "[InitVar]", "enabled": False, "content": "Money: 0"}]},
            "extensions": {"regex_scripts": [rule], "unknown": {"preserve": True}}}


async def test_published_recipe_is_available_written_and_tested_through_editor_tools(tmp_path):
    store = Store(tmp_path)
    original = document()
    original["extensions"]["regex_scripts"] = []
    source = store.import_card(dump(original).encode(), "fixture.json")
    editor = CardEditorService(store)
    workspace = editor.edits.create(source["id"])
    rule, probe = recipe()
    provider = ToolLLM([
        ("read_skill", {"resource": "references/regex-display.md"}),
        ("read_field", {"card_ref": "C1", "path": "/extensions"}),
        ("patch_draft", {"card_ref": "C1", "comment": "保留原状态界面并绑定 MVU", "operations": [
            {"op": "add", "path": "/extensions/regex_scripts/-", "value": rule}]}),
        ("test_draft", probe),
    ])
    try:
        result = await run(editor, workspace["id"], provider)
        assert result["status"] == "completed", result["error"]
        outputs = [json.loads(m["content"]) for m in provider.call_log[-1] if m.get("role") == "tool"]
        assert outputs[0]["text"] == GUIDE.read_text(encoding="utf-8")
        observed = outputs[-1]
        assert observed["status"] == "passed", observed
        displays = [c for c in observed["checks"] if c["check"] in ("display", "display_after_patch")]
        assert len(displays) == 4 and all(c["status"] == "passed" for c in displays)
        assert {c["assertions"][0]["actual"] for c in displays} == {"0", "12"}
        for check in observed["checks"]:
            if check["check"] == "regex":
                for channel in ("input_model", "output_model", "input_display"):
                    assert check["observed"][channel] == check["source"]
                assert check["observed"]["output_display"].startswith(check["source"])
        draft = editor.edits.conversation(workspace["id"])["cards"][0]["document"]
        assert draft["extensions"]["regex_scripts"] == [rule]
        assert draft["extensions"]["unknown"] == original["extensions"]["unknown"]
        assert store.card(source["id"])["document"] == original
        assert not store.sessions()
    finally:
        await editor.close()


@pytest.mark.parametrize("fault", ["missing", "placeholder", "wrong_channel", "wrong_path", "static", "syntax"])
async def test_frontend_failures_cannot_pass_as_successful_migration(tmp_path, fault):
    doc = document()
    rule = doc["extensions"]["regex_scripts"][0]
    if fault == "missing":
        doc["extensions"]["regex_scripts"] = []
    elif fault == "placeholder":
        rule["findRegex"] = "/<StatusPlaceHolderImpl\\/>/g"
    elif fault == "wrong_channel":
        rule.update(markdownOnly=False, promptOnly=True)
    elif fault == "wrong_path":
        rule["replaceString"] = rule["replaceString"].replace(".stat_data.Money", ".Money")
    elif fault == "static":
        rule["replaceString"] = '<div id="money">0</div>'
    else:
        rule["replaceString"] += '<script>throw new Error("DISPLAY_ERROR")</script>'
    original = copy.deepcopy(doc)
    _, args = recipe()
    args.pop("card_ref")
    result = await probe_card(Store(tmp_path), {"ref": "C1", "document": doc}, **args)
    assert result["status"] == "failed", result
    assert doc == original
    if fault == "static":
        assert all(c["status"] == "passed" for c in result["checks"] if c["check"] == "display")
        assert any(c["status"] == "failed" for c in result["checks"] if c["check"] == "display_after_patch")


async def test_no_assertions_explicitly_leave_display_untested(tmp_path):
    doc = document()
    doc["extensions"]["regex_scripts"] = []
    result = await probe_card(Store(tmp_path), {"ref": "C1", "document": doc})
    assert result["status"] == "passed"
    assert result["checks"][1]["status"] == "skipped"
    assert any("display_assertions" in scope for scope in result["untested"])


async def test_display_document_ready_and_literal_markup_values(tmp_path):
    doc = document()
    doc["character_book"]["entries"][0]["content"] = "Money: '0'"
    rule = doc["extensions"]["regex_scripts"][0]
    rule["replaceString"] = rule["replaceString"].replace(
        "<script>", "<script>document.addEventListener('DOMContentLoaded', () => {").replace("</script>", "});</script>")
    value = '</script><div id="injected">文本</div>'
    result = await probe_card(Store(tmp_path), {"ref": "C1", "document": doc},
                              samples=['```\n普通叙事代码块\n```\n客人进门。\n'],
                              display_assertions=[{"sample_index": 1, "selector": "#money", "text": "0", "after_text": value}],
                              state_patch=[{"op": "replace", "path": "/Money", "value": value}])
    assert result["status"] == "passed", result
    assert result["checks"][-1]["assertions"][0]["actual"] == value


@pytest.mark.parametrize("rules", [None, {}, [None], [{"regex": "x", "replacement": "y"}],
                                  [{"findRegex": "x", "replaceString": "<script>1</script>", "placement": 2}],
                                  [{"findRegex": "x", "replaceString": "<script>1</script>", "placement": ["2"], "disabled": "false"}]])
def test_regex_diagnostics_explain_ignored_or_invalid_fields_without_mutating(rules):
    doc = document()
    doc["extensions"]["regex_scripts"] = rules
    original = copy.deepcopy(doc)
    result = validation_report(doc)
    assert result["valid"] and result["warnings"] and not result["adapted"]
    assert doc == original
