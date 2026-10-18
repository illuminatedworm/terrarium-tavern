"""Status-display migration diagnostics and the real editor/runtime tool path."""

import copy
import json
import re
from pathlib import Path

import pytest
from kohakuterrarium.testing.llm import ScriptedLLM
from pydantic import ValidationError

from tavern.application.service import RoleplayRuntime
from tavern.character.card_author import CardAuthorService
from tavern.character.card_workflow import validation_report
from tavern.character.editor_config import SYSTEM
from tavern.character.editor_sandbox import test_card as probe_card
from tavern.character.editor_tools import Skill, tool_definitions
from tavern.conversation.context import compile_context
from tavern.core.models import SessionCreate
from tavern.full_agent.context import compile_window
from tavern.scripting.variables import analyze_variables
from tavern.storage.store import Store, dump
from tests.backend.test_card_author import ToolLLM
from tests.backend.test_card_authoring import run

TEMPLATE = '<details><summary>状态栏</summary><div>TIME: {{getvar::Time}} MONEY: {{getvar::Money}}</div></details>'
OPENING = '旅客放下行李。\n<details><summary>状态栏</summary><div>TIME: 08:20 MONEY: 12</div></details>'
RULE = '营业收入仅在正文明确到账后增加；报价不代表到账。'


def card():
    return {"name": "测试旅店", "description": "玩家经营旅店，模型扮演来客。", "first_mes": OPENING,
            "alternate_greetings": [], "extensions": {"unknown": "preserved"},
            "character_book": {"entries": [
                {"content": "每次回复结尾必须生成以下内容：\n" + TEMPLATE, "constant": True},
                {"content": "旅店在清晨开门。", "constant": True},
            ]}}


@pytest.mark.parametrize("wrapped,object_entries", [(False, False), (True, True)])
def test_legacy_status_card_reports_locations_and_missing_contract_without_mutation(wrapped, object_entries):
    document = card()
    if object_entries:
        document["character_book"]["entries"] = {"4af8b4de-116f-4b40-8822-e4e79096b43f": document["character_book"]["entries"][0]}
    if wrapped:
        document = {"spec": "chara_card_v2", "spec_version": "2.0", "data": document}
    original = copy.deepcopy(document)
    report = validation_report(document)
    assert report["valid"] and not report["adapted"]
    warnings = "\n".join(report["warnings"])
    assert all(text in warnings for text in ("first_mes", "世界书第 1 条", "逐轮", "根路径", "初始化", "tracker_mvu"))
    assert "4af8b4de" not in warnings and TEMPLATE not in warnings
    assert document == original


@pytest.mark.parametrize("field,text", [
    ("system_prompt", 'Every reply must append this status block: <status>HP: 8</status>'),
    ("post_history_instructions", '每轮回答必须输出状态栏\n| 项目 | 数值 |\n| --- | --- |\n| HP | 8 |'),
    ("scenario", '每轮回复显示状态面板'),
    ("alternate_greetings", OPENING),
])
def test_model_fields_examples_and_alternate_openings_are_checked(field, text):
    document = {"name": "定位", field: [text] if field == "alternate_greetings" else text}
    report = validation_report(document)
    assert report["valid"] and not report["adapted"]
    assert any(field in warning for warning in report["warnings"])


@pytest.mark.parametrize("text", [
    '<div style="color:cyan">欢迎来到旅店。</div>',
    '不要在每次回复结尾生成状态栏。',
    "Never append a status panel after every reply.",
    '<initvar>hp: 8</initvar><StatusPlaceHolderImpl/>',
    '<div>状态栏</div><script>const v = getMvuData();</script>',
])
def test_layout_prohibitions_and_existing_mvu_displays_are_not_migration_demands(text):
    report = validation_report({"name": "保留", "first_mes": text})
    assert report["valid"] and report["warnings"] == []


