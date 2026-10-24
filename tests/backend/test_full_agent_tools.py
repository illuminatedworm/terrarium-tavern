import json
import re

import pytest
from httpx import ASGITransport, AsyncClient

from tavern.application.api import create_app
from tavern.core.models import SessionCreate, TurnRequest
from tavern.full_agent.activity import turn_items
from tavern.full_agent.agent import FullAgent
from tavern.full_agent.handoff import (
    HandoffError,
    HandoffWrite,
    apply_write,
    render_text,
    render_written_current,
    require_coverage,
)
from tavern.full_agent.tools import SPECS
from tavern.storage.store import Store
from tests.backend import test_full_agent
from tests.backend.test_full_agent import Model, current_text, handoff, runtime, turn

story = test_full_agent.story


@pytest.mark.parametrize("name", ["patch_state", "revise_content"])
async def test_tool_comment_contract_repairs_old_arguments_and_preserves_activity(story, name):
    store, session = story
    service = runtime(store)
    operations = (
        []
        if name == "patch_state"
        else [{"op": "set_character", "field": "description", "value": "新的角色描述"}]
    )
    calls = [] if name == "patch_state" else [("read_character", {"ref": "description"})]
    comment = "本轮操作备注"
    model = Model(
        *calls,
        (name, {"operations": operations, "reason": "旧参数备注"}),
        (name, {"operations": operations, "comment": comment}),
        "继续正文",
    )
    try:
        result, current = await turn(
            service,
            session,
            model,
            update_variables=name == "patch_state",
            allow_content_edit=name == "revise_content",
        )
        assert result["status"] == "committed", result["error"]
        schema = next(
            tool["function"]["parameters"]
            for tool in model.requests[0]["extra_body"]["tools"]
            if tool["function"]["name"] == name
        )
        assert set(schema["properties"]) == {"operations", "comment"}
        assert "comment" in schema["required"]
        outputs = [
            json.loads(message["content"])
            for message in model.requests[-1]["messages"]
            if message["role"] == "tool"
        ]
        feedback, successful = outputs[-2:]
        assert feedback["error"]["code"] == "INVALID_ARGUMENTS"
        assert "缺少必填参数 comment" in feedback["error"]["message"]
        assert "不接受参数 reason" in feedback["error"]["message"]
        assert set(feedback["expected_parameters"]["properties"]) == {"operations", "comment"}
        assert successful["ok"] and successful["data"]["saved_to_draft"]
        if name == "revise_content":
            content = current["snapshot"]["content"]
            assert content["document"]["data"]["description"] == "新的角色描述"
            assert content["reason"] == comment
        else:
            assert successful["data"]["patch_state_completed"]
        items = [
            item
            for item in turn_items(store, session["id"], result["id"])
            if item["kind"] == "tool" and item["name"] == name
        ]
        assert [item["summary"] for item in items] == ["旧参数备注", comment]
        assert [item["status"] for item in items] == ["error", "returned"]
        assert json.loads(items[0]["details"]["arguments"])["reason"] == "旧参数备注"
        assert json.loads(items[1]["details"]["arguments"])["comment"] == comment
    finally:
        await service.close()


