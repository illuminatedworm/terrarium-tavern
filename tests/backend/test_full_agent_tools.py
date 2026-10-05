import json
import re

import pytest
from httpx import ASGITransport, AsyncClient

from tavern.application.api import create_app
from tavern.core.models import SessionCreate, TurnRequest
from tavern.full_agent.agent import FullAgent
from tavern.full_agent.tools import SPECS
from tavern.storage.store import Store
from tests.backend import test_full_agent
from tests.backend.test_full_agent import Model, runtime, turn

story = test_full_agent.story


@pytest.mark.parametrize("name,arguments,expected", [
    ("read_history", {"reason": "PRIVATE_INPUT_48c2515daf9742308a8b50777d39dd75"},
     ["缺少必填参数 ref", "不接受参数 reason"]),
    ("read_history", {"ref": "H2", "48c2515daf9742308a8b50777d39dd75": "PRIVATE_INPUT"},
     ["不接受参数 @D", "请删除"]),
    ("write_handoff", {"content": "不得写入的草稿", "reason": "多余说明"}, ["不接受参数 reason"]),
    ("read_history", {"ref": 35}, ["参数 ref 必须是字符串"]),
    ("read_history", [], ["参数必须是对象"]),
    ("new_context", {"content": "不得换窗", "keep_recent": 99}, ["参数 keep_recent 必须小于或等于 12"]),
    ("search_history", {"query": "*", "limit": "2"}, ["参数 limit 必须是整数"]),
    ("search_history", {"query": "*", "limit": 0}, ["参数 limit 必须大于或等于 1"]),
    ("search_history", {"query": ""}, ["参数 query 至少需要 1 个字符"]),
    ("write_note", {"key": "test", "content": "未接受", "kind": "wrong"},
     ["参数 kind 必须取允许值", "fact", "hypothesis", "plan"]),
    ("write_note", {"key": "test", "content": "未接受", "kind": "fact", "sources": [35]},
     ["参数 sources[0] 必须是字符串"]),
    ("write_note", {"key": "test", "content": "未接受", "kind": "fact", "sources": ["H2"] * 21},
     ["参数 sources 最多允许 20 项"]),
])
async def test_argument_errors_explain_repairs_without_executing_or_echoing_input(story, name, arguments, expected):
    store, session = story
    service = runtime(store)
    model = Model((name, arguments), "继续正文")
    try:
        result, current = await turn(service, session, model)
        assert result["status"] == "committed", result["error"]
        feedback = json.loads(next(m["content"] for m in model.requests[1]["messages"] if m["role"] == "tool"))
        assert feedback["error"].startswith(f"{name} 参数错误，工具未执行：")
        assert all(text in feedback["error"] for text in expected)
        schema = feedback["expected_parameters"]
        original = SPECS[name][0].model_json_schema()
        assert set(schema["properties"]) == set(original["properties"])
        assert schema.get("required", []) == original.get("required", [])
        assert schema["additionalProperties"] is False
        encoded = json.dumps(feedback, ensure_ascii=False)
        assert all(text not in encoded for text in (
            "errors.pydantic.dev", "validation error", "input_value", "input_type",
            "PRIVATE_INPUT", "48c2515daf9742308a8b50777d39dd75", '"title"'))
        for key in ("handoff", "notes", "window"):
            assert current["snapshot"]["full_agent"][key] == session["snapshot"]["full_agent"][key]
        event = next(e for e in store.events(result["id"]) if e["kind"] == "full_agent_tool")
        assert event["payload"]["result"] == feedback
    finally:
        await service.close()


async def test_read_history_can_correct_missing_ref_after_actionable_feedback(story):
    store, session = story
    service = runtime(store)
    model = Model(("read_history", {"reason": "核对H2内容"}), ("read_history", {"ref": "H2"}), "核对后继续正文")
    try:
        result, current = await turn(service, session, model)
        assert result["status"] == "committed", result["error"]
        returned = [json.loads(m["content"]) for m in model.requests[-1]["messages"] if m["role"] == "tool"]
        assert "缺少必填参数 ref" in returned[0]["error"]
        assert returned[0]["expected_parameters"]["required"] == ["ref"]
        assert set(returned[0]["expected_parameters"]["properties"]) == {"ref"}
        assert returned[0]["expected_parameters"]["properties"]["ref"]["type"] == "string"
        assert "error" not in returned[1] and returned[1]["ref"] == "H2"
        assert returned[1]["text"] == "我答应明天在钟楼见面。"
        assert current["messages"][-1]["raw"] == "核对后继续正文"
    finally:
        await service.close()


