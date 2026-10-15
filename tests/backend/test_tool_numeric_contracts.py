"""Dynamic position bounds match the model-facing contract without clamping indices."""
import copy
from types import SimpleNamespace

import pytest

from tavern.character.editor_config import blank_card
from tavern.character.editor_sandbox import test_card as probe_card
from tavern.character.editor_tools import EditorTools
from tavern.core.model_view import ModelView
from tavern.scripting.protocols import definitions, native_tools, response_tools
from tavern.web.tools import SPECS


def editor():
    document = blank_card("测试")
    document["data"].update(first_mes="A", alternate_greetings=["B", "C"])
    item = {"ref": "C1", "document": document, "kind": "draft"}
    owner = SimpleNamespace(context=SimpleNamespace(view=ModelView()), legacy=True, reads={}, working={},
                            edits=SimpleNamespace(item=lambda working, ref: item))
    return EditorTools(owner), item


@pytest.mark.parametrize("arguments,expected", [
    ({"action": "add", "index": 3, "narrative": "D"}, ["A", "B", "C", "D"]),
    ({"action": "add", "index": 0, "narrative": "Z"}, ["Z", "A", "B", "C"]),
    ({"action": "update", "index": 2, "narrative": "X"}, ["A", "B", "X"]),
    ({"action": "remove", "index": 2}, ["A", "B"]),
    ({"action": "move", "index": 0, "to_index": 2}, ["B", "C", "A"]),
    ({"action": "move", "index": 2, "to_index": 0}, ["C", "A", "B"]),
])
async def test_opening_positions_are_zero_based_and_move_destination_is_final(arguments, expected):
    tools, item = editor()
    await tools.execute("set_opening", {"card_ref": "C1", **arguments})
    data = item["document"]["data"]
    assert [data["first_mes"], *data["alternate_greetings"]] == expected


@pytest.mark.parametrize("arguments", [
    {"action": "add", "index": -1, "narrative": "D"},
    {"action": "add", "index": 4, "narrative": "D"},
    {"action": "update", "index": 3, "narrative": "D"},
    {"action": "remove", "index": 3},
    {"action": "move", "index": 3, "to_index": 0},
    {"action": "move", "index": 0, "to_index": 3},
    {"action": "move", "index": 0},
])
async def test_invalid_opening_positions_do_not_mutate_or_clamp(arguments):
    tools, item = editor()
    original = copy.deepcopy(item)
    with pytest.raises(ValueError):
        await tools.execute("set_opening", {"card_ref": "C1", **arguments})
    assert item == original


async def test_probe_checks_opening_and_sample_bounds_before_starting_browser():
    _, item = editor()
    with pytest.raises(ValueError, match="开场编号"):
        await probe_card(None, item, index=3)
    with pytest.raises(ValueError, match="sample_index"):
        await probe_card(None, item, samples=["one"], display_assertions=[{"sample_index": 2}])


def test_all_provider_schemas_preserve_web_page_numeric_limits_and_defaults():
    model, description = SPECS["read_webpage"]
    function = {"name": "read_webpage", "description": description, "parameters": model.model_json_schema()}
    tools = [{"type": "function", "function": function}]
    parameters = [definitions(tools)[0]["function"]["parameters"], response_tools(tools)[0]["parameters"],
                  native_tools(tools, "anthropic")[0]["input_schema"],
                  native_tools(tools, "gemini")[0]["functionDeclarations"][0]["parametersJsonSchema"]]
    for schema in parameters:
        assert schema == function["parameters"]
        assert schema["properties"]["offset"]["minimum"] == 0
        limit = schema["properties"]["limit"]
        assert (limit["minimum"], limit["maximum"], limit["default"]) == (1, 30000, 12000)