@pytest.mark.parametrize(
    "name,arguments,expected",
    [
        (
            "read_history",
            {"reason": "PRIVATE_INPUT_48c2515daf9742308a8b50777d39dd75"},
            ["缺少必填参数 ref", "不接受参数 reason"],
        ),
        (
            "read_history",
            {"ref": "H2", "48c2515daf9742308a8b50777d39dd75": "PRIVATE_INPUT"},
            ["不接受参数 @D", "请删除"],
        ),
        (
            "new_context",
            {"handoff": handoff("不得换窗"), "keep_recent": 99},
            ["参数 keep_recent 必须小于或等于 12"],
        ),
        (
            "write_note",
            {"key": "test", "content": "未接受", "kind": "wrong"},
            ["参数 kind 必须取允许值", "fact", "hypothesis", "plan"],
        ),
    ],
)
async def test_argument_errors_explain_repairs_without_executing_or_echoing_input(
    story, name, arguments, expected
):
    store, session = story
    service = runtime(store)
    model = Model((name, arguments), "继续正文")
    try:
        result, current = await turn(service, session, model)
        assert result["status"] == "committed", result["error"]
        feedback = json.loads(
            next(m["content"] for m in model.requests[1]["messages"] if m["role"] == "tool")
        )
        assert feedback["error"]["message"].startswith(f"{name} 参数错误，工具未执行：")
        assert all(text in feedback["error"]["message"] for text in expected)
        schema = feedback["expected_parameters"]
        original = SPECS[name][0].model_json_schema()
        assert set(schema["properties"]) == set(original["properties"])
        assert schema.get("required", []) == original.get("required", [])
        assert schema["additionalProperties"] is False
        encoded = json.dumps(feedback, ensure_ascii=False)
        assert all(
            text not in encoded
            for text in (
                "errors.pydantic.dev",
                "validation error",
                "input_value",
                "input_type",
                "PRIVATE_INPUT",
                "48c2515daf9742308a8b50777d39dd75",
                # Schema metadata titles; a chapter's own "title" property is a parameter.
                '"title": "',
            )
        )
        for key in ("handoff", "notes", "window"):
            assert current["snapshot"]["full_agent"][key] == session["snapshot"]["full_agent"][key]
        event = next(e for e in store.events(result["id"]) if e["kind"] == "full_agent_tool")
        assert event["payload"]["result"] == feedback
    finally:
        await service.close()


async def test_read_history_can_correct_missing_ref_after_actionable_feedback(story):
    store, session = story
    service = runtime(store)
    model = Model(
        ("read_history", {"reason": "核对H2内容"}), ("read_history", {"ref": "H2"}), "核对后继续正文"
    )
    try:
        result, current = await turn(service, session, model)
        assert result["status"] == "committed", result["error"]
        returned = [json.loads(m["content"]) for m in model.requests[-1]["messages"] if m["role"] == "tool"]
        assert "缺少必填参数 ref" in returned[0]["error"]["message"]
        assert returned[0]["expected_parameters"]["required"] == ["ref"]
        assert set(returned[0]["expected_parameters"]["properties"]) == {"ref"}
        assert returned[0]["expected_parameters"]["properties"]["ref"]["type"] == "string"
        assert "error" not in returned[1] and returned[1]["data"]["ref"] == "H2"
        assert returned[1]["data"]["text"] == "我答应明天在钟楼见面。"
        assert current["messages"][-1]["raw"] == "核对后继续正文"
    finally:
        await service.close()


@pytest.mark.parametrize("name", ["search_lore", "search_read_lore", "search_lore_entries", "unknown_tool"])
async def test_unknown_tool_errors_identify_missing_name_and_allow_retry(story, name):
    store, session = story
    service = runtime(store)
    model = Model((name, {"query": "灯塔"}), ("recall_lore", {"query": "灯塔"}), "继续。")
    try:
        result, current = await turn(service, session, model)
        assert result["status"] == "committed", result["error"]
        assert name not in {t["function"]["name"] for t in model.requests[0]["extra_body"]["tools"]}
        output = [json.loads(m["content"]) for m in model.requests[-1]["messages"] if m["role"] == "tool"]
        assert output[0]["ok"] is False
        assert output[0]["error"]["code"] == "UNKNOWN_TOOL"
        assert name in output[0]["error"]["message"]
        assert output[1]["data"]["matches"]
        event = next(e for e in store.events(result["id"]) if e["kind"] == "full_agent_tool")
        assert event["payload"]["result"] == output[0]
        for key in ("handoff", "notes", "window"):
            assert current["snapshot"]["full_agent"][key] == session["snapshot"]["full_agent"][key]
    finally:
        await service.close()


@pytest.mark.parametrize("old_name", ["search_lore", "search_read_lore", "search_lore_entries", "read_lore"])
async def test_retired_tool_names_are_not_accepted_in_settings(story, old_name):
    store, session = story
    app = create_app(store.root)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
            response = await client.put(
                f"/api/sessions/{session['id']}/full-agent/tools",
                json={"expected_revision": 0, "disabled_tools": [old_name]},
            )
            assert response.status_code == 400
            assert store.session(session["id"])["disabled_tools"] == []
    finally:
        await app.state.service.close()