@pytest.mark.parametrize("name", ["search_lore", "unknown_tool"])
async def test_unknown_tool_errors_identify_missing_name_and_allow_retry(story, name):
    store, session = story
    service = runtime(store)
    model = Model((name, {"query": "灯塔"}), ("search_read_lore", {"query": "灯塔"}), "继续。")
    try:
        result, current = await turn(service, session, model)
        assert result["status"] == "committed", result["error"]
        assert name not in {t["function"]["name"] for t in model.requests[0]["extra_body"]["tools"]}
        output = [json.loads(m["content"]) for m in model.requests[-1]["messages"] if m["role"] == "tool"]
        assert output[0] == {"error": f"工具不存在：{name}；请使用本轮提供的工具名称"}
        assert output[1]["matches"]
        event = next(e for e in store.events(result["id"]) if e["kind"] == "full_agent_tool")
        assert event["payload"]["result"] == output[0]
        for key in ("handoff", "notes", "window"):
            assert current["snapshot"]["full_agent"][key] == session["snapshot"]["full_agent"][key]
    finally:
        await service.close()


@pytest.mark.parametrize("lore_name", ["search_lore", "search_read_lore"])
async def test_retired_lore_tool_settings_remain_editable_and_calls_are_rejected(story, lore_name):
    store, session = story
    options = {"read_lore": {"include_metadata": False}, lore_name: {"include_title": True}}
    with store.connection() as db:
        db.execute("UPDATE sessions SET disabled_tools=?,tool_result_options=? WHERE id=?",
                   (json.dumps(["read_lore", "write_note", lore_name]), json.dumps(options), session["id"]))
    session = Store(store.root).session(session["id"])
    assert session["disabled_tools"] == ["write_note", "search_read_lore"]
    assert session["tool_result_options"] == {"search_read_lore": options[lore_name]}
    fork = store.fork_session(session["id"], session["head"], session["revision"])
    assert fork["disabled_tools"] == session["disabled_tools"]
    assert fork["tool_result_options"] == session["tool_result_options"]
    app = create_app(store.root)
    service = runtime(store)
    url = f"/api/sessions/{session['id']}/full-agent/tools"
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
            # A pre-upgrade open client may submit every switch, including the retired tool.
            response = await client.put(url, json={"expected_revision": 0,
                "disabled_tools": [*SPECS, "read_lore"], "tool_result_options": options})
            assert response.status_code == 200
            session = response.json()
            assert set(session["disabled_tools"]) == set(SPECS)
            assert session["tool_result_options"] == {"search_read_lore": options[lore_name]}
            response = await client.put(url, json={"expected_revision": session["revision"], "disabled_tools": []})
            assert response.status_code == 200
            session = response.json()
        model = Model(("read_lore", {"ref": "L1"}), ("search_read_lore", {"query": "灯塔"}), "继续。")
        result, _ = await turn(service, session, model)
        assert result["status"] == "committed", result["error"]
        assert all("read_lore" not in {t["function"]["name"] for t in r["extra_body"]["tools"]}
                   for r in model.requests)
        assert all(r["extra_body"]["tools"] == model.requests[0]["extra_body"]["tools"] for r in model.requests)
        initial = model.requests[0]["messages"]
        input_at = max(i for i, message in enumerate(initial) if message["role"] == "user")
        assert all(r["messages"][:input_at + 1] == initial[:input_at + 1] for r in model.requests)
        output = [json.loads(m["content"]) for m in model.requests[-1]["messages"] if m["role"] == "tool"]
        assert output[0] == {"error": "工具不存在：read_lore；请使用本轮提供的工具名称"}
        assert output[1]["matches"]
        assert all("text" in match and "error" not in match for match in output[1]["matches"])
        # The rejected historical call/result remains a complete exchange before the successful search.
        messages = model.requests[-1]["messages"]
        exchange = [m for m in messages if m.get("tool_calls") or m["role"] == "tool"]
        assert [m["role"] for m in exchange] == ["assistant", "tool", "assistant", "tool"]
        assert exchange[1]["tool_call_id"] == exchange[0]["tool_calls"][0]["id"]
        assert exchange[3]["tool_call_id"] == exchange[2]["tool_calls"][0]["id"]
    finally:
        await service.close()
        await app.state.service.close()