def test_negation_does_not_hide_later_requirement_and_relabeling_template_is_not_a_fix():
    document = card()
    entry = document["character_book"]["entries"][0]
    entry["content"] = "不要重复叙事。每次回复必须输出状态栏。\n" + TEMPLATE
    entry["extensions"] = {"terrarium": {"audience": "tracker_mvu"}}
    assert any("世界书第 1 条：可能要求" in w for w in validation_report(document)["warnings"])
    entry["enabled"] = False
    document["first_mes"] = "纯叙事"
    assert validation_report(document)["warnings"] == []


def test_initialization_hints_follow_disabled_init_and_inline_openings():
    document = card()
    entries = document["character_book"]["entries"]
    entries.append({"comment": "[InitVar]", "enabled": False, "content": "Time: '08:20'\nMoney: 12"})
    entries.append({"content": RULE, "extensions": {"terrarium": {"audience": "tracker_mvu"}}})
    assert not any("未发现" in w for w in validation_report(document)["warnings"])
    entries[-2]["comment"] = "普通初值"
    assert any("初始化来源" in w for w in validation_report(document)["warnings"])
    document["alternate_greetings"] = [OPENING]
    document["first_mes"] += "\n<initvar>Money: 12</initvar>"
    missing = next(w for w in validation_report(document)["warnings"] if "初始化来源" in w)
    assert "alternate_greetings[0]" in missing and "first_mes" not in missing


def test_correct_state_macros_do_not_require_rewriting_ordinary_prose():
    report = validation_report({"name": "宏", "description": '此刻是 {{getvar::stat_data.时间}}。'})
    assert report["warnings"] == []


def test_invalid_structure_stops_semantic_diagnostics():
    report = validation_report({"name": "坏开场", "alternate_greetings": 1})
    assert not report["valid"] and not report["adapted"] and report["errors"]


async def test_skill_is_whitelisted_and_full_resource_reaches_editor_model(tmp_path):
    definitions = {t["function"]["name"]: t["function"] for t in tool_definitions()}
    resources = definitions["read_skill"]["parameters"]["properties"]["resource"]["enum"]
    assert "references/status-display.md" in resources
    with pytest.raises(ValidationError):
        Skill(resource="../editor_config.py")
    # Guides point to each other by resource name; every named guide must stay readable.
    root = Path(__file__).parents[2] / "src/tavern/skills"
    texts = [SYSTEM, *(path.read_text(encoding="utf-8") for path in root.rglob("*.md"))]
    named = {name for text in texts for name in re.findall(r"(?:references/)?[\w-]+\.md", re.sub(r"https?://\S+", "", text))}
    assert named <= set(resources)
    store = Store(tmp_path)
    settings = store.settings_document()
    settings.shared.card_author.context_tokens = 200000  # all guides together exceed the default window
    store.save_settings(settings)
    editor = CardAuthorService(store)
    wid = editor.edits.create_conversation()["id"]
    provider = ToolLLM([("read_skill", {"resource": resource}) for resource in resources])
    try:
        result = await run(editor, wid, provider, "说明正文状态栏的迁移方法")
        assert result["status"] == "completed", result["error"]
        returned = [json.loads(m["content"]) for m in provider.call_log[-1] if m.get("role") == "tool"]
        sources = {path.relative_to(path.parents[1] if path.parent.name == "references" else path.parent).as_posix():
                   path.read_text(encoding="utf-8") for path in root.rglob("*.md")}
        assert {item["resource"]: item["text"] for item in returned} == sources
        assert not store.cards()
    finally:
        await editor.close()