@pytest.mark.parametrize("tool", ["patch_state"])
async def test_required_mvu_update_rejects_disabled_dependency(story, tool):
    store, session = story
    with store.connection() as db:
        db.execute("UPDATE sessions SET disabled_tools=? WHERE id=?", (json.dumps([tool]), session["id"]))
    service = runtime(store)
    try:
        with pytest.raises(ValueError, match=f"启用 {tool}"):
            service.submit(
                session["id"],
                TurnRequest(
                    text="继续", operation_id="mvu-disabled", expected_revision=0, update_variables=True
                ),
            )
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
                assert tool["enabled"] == (tool["name"] != "write_plot")
                assert not re.search(r"[\u4e00-\u9fff]", json.dumps(tool, ensure_ascii=False))
                assert all(p.get("description") for p in tool["parameters"]["properties"].values())
            lore = next(t for t in catalog if t["name"] == "recall_lore")
            assert lore["result_options"] == {"include_ref": False, "include_title": False}
            assert set(lore["parameters"]["properties"]) == {"query"}
            patch = next(t for t in catalog if t["name"] == "patch_state")
            assert not patch["available"]
            authorized = (await client.get(url, params={"update_variables": "true"})).json()["tools"]
            assert next(t for t in authorized if t["name"] == "patch_state")["available"]
            response = await client.put(
                url, json={"expected_revision": 0, "disabled_tools": ["write_handoff"]}
            )
            assert response.status_code == 200
            session = response.json()
            assert Store(store.root).session(session["id"])["disabled_tools"] == ["write_handoff"]
            assert (
                await client.put(url, json={"expected_revision": 0, "disabled_tools": []})
            ).status_code == 409
            assert (
                await client.put(url, json={"expected_revision": 1, "disabled_tools": ["unknown"]})
            ).status_code == 400
            preview = (
                await client.post(f"/api/sessions/{session['id']}/preview", json={"text": "继续"})
            ).json()
            assert "write_handoff" not in {t["function"]["name"] for t in preview["tools"]}
            model = Model(("write_handoff", handoff("禁止写入", through=None)), "正文")
            result, session = await turn(service, session, model)
            assert result["status"] == "committed", result["error"]
            assert not current_text(session["snapshot"]["full_agent"])
            assert "write_handoff" not in {
                t["function"]["name"] for t in model.requests[0]["extra_body"]["tools"]
            }
            feedback = json.loads(
                next(m["content"] for m in model.requests[1]["messages"] if m["role"] == "tool")
            )
            assert feedback["ok"] is False
            assert feedback["error"]["code"] == "PERMISSION_DENIED"
            assert feedback["error"]["retryable"] is False
            fork = store.fork_session(session["id"], session["head"], session["revision"])
            assert fork["disabled_tools"] == ["write_handoff"]
            result, session = await turn(service, session, Model("重生成"), mode="regenerate")
            assert result["status"] == "committed"
            assert session["disabled_tools"] == ["write_handoff"]
            response = await client.put(
                url,
                json={
                    "expected_revision": session["revision"],
                    "disabled_tools": [name for name in SPECS if name != "write_plot"],
                },
            )
            assert response.status_code == 200, response.text
            session = response.json()
            no_tools = Model("不使用工具的正文")
            result, session = await turn(service, session, no_tools)
            assert result["status"] == "committed", result["error"]
            assert "tools" not in no_tools.requests[0]["extra_body"]
            response = await client.put(
                url, json={"expected_revision": session["revision"], "disabled_tools": []}
            )
            session = response.json()
            assert session["disabled_tools"] == []
            restored_tools = (await client.post(preview_url, json={"text": "继续"})).json()["tools"]
            assert json.dumps(restored_tools, ensure_ascii=False) == json.dumps(
                original_tools, ensure_ascii=False
            )
            tid, _ = store.create_turn(
                session["id"], TurnRequest(operation_id="pending", expected_revision=session["revision"])
            )
            assert (
                await client.put(url, json={"expected_revision": session["revision"], "disabled_tools": []})
            ).status_code == 409
            store.set_turn(tid, status="cancelled")
            half = store.new_session(SessionCreate(card_id=session["card"]))
            assert (await client.get(f"/api/sessions/{half['id']}/full-agent/tools")).status_code == 400
    finally:
        await service.close()
        await app.state.service.close()


