"""Actual Agent loops use the shared service without changing story/draft permissions."""
import json

import httpx
import pytest

from tavern.advisor.runtime import AdvisorService, ConsultationRequest
from tavern.application.api import create_app
from tavern.character.card_editor import CardEditorService
from tavern.core.models import CardEditorRequest
from tavern.storage.store import dump, uid
from tests.backend.chatgpt_fixtures import completed, sse
from tests.backend.test_advisor import ask, setup
from tests.backend.test_card_editor import ToolLLM, import_fixture
from tests.backend.test_chatgpt_integration import connected_store, story


@pytest.mark.parametrize("agent", ["advisor", "card_editor"])
async def test_shared_web_full_results_reach_model_and_off_is_enforced(tmp_path, monkeypatch, agent):
    app, store, session, advisor = setup(tmp_path)
    doc = store.settings_document()
    doc.shared.web.exa.enabled = False
    doc.shared.web.duckduckgo.enabled = False
    doc.shared.web.tavily.api_key = "private-tavily-key"
    doc.shared.web.tavily.auth_mode = "api_key"
    doc.shared.card_editor.context_tokens = 200000
    store.save_settings(doc)
    source = "完整网页资料" * 10000 + "模型必须收到尾部"
    requests = []
    async def public(url):
        return url
    monkeypatch.setattr("tavern.web.client.public_url", public)
    def route(request):
        requests.append(request)
        assert request.headers["authorization"] == "Bearer private-tavily-key"
        if request.url.path == "/search":
            return httpx.Response(200, json={"results": [{"title": "来源", "url": "https://example.org", "content": "定位信息"}]})
        return httpx.Response(200, json={"results": [{"url": "https://example.org", "raw_content": source}]})
    provider = ToolLLM([("web_search", {"query": "背景"}), ("read_webpage", {"url": "https://example.org"})])
    if agent == "advisor":
        service = advisor
        service.web_transport = httpx.MockTransport(route)
        result = await ask(service, session["id"], provider)
        assert store.session(session["id"])["messages"] == session["messages"]
        assert result["status"] == "completed", result
        doc.shared.advisor.web.mode = "off"
    else:
        service = CardEditorService(store)
        service.web_transport = httpx.MockTransport(route)
        card = import_fixture(store)
        workspace = service.edits.create(card["id"])
        service.provider_factory = lambda: provider
        run = service.submit(workspace["id"], CardEditorRequest(text="查证设定", expected_revision=0, operation_id=uid()))
        await service.tasks[run["id"]]
        assert service.edits.run(run["id"])["status"] == "completed"
        assert service.edits.get(workspace["id"])["document"] == card["document"]
        doc.shared.card_editor_config.web.mode = "off"
    assert len(requests) == 2
    final_wire = dump(provider.call_log[-1])
    assert source in final_wire and "private-tavily-key" not in final_wire
    store.save_settings(doc)
    blocked = ToolLLM([("web_search", {"query": "不能执行"})])
    if agent == "advisor":
        assert (await ask(service, session["id"], blocked))["status"] == "completed"
    else:
        service.provider_factory = lambda: blocked
        run = service.submit(workspace["id"], CardEditorRequest(text="查证", expected_revision=0, operation_id=uid()))
        await service.tasks[run["id"]]
        assert service.edits.run(run["id"])["status"] == "completed"
    assert len(requests) == 2
    assert not {t.name for t in blocked.schemas[0]} & {"web_search", "read_webpage"}
    assert "error" in dump(blocked.call_log[-1])
    await service.close()


@pytest.mark.parametrize("mode", ["auto", "local", "off"])
async def test_previews_use_actual_agent_mode_and_never_expose_shared_credentials(tmp_path, mode):
    store, _, _ = connected_store(tmp_path)
    doc = store.settings_document()
    doc.shared.advisor.official_model = doc.full_agent.official_model.model_copy(deep=True)
    doc.shared.advisor.web.mode = doc.shared.card_editor_config.web.mode = mode
    doc.shared.web.exa.api_key = "never-in-preview"
    store.save_settings(doc)
    app = create_app(tmp_path)
    app.state.store._chatgpt_accounts = store.chatgpt
    card, session = story(app.state.store)
    workspace = app.state.editor.edits.create(card["id"])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        responses = [await client.get(f"/api/sessions/{session['id']}/advisor/preview"),
                     await client.post(f"/api/card-editor/conversations/{workspace['id']}/preview", json={})]
    for response in responses:
        assert response.status_code == 200, response.text
        assert "never-in-preview" not in response.text
        tools = response.json()["tools"]
        functions = {t.get("function", {}).get("name") for t in tools}
        assert any(t["type"] == "web_search" for t in tools) == (mode == "auto")
        assert ("web_search" in functions) == (mode == "local")
        assert ("read_webpage" in functions) == (mode != "off")
    await app.state.service.close()


