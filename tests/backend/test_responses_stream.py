import pytest

from tavern.scripting.model_stream import StreamState
from tests.backend.chatgpt_fixtures import completed


@pytest.mark.parametrize("summary", ["empty", "full", "metadata"])
def test_done_items_survive_terminal_summary_without_duplication(summary):
    payload = completed("正文", [("lookup", {"query": "原文"})])
    payload["output"].insert(0, {"id": "r1", "type": "reasoning", "encrypted_content": "cipher", "summary": []})
    state = StreamState("responses")
    for index, item in enumerate(payload["output"]):
        state.feed({"type": "response.output_item.added", "output_index": index, "item": {"id": item["id"], "type": item["type"]}})
        state.feed({"type": "response.output_item.done", "output_index": index, "item": item})
    output = payload["output"] if summary == "full" else (
        [{"id": item["id"], "type": item["type"]} for item in payload["output"]] if summary == "metadata" else [])
    state.feed({"type": "response.completed", "response": {**payload, "output": output}})
    assert state.finish() == payload


def test_partial_item_or_missing_terminal_event_cannot_succeed():
    state = StreamState("responses")
    state.feed({"type": "response.output_item.added", "output_index": 0, "item": {"id": "m1", "type": "message", "content": []}})
    with pytest.raises(ValueError, match="未完整接收"):
        state.feed({"type": "response.completed", "response": {"status": "completed", "output": []}})
    with pytest.raises(ValueError, match="提前结束"):
        state.finish()