async def test_user_result_options_save_and_control_tool_output(tmp_path):
    store = Store(tmp_path)
    card = store.import_card(
        json.dumps(
            {
                "name": "港口",
                "first_mes": "开场。",
                "character_book": {
                    "entries": [
                        {
                            "id": "port",
                            "keys": ["旧港"],
                            "comment": "旧港",
                            "content": "旧港有一艘船。",
                            "enabled": True,
                        }
                    ]
                },
            }
        ).encode(),
        "card.json",
    )
    settings = store.settings()
    settings.retrieval.channels = ["keyword"]
    settings.full_agent_config.auto_retrieval = False
    store.save_settings(settings)
    session = store.new_session(SessionCreate(card_id=card["id"], agent_mode="full_agent"))
    app = create_app(store.root)
    service = runtime(store)
    url = f"/api/sessions/{session['id']}/full-agent/tools"
    options = {
        "recall_lore": {"include_ref": True, "include_title": True},
        "search_history": {"include_preview": False},
        "read_history": {"include_metadata": False},
    }
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
            assert (
                await client.put(
                    url,
                    json={
                        "expected_revision": 0,
                        "disabled_tools": [],
                        "tool_result_options": {"recall_lore": {"unknown": True}},
                    },
                )
            ).status_code == 400
            response = await client.put(
                url, json={"expected_revision": 0, "disabled_tools": [], "tool_result_options": options}
            )
            assert response.status_code == 200
            session = response.json()
            assert session["tool_result_options"] == options
            catalog = (await client.get(url)).json()["tools"]
            assert (
                next(t for t in catalog if t["name"] == "recall_lore")["result_options"]
                == options["recall_lore"]
            )
            fork = store.fork_session(session["id"], session["head"], session["revision"])
            assert fork["tool_result_options"] == options
            model = Model(
                ("recall_lore", {"query": "旧港"}),
                ("search_history", {"query": "开场"}),
                ("read_history", {"ref": "H1"}),
                ("read_history", {"ref": "H1-H2"}),
                "继续。",
            )
            result, _ = await turn(service, session, model)
            assert result["status"] == "committed", result["error"]
            output = [json.loads(m["content"]) for m in model.requests[-1]["messages"] if m["role"] == "tool"]
            assert output[0]["data"]["matches"] == [
                {"ref": "L1", "title": "旧港", "text": "<lorebook>\n旧港有一艘船。\n</lorebook>"}
            ]
            assert "preview" not in output[1]["data"]["matches"][0]
            assert "offset" not in output[1]["data"]["matches"][0]
            assert set(output[2]["data"]) == {"text"}
            # The switch applies to every message of a span, including the current input.
            assert [set(m) for m in output[3]["data"]["messages"]] == [{"text"}, {"text"}]
    finally:
        await service.close()
        await app.state.service.close()


async def test_disabled_rollover_fails_before_requesting_unavailable_tool(story, monkeypatch):
    store, session = story
    with store.connection() as db:
        db.execute(
            "UPDATE sessions SET disabled_tools=? WHERE id=?", (json.dumps(["new_context"]), session["id"])
        )
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
    definitions = tool_definitions(
        TurnRequest(operation_id="guidance", expected_revision=0, allow_content_edit=True),
        {"policy_effective": "auto"},
        document=document,
    )
    catalog = {item["function"]["name"]: item["function"] for item in definitions}
    assert "custom_field" not in json.dumps(catalog)
    assert set(catalog["read_character"]["parameters"]["properties"]["ref"]["enum"]) == {
        "name",
        "description",
        "personality",
        "scenario",
        "mes_example",
        "system_prompt",
        "post_history_instructions",
    }
    owner = SimpleNamespace(
        document=document,
        definitions=definitions,
        state=new_state(),
        snapshot={},
        session={},
        model_view=ModelView(document),
        tid="guidance",
    )
    tools = AgentTools(owner)
    before = await tools.execute("read_character", json.dumps({"ref": "description"}))
    assert before["data"]["text"] == "Before"
    await tools.execute(
        "revise_content",
        json.dumps(
            {
                "operations": [{"op": "set_character", "field": "description", "value": "After"}],
                "comment": "Update setting",
            }
        ),
    )
    after = await tools.execute("read_character", json.dumps({"ref": "description"}))
    assert after["data"]["text"] == "After"