@pytest.mark.parametrize("tool", ["patch_state", "read_state"])
async def test_required_mvu_update_rejects_disabled_dependency(story, tool):
    store, session = story
    with store.connection() as db:
        db.execute("UPDATE sessions SET disabled_tools=? WHERE id=?", (json.dumps([tool]), session["id"]))
    service = runtime(store)
    try:
        with pytest.raises(ValueError, match=f"启用 {tool}"):
            service.submit(session["id"], TurnRequest(
                text="继续", operation_id="mvu-disabled", expected_revision=0, update_variables=True))
        assert store.session(session["id"])["turns"] == []
    finally:
        await service.close()


async def test_tool_catalog_switches_persist_and_enforce_permissions(story):
    store, session = story
    app = create_app(store.root)
    service = runtime(store)
    url = f"/api/sessions/{session['id']}/full-agent/tools"
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
            catalog = (await client.get(url)).json()["tools"]
            preview_url = f"/api/sessions/{session['id']}/preview"
            original_tools = (await client.post(preview_url, json={"text": "继续"})).json()["tools"]
            assert {t["name"] for t in catalog} == set(SPECS)
            assert "read_lore" not in {t["name"] for t in catalog}
            for tool in catalog:
                assert tool["enabled"]
                assert not re.search(r"[\u4e00-\u9fff]", json.dumps(tool, ensure_ascii=False))
                assert all(p.get("description") for p in tool["parameters"]["properties"].values())
            lore = next(t for t in catalog if t["name"] == "search_read_lore")
            assert lore["result_options"] == {"include_ref": False, "include_title": False}
            assert set(lore["parameters"]["properties"]) == {"query"}
            patch = next(t for t in catalog if t["name"] == "patch_state")
            assert not patch["available"]
            authorized = (await client.get(url, params={"update_variables": "true"})).json()["tools"]
            assert next(t for t in authorized if t["name"] == "patch_state")["available"]
            response = await client.put(url, json={"expected_revision": 0, "disabled_tools": ["write_handoff"]})
            assert response.status_code == 200
            session = response.json()
            assert Store(store.root).session(session["id"])["disabled_tools"] == ["write_handoff"]
            assert (await client.put(url, json={"expected_revision": 0, "disabled_tools": []})).status_code == 409
            assert (await client.put(url, json={"expected_revision": 1, "disabled_tools": ["unknown"]})).status_code == 400
            preview = (await client.post(f"/api/sessions/{session['id']}/preview", json={"text": "继续"})).json()
            assert "write_handoff" not in {t["function"]["name"] for t in preview["tools"]}
            model = Model(("write_handoff", {"content": "禁止写入"}), "正文")
            result, session = await turn(service, session, model)
            assert result["status"] == "committed", result["error"]
            assert not session["snapshot"]["full_agent"]["handoff"]["text"]
            assert "write_handoff" not in {t["function"]["name"] for t in model.requests[0]["extra_body"]["tools"]}
            feedback = json.loads(next(m["content"] for m in model.requests[1]["messages"] if m["role"] == "tool"))
            assert feedback == {"error": "当前模式或授权不允许此工具"}
            fork = store.fork_session(session["id"], session["head"], session["revision"])
            assert fork["disabled_tools"] == ["write_handoff"]
            result, session = await turn(service, session, Model("重生成"), mode="regenerate")
            assert result["status"] == "committed"
            assert session["disabled_tools"] == ["write_handoff"]
            response = await client.put(url, json={"expected_revision": session["revision"], "disabled_tools": list(SPECS)})
            session = response.json()
            no_tools = Model("不使用工具的正文")
            result, session = await turn(service, session, no_tools)
            assert result["status"] == "committed", result["error"]
            assert no_tools.requests[0]["extra_body"] == {}
            response = await client.put(url, json={"expected_revision": session["revision"], "disabled_tools": []})
            session = response.json()
            assert session["disabled_tools"] == []
            restored_tools = (await client.post(preview_url, json={"text": "继续"})).json()["tools"]
            assert json.dumps(restored_tools, ensure_ascii=False) == json.dumps(original_tools, ensure_ascii=False)
            tid, _ = store.create_turn(session["id"], TurnRequest(operation_id="pending", expected_revision=session["revision"]))
            assert (await client.put(url, json={"expected_revision": session["revision"], "disabled_tools": []})).status_code == 409
            store.set_turn(tid, status="cancelled")
            half = store.new_session(SessionCreate(card_id=session["card"]))
            assert (await client.get(f"/api/sessions/{half['id']}/full-agent/tools")).status_code == 400
    finally:
        await service.close()
        await app.state.service.close()