async def test_editor_migration_preserves_source_and_initializes_each_opening_with_clean_contexts(tmp_path):
    store = Store(tmp_path)
    original = card()
    opaque_id = "50b3f562-051d-47f9-bfcc-e4c3e7076573"
    original["character_book"]["entries"][0]["id"] = opaque_id
    original["alternate_greetings"] = [OPENING.replace("08:20", "19:40").replace("12", "35")]
    source = store.import_card(dump(original).encode(), "fixture.json")
    editor = CardAuthorService(store)
    workspace = editor.edits.create(source["id"])
    wid = workspace["id"]
    model = ToolLLM([
        ("read_skill", {"resource": "references/status-display.md"}),
        ("read_field", {"card_ref": "C1", "path": ""}),
        ("validate_draft", {"card_ref": "C1"}),
        ("patch_draft", {"card_ref": "C1", "comment": "迁移正文状态栏", "operations": [
            {"op": "replace", "path": "/character_book/entries/0/content", "value": RULE},
            {"op": "add", "path": "/character_book/entries/0/extensions", "value": {"terrarium": {"audience": "tracker_mvu"}}},
            {"op": "add", "path": "/extensions/regex_scripts", "value": [{
                "scriptName": "隐藏初始化", "findRegex": r"/<initvar\b[^>]*>[\s\S]*?</initvar\s*>/gi",
                "replaceString": "", "placement": [2], "markdownOnly": True, "promptOnly": True}]}]}),
        *[("set_opening", {"card_ref": "C1", "index": i,
            "narrative": f'旅客放下行李。\n<initvar>时间: "{time}"\n营业收入: {money}</initvar>'})
          for i, time, money in [(0, "08:20", 12), (1, "19:40", 35)]],
        ("validate_draft", {"card_ref": "C1"}),
    ])
    try:
        result = await run(editor, wid, model, "将正文状态栏迁移为独立变量；金额已确认为营业收入，保留两个开场的初值。")
        assert result["status"] == "completed", result["error"]
        model_wire = dump(model.call_log[-1])
        assert "世界书第 1 条" in model_wire and "初始化来源" in model_wire
        assert opaque_id not in model_wire
        item = editor.edits.conversation(wid)["cards"][0]
        document = item["document"]
        assert item["validation"]["warnings"] == []
        assert store.card(source["id"])["document"] == original
        assert document["extensions"]["unknown"] == "preserved"
        assert document["character_book"]["entries"][0]["id"] == opaque_id
        assert document["description"] == original["description"]
        assert document["character_book"]["entries"][1] == original["character_book"]["entries"][1]
        migrated = store.import_card(dump(document).encode(), "migrated-fixture.json")
        for index, time, money in [(0, "08:20", 12), (1, "19:40", 35)]:
            probe = await probe_card(store, item, index=index)
            assert probe["status"] == "passed", probe
            assert probe["model_calls"] == 0
            state = probe["checks"][0]["state"]
            assert state["stat_data"] == {"时间": time, "营业收入": money}
            # Compile actual new-story messages in an isolated store: an existing
            # story would intentionally retain its original opening and settings.
            session = store.new_session(SessionCreate(card_id=migrated["id"], greeting=index))
            runtime = RoleplayRuntime(store)
            try:
                session = await runtime.initialize_session(session["id"])
            finally:
                await runtime.close()
            snapshot = session["snapshot"]
            half = compile_context(document, session, session["messages"], snapshot, "继续", store.settings(), "test")
            full = compile_window(document, session, session["messages"], snapshot, "继续", store.settings(), [], require_state_patch=True)
            for compiled in (half, full):
                wire = dump(compiled["messages"])
                assert TEMPLATE not in wire and "<details>" not in wire and "<initvar>" not in wire
                assert "旅客放下行李。" in wire and "旅店在清晨开门。" in wire
            assert RULE not in dump(half["messages"])
            assert RULE in dump(full["messages"])
        # Half Agent sends the same semantic rule to the independent variable task.
        provider = ScriptedLLM(['[]'])

        class Capture:
            def event(self, *args):
                pass

        await analyze_variables(Capture(), "test", store.settings(), [], state, "继续", "旅客等待。",
                                lambda: provider, document=document, persona="店主")
        assert RULE in dump(provider.last_messages) and TEMPLATE not in dump(provider.last_messages)
        assert store.card(source["id"])["document"] == original
    finally:
        await editor.close()