def test_handoff_chapters_cover_sources_and_reject_old_formats():
    from tavern.core.models import Settings
    from tavern.full_agent.context import compile_window
    from tavern.full_agent.state import full_state, new_state

    history = [{"id": f"m{i}", "role": ("assistant", "user")[i % 2], "raw": f"原文{i}"} for i in range(6)]
    empty = {"chapters": [], "current": [], "revision": 0}

    def write(handoff, chapters, current="仍在港口"):
        return apply_write(
            handoff,
            HandoffWrite.model_validate(
                {"chapters": chapters, "current": [{"text": current, "refs": ["H6"]}]}
            ),
            history,
        )

    def point(text, *refs):
        return {"text": text, "refs": list(refs)}

    first = write(
        empty, [{"op": "add", "through": "H3", "title": "初到", "points": [point("借出戒指", "H2")]}]
    )
    with pytest.raises(HandoffError, match="不在所属章节"):
        write(first, [{"op": "add", "through": "H6", "title": "夜航", "points": [point("上船", "H2")]}])
    with pytest.raises(HandoffError) as gap:
        require_coverage(first, history, 6)
    assert gap.value.code == "HANDOFF_COVERAGE" and "H4-H6" in str(gap.value)
    both = write(first, [{"op": "add", "through": "H6", "title": "夜航", "points": [point("上船", "H5-H6")]}])
    require_coverage(both, history, 6)
    # Without through, the Runtime endpoint may leave nothing new to archive.
    with pytest.raises(HandoffError, match="章节已覆盖到 H6.*删去这个 add"):
        write(both, [{"op": "add", "title": "无新内容", "points": [point("无")]}])
    # Merge is the only way old chapters shrink; spans stay addressable.
    merged = write(
        both,
        [
            {
                "op": "merge",
                "chapters": ["C1", "C2"],
                "title": "港口",
                "points": [point("借戒指后上船", "H2", "H5-H6")],
            }
        ],
    )
    text = render_text(merged, history)
    assert text == "## C1 港口 [H1-H6]\n- 借戒指后上船 [H2, H5-H6]\n\n## 当前交接\n- 仍在港口 [H6]"
    # Read-only rendering preserves the model's complete prose without an
    # intermediate editable grammar or converting literal text into references.
    unusual = apply_write(
        empty,
        HandoffWrite(
            chapters=[
                {
                    "op": "add",
                    "title": '两行\n标题 "港口"',
                    "points": [
                        {"text": "第一行\n第二行"},
                        {"text": "招呼 [Hello]"},
                        {"text": "字面量 [H2]"},
                        {"text": "（推断）这是引文", "kind": "inference", "refs": ["H2"]},
                    ],
                }
            ],
            current=[{"text": '"原话"\n仍保留', "refs": ["H3"]}, {"text": "（推断）只是文字"}],
        ),
        history,
    )
    rendered = render_text(unusual, history)
    assert '两行\n标题 "港口"' in rendered
    assert "第一行\n第二行" in rendered
    assert "招呼 [Hello]" in rendered and "字面量 [H2]" in rendered
    assert '"原话"\n仍保留' in rendered
    assert not unusual["chapters"][0]["points"][2]["refs"]
    assert render_written_current(
        [{"text": '"原话"\n仍保留', "refs": ["H3"]}, {"text": "（推断）只是文字"}]
    ) == render_text({"chapters": [], "current": unusual["current"]}, history)
    # Unreleased v2 development states are rejected like v1, not migrated.
    legacy = new_state()
    legacy["version"] = 2
    with pytest.raises(ValueError, match="格式版本 2 不受支持"):
        full_state({"full_agent": legacy})
    revised = write(
        merged,
        [{"op": "revise", "chapter": "C1", "title": "港口", "points": [point("核对后的经过", "H2")]}],
    )
    assert revised["chapters"][0]["id"] == merged["chapters"][0]["id"]
    # Ordinary writes contain no mechanical H fields; Runtime supplies the exiting
    # range, while current can carry this turn's instructions without a fake source.
    plain = HandoffWrite.model_validate(
        {
            "chapters": [
                {"op": "add", "title": "港口", "points": [{"text": "已借出戒指"}, {"text": "船票仍未找到"}]}
            ],
            "current": [{"text": "按本轮用户要求保持第一人称"}],
        }
    )
    automatic = apply_write(empty, plain, history, archive_through=3)
    require_coverage(automatic, history, 3)
    assert automatic["chapters"][0]["end"] == history[2]["id"]
    assert (
        render_written_current(plain.model_dump()["current"]) == "## 当前交接\n- 按本轮用户要求保持第一人称"
    )
    plain_text = render_text(automatic, history)
    assert (
        plain_text
        == "## C1 港口 [H1-H3]\n- 已借出戒指\n- 船票仍未找到\n\n## 当前交接\n- 按本轮用户要求保持第一人称"
    )
    # Full-source replay must drop both handoff frames even when the baseline
    # still matches (local -> cloud -> local with no intervening story request).
    state = new_state()
    state["window"]["number"] = 2
    state["handoff"] = automatic
    session, settings = {"id": "story", "persona": "User", "messages": history}, Settings()
    snapshot = {"state": {}, "full_agent": state}
    compiled = compile_window({}, session, history, snapshot, "", settings, [])
    trace = [
        {"message_id": history[-1]["id"], "window_id": state["window"]["id"], "message": item}
        for item in compiled["context_additions"]
    ]
    replay = compile_window({}, session, history, snapshot, "", settings, [], trace=trace, replay_all=True)
    assert replay["context_ledger"]["rebuild_reason"] is None
    assert not any(m.get("source") in ("handoff", "handoff_chapters") for m in replay["trace"])