@pytest.mark.parametrize("agent", ["advisor", "card_editor"])
@pytest.mark.parametrize("resume", [False, True])
async def test_cloud_fallback_in_agent_preserves_prior_mutation_and_native_sources(tmp_path, monkeypatch, agent, resume):
    store, _, _ = connected_store(tmp_path)
    card, session = story(store)
    doc = store.settings_document()
    doc.shared.advisor.official_model = doc.full_agent.official_model.model_copy(deep=True)
    doc.shared.web.exa.enabled = False
    doc.shared.web.duckduckgo.enabled = False
    store.save_settings(doc)
    wire = []
    def route(request):
        body = json.loads(request.content)
        wire.append(body)
        n = len(wire)
        if n == 1:
            tools = [("update_turn_preference", {"action": "append", "content": "保持悬念", "request_ref": "Q1"})] if agent == "advisor" else [
                ("read_field", {"card_ref": "C1", "path": ""}),
                ("set_text_field", {"card_ref": "C1", "path": "/description", "text": "修改后设定", "comment": "更新设定"})]
            return sse(completed("", tools))
        if n == 2:
            return httpx.Response(400, json={"error": {"code": "unsupported_tool", "param": "tools", "message": "web_search is not supported"}})
        if n == 3:
            payload = completed("", [("web_search", {"query": "资料"})])
            payload["output"][0].update(id="item-search", call_id="call-search")
            return sse(payload)
        if n == 4 and resume:
            return sse(events=[{"type": "response.output_text.delta", "delta": "连接提前结束"}])
        return sse(completed("搜索完成。"))
    store.chatgpt.http_transport = httpx.MockTransport(route)
    async def public(url):
        return url
    monkeypatch.setattr("tavern.web.client.public_url", public)
    web_requests = []
    def local(request):
        web_requests.append(request)
        return httpx.Response(200, json={"results": [{"title": "完整定位资料", "url": "https://example.org/source", "content": "已取得搜索结果尾部"}]})
    web_transport = httpx.MockTransport(local)
    if agent == "advisor":
        service = AdvisorService(store, None)
        service.web_transport = web_transport
        request = ConsultationRequest(text="保存偏好：保持悬念，然后查资料", expected_revision=0, operation_id=uid())
        run = service.submit(session["id"], request)
    else:
        service = CardEditorService(store)
        service.web_transport = web_transport
        workspace = service.edits.create(card["id"])
        run = service.submit(workspace["id"], CardEditorRequest(text="修改设定并查资料", expected_revision=0, operation_id=uid()))
    await service.tasks[run["id"]]
    result = service.data.run(run["id"]) if agent == "advisor" else service.edits.run(run["id"])
    if resume:
        assert result["status"] == "error"
        if agent == "advisor":
            assert store.session(session["id"])["turn_preference"] == ""
            continued = service.submit(session["id"], ConsultationRequest(text="", expected_revision=0, operation_id=uid(), resume_run_id=run["id"]))
        else:
            assert service.edits.get(workspace["id"])["document"]["description"] != "修改后设定"
            continued = service.submit(workspace["id"], CardEditorRequest(text="", expected_revision=0, operation_id=uid(), resume_run_id=run["id"]))
        await service.tasks[continued["id"]]
        result = service.data.run(continued["id"]) if agent == "advisor" else service.edits.run(continued["id"])
        assert any(i.get("type") == "function_call_output" and "已取得搜索结果尾部" in i.get("output", "") for i in wire[-1]["input"])
    assert result["status"] == "completed", result.get("error")
    assert len(wire) == (5 if resume else 4)
    assert len(web_requests) == 1
    assert wire[1]["input"] == wire[2]["input"]
    assert wire[0]["tools"][-1]["type"] == "web_search"
    assert not any(t["type"] == "web_search" for t in wire[2]["tools"])
    if agent == "advisor":
        assert store.session(session["id"])["turn_preference"] == "保持悬念"
        trace = service.public(session["id"])["exchanges"][-1]["trace"]
        assert sum(c["function"]["name"] == "update_turn_preference" for row in trace for c in row.get("tool_calls", [])) == 1
    else:
        assert service.edits.get(workspace["id"])["document"]["description"] == "修改后设定"
        assert store.card(card["id"])["document"]["description"] != "修改后设定"
    await service.close()
