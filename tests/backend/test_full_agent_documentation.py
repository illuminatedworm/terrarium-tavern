"""Keep documented schemas, source descriptions and successful examples executable."""
import copy
import json
import re
from types import SimpleNamespace

import pytest

from tavern.core.model_view import ModelView
from tavern.core.models import Settings, TurnRequest
from tavern.full_agent.protocol import success
from tavern.full_agent.state import new_state
from tavern.full_agent.tools import SPECS, AgentTools, tool_definitions
from tools.export_full_agent_tools import ENTRY, EXAMPLES, NOTE_KEY, generated_documents


def test_generated_documents_and_catalog_match_source():
    for path, expected in generated_documents():
        assert path.read_text(encoding="utf-8") == expected, f"Run tools/export_full_agent_tools.py: {path}"
    summary = next(generated_documents())[1]
    table = summary.split("## 完整工具列表", 1)[1].split("## 编辑参数补充", 1)[0]
    assert set(re.findall(r"^\| `([a-z_]+)` \|", table, re.M)) == set(SPECS)


@pytest.mark.parametrize("name", list(SPECS))
async def test_documented_minimal_results_match_execution(name):
    settings = Settings()
    state = new_state()
    state["notes"][NOTE_KEY] = {"key": NOTE_KEY, "content": "明天去港口。", "kind": "plan",
                               "status": "active", "revision": 1, "sources": []}
    document = {"name": "向导", "description": "角色描述。", "character_book": {
        "entries": [{"id": "port", "comment": ENTRY["title"],
                     **{key: value for key, value in ENTRY.items() if key not in ("ref", "title")}}]}}
    request = TurnRequest(operation_id="docs", expected_revision=0, allow_content_edit=True, update_variables=True)
    owner = SimpleNamespace(document=document, session={"plot_enabled": True}, state=state,
        snapshot={"state": {"stat_data": {"hp": 8}}, "scopes": {}}, history=[],
        source_history=[{"id": "source", "role": "user", "raw": "我明天去港口。"}],
        settings=settings, request=request, model_view=ModelView(document), tid="docs", patch_state_done=False,
        definitions=tool_definitions(request, {"policy_effective": "directed", "plot_enabled": True}))

    async def retrieve(query, **options):
        assert query == "港口" and not options["include_ref"] and not options["include_title"]
        return {"matches": [{"text": "<lorebook>\n" + ENTRY["content"] + "\n</lorebook>"}]}

    owner.retrieve = retrieve
    tools = AgentTools(owner)
    if name == "revise_content":
        await tools.execute("read_character", '{"ref":"description"}')
    if name in ("write_note", "archive_note"):
        await tools.execute("read_note", json.dumps({"key": NOTE_KEY}))
    arguments, expected = copy.deepcopy(EXAMPLES[name])
    # Tool parsing validates every documented top-level and nested parameter.
    result = await tools.execute(name, json.dumps(arguments, ensure_ascii=False))
    assert result == success(expected)
    assert "revision" not in json.dumps(result)