async def test_new_context_reuses_handoff_and_checks_only_exiting_history():
    import copy
    from types import SimpleNamespace

    from tavern.core.model_view import ModelView
    from tavern.core.models import Settings
    from tavern.full_agent.protocol import ToolError
    from tavern.full_agent.state import new_state
    from tavern.full_agent.tools import AgentTools, tool_definitions

    # H1 is the greeting; H2-H7 are three complete user/assistant exchanges.
    history = [{"id": f"m{i}", "role": ("assistant", "user")[i % 2], "raw": f"原文{i}"} for i in range(7)]
    request = TurnRequest(operation_id="optional-handoff", expected_revision=0)
    owner = SimpleNamespace(
        state=new_state(),
        history=history,
        settings=Settings(),
        definitions=tool_definitions(request, {}),
        model_view=ModelView({}),
    )
    tools = AgentTools(owner)
    # Keeping all story messages needs no summary and must not invent one.
    empty = copy.deepcopy(owner.state["handoff"])
    await tools.execute("new_context", '{"keep_recent":7}')
    assert owner.state["window"]["start"] == 0
    assert owner.state["handoff"] == empty
    await tools.execute(
        "write_handoff",
        json.dumps(
            {
                "chapters": [
                    {"op": "add", "through": "H5", "title": "先前经历", "points": [{"text": "旧事已记录"}]}
                ],
                "current": [{"text": "继续最新交流"}],
            }
        ),
    )
    before = copy.deepcopy(owner.state["handoff"])
    # keep_recent=1 expands to H6-H7, so coverage through H5 is sufficient.
    await tools.execute("new_context", '{"keep_recent":1}')
    assert owner.state["window"]["start"] == 5
    assert owner.state["handoff"] == before
    # Retiring the previously retained exchange does require a chapter.
    unchanged = copy.deepcopy(owner.state)
    with pytest.raises(ToolError) as gap:
        await tools.execute("new_context", '{"keep_recent":0}')
    assert gap.value.result["error"]["code"] == "HANDOFF_COVERAGE"
    assert "H6-H7" in str(gap.value)
    assert owner.state == unchanged