async def test_user_result_options_save_and_control_tool_output(tmp_path):
    store = Store(tmp_path)
    card = store.import_card(json.dumps({"name": "港口", "first_mes": "开场。",
        "character_book": {"entries": [{"id": "port", "keys": ["旧港"], "comment": "旧港",
                                       "content": "旧港有一艘船。", "enabled": True}]}}
    ).encode(), "card.json")
    settings = store.settings()
    settings.retrieval.channels = ["keyword"]
    settings.full_agent_config.auto_retrieval = False
    store.save_settings(settings)
    session = store.new_session(SessionCreate(card_id=card["id"], agent_mode="full_agent"))
    app = create_app(store.root)
    service = runtime(store)
    url = f"/api/sessions/{session['id']}/full-agent/tools"
    options = {"search_read_lore": {"include_ref": True, "include_title": True},
               "search_history": {"include_preview": False},
               "read_history": {"include_metadata": False}}
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
            assert (await client.put(url, json={"expected_revision": 0, "disabled_tools": [],
                                                "tool_result_options": {"search_read_lore": {"unknown": True}}})).status_code == 400
            response = await client.put(url, json={"expected_revision": 0, "disabled_tools": [],
                                                    "tool_result_options": options})
            assert response.status_code == 200
            session = response.json()
            assert session["tool_result_options"] == options
            catalog = (await client.get(url)).json()["tools"]
            assert next(t for t in catalog if t["name"] == "search_read_lore")["result_options"] == options["search_read_lore"]
            fork = store.fork_session(session["id"], session["head"], session["revision"])
            assert fork["tool_result_options"] == options
            model = Model(("search_read_lore", {"query": "旧港"}),
                          ("search_history", {"query": "开场"}),
                          ("read_history", {"ref": "H1"}), "继续。")
            result, _ = await turn(service, session, model)
            assert result["status"] == "committed", result["error"]
            output = [json.loads(m["content"]) for m in model.requests[-1]["messages"] if m["role"] == "tool"]
            assert output[0]["matches"] == [{"ref": "L1", "title": "旧港",
                                              "text": "<lorebook>\n旧港有一艘船。\n</lorebook>"}]
            assert "preview" not in output[1]["matches"][0]
            assert "offset" not in output[1]["matches"][0]
            assert set(output[2]) == {"text"}
    finally:
        await service.close()
        await app.state.service.close()


async def test_disabled_rollover_fails_before_requesting_unavailable_tool(story, monkeypatch):
    store, session = story
    with store.connection() as db:
        db.execute("UPDATE sessions SET disabled_tools=? WHERE id=?", (json.dumps(["new_context"]), session["id"]))
    session = store.session(session["id"])
    original = FullAgent.compile

    def full_window(self):
        result = original(self)
        self.must_rollover = True
        return result

    monkeypatch.setattr(FullAgent, "compile", full_window)
    service = runtime(store)
    model = Model("应当不请求模型")
    try:
        result, current = await turn(service, session, model)
        assert result["status"] == "error"
        assert "new_context 已关闭" in result["error"]
        assert model.requests == []
        assert current["snapshot"] == session["snapshot"]
    finally:
        await service.close()


@pytest.mark.parametrize("wrapped", [False, True])
async def test_character_tool_has_same_semantic_contract_for_wrapped_and_legacy_cards(wrapped):
    from types import SimpleNamespace

    from tavern.core.model_view import ModelView
    from tavern.full_agent.state import new_state
    from tavern.full_agent.tools import AgentTools, tool_definitions

    data = {"name": "Guide", "description": "Before", "custom_field": "Custom"}
    document = {"spec": "chara_card_v2", "spec_version": "2.0", "data": data} if wrapped else data
    definitions = tool_definitions(TurnRequest(operation_id="guidance", expected_revision=0),
                                   {"policy_effective": "auto"}, document=document)
    catalog = {item["function"]["name"]: item["function"] for item in definitions}
    assert "custom_field" not in json.dumps(catalog)
    assert "No wildcard" in catalog["read_character"]["description"]
    owner = SimpleNamespace(document=document, definitions=definitions, state=new_state(), snapshot={},
                            session={}, model_view=ModelView(document), tid="guidance")
    tools = AgentTools(owner)
    before = await tools.execute("read_character", json.dumps({"ref": "description"}))
    assert before["text"] == "Before"
    await tools.execute("revise_content", json.dumps({"operations": [
        {"op": "set_character", "field": "description", "value": "After"}], "reason": "Update setting"}))
    after = await tools.execute("read_character", json.dumps({"ref": "description"}))
    assert after["text"] == "After"
