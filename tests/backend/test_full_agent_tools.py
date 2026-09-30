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


async def test_retired_lore_tool_settings_remain_editable_and_calls_are_rejected(story):
    store, session = story
    options = {"read_lore": {"include_metadata": False}, "search_lore": {"include_title": True}}
    with store.connection() as db:
        db.execute("UPDATE sessions SET disabled_tools=?,tool_result_options=? WHERE id=?",
                   (json.dumps(["read_lore", "write_note"]), json.dumps(options), session["id"]))
    session = Store(store.root).session(session["id"])
    assert session["disabled_tools"] == ["write_note"]
    assert session["tool_result_options"] == {"search_lore": options["search_lore"]}
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
            assert session["tool_result_options"] == {"search_lore": options["search_lore"]}
            response = await client.put(url, json={"expected_revision": session["revision"], "disabled_tools": []})
            assert response.status_code == 200
            session = response.json()
        model = Model(("read_lore", {"ref": "L1"}), ("search_lore", {"query": "灯塔"}), "继续。")
        result, _ = await turn(service, session, model)
        assert result["status"] == "committed", result["error"]
        assert all("read_lore" not in {t["function"]["name"] for t in r["extra_body"]["tools"]}
                   for r in model.requests)
        assert all(r["extra_body"]["tools"] == model.requests[0]["extra_body"]["tools"] for r in model.requests)
        initial = model.requests[0]["messages"]
        input_at = max(i for i, message in enumerate(initial) if message["role"] == "user")
        assert all(r["messages"][:input_at + 1] == initial[:input_at + 1] for r in model.requests)
        output = [json.loads(m["content"]) for m in model.requests[-1]["messages"] if m["role"] == "tool"]
        assert "不允许此工具" in output[0]["error"]
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


async def test_required_mvu_update_rejects_disabled_patch_tool(story):
    store, session = story
    with store.connection() as db:
        db.execute("UPDATE sessions SET disabled_tools=? WHERE id=?", (json.dumps(["patch_state"]), session["id"]))
    service = runtime(store)
    try:
        with pytest.raises(ValueError, match="启用 patch_state"):
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
            lore = next(t for t in catalog if t["name"] == "search_lore")
            assert lore["result_options"] == {"include_ref": False, "include_title": False}
            assert set(lore["parameters"]["properties"]) == {"query"}
            patch = next(t for t in catalog if t["name"] == "patch_state")
            assert not patch["available"]
            authorized = (await client.get(url, params={"update_variables": "true"})).json()["tools"]
            assert next(t for t in authorized if t["name"] == "patch_state")["available"]
            response = await client.put(url, json={"expected_revision": 0, "disabled_tools": ["write_checkpoint"]})
            assert response.status_code == 200
            session = response.json()
            assert Store(store.root).session(session["id"])["disabled_tools"] == ["write_checkpoint"]
            assert (await client.put(url, json={"expected_revision": 0, "disabled_tools": []})).status_code == 409
            assert (await client.put(url, json={"expected_revision": 1, "disabled_tools": ["unknown"]})).status_code == 400
            preview = (await client.post(f"/api/sessions/{session['id']}/preview", json={"text": "继续"})).json()
            assert "write_checkpoint" not in {t["function"]["name"] for t in preview["tools"]}
            model = Model(("write_checkpoint", {"content": "禁止写入"}), "正文")
            result, session = await turn(service, session, model)
            assert result["status"] == "committed", result["error"]
            assert not session["snapshot"]["full_agent"]["checkpoint"]["text"]
            assert "write_checkpoint" not in {t["function"]["name"] for t in model.requests[0]["extra_body"]["tools"]}
            assert "error" in json.loads(next(m["content"] for m in model.requests[1]["messages"] if m["role"] == "tool"))
            fork = store.fork_session(session["id"], session["head"], session["revision"])
            assert fork["disabled_tools"] == ["write_checkpoint"]
            result, session = await turn(service, session, Model("重生成"), mode="regenerate")
            assert result["status"] == "committed"
            assert session["disabled_tools"] == ["write_checkpoint"]
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
    options = {"search_lore": {"include_ref": True, "include_title": True},
               "search_history": {"include_preview": False},
               "read_history": {"include_metadata": False}}
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
            assert (await client.put(url, json={"expected_revision": 0, "disabled_tools": [],
                                                "tool_result_options": {"search_lore": {"unknown": True}}})).status_code == 400
            response = await client.put(url, json={"expected_revision": 0, "disabled_tools": [],
                                                    "tool_result_options": options})
            assert response.status_code == 200
            session = response.json()
            assert session["tool_result_options"] == options
            catalog = (await client.get(url)).json()["tools"]
            assert next(t for t in catalog if t["name"] == "search_lore")["result_options"] == options["search_lore"]
            fork = store.fork_session(session["id"], session["head"], session["revision"])
            assert fork["tool_result_options"] == options
            model = Model(("search_lore", {"query": "旧港"}),
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
