import asyncio
import json
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from kohakuterrarium.testing.llm import ScriptedLLM, ScriptEntry

from tavern.application.api import create_app
from tavern.application.service import RoleplayRuntime
from tavern.core.models import SessionCreate, TurnRequest
from tavern.storage.store import Store
from tests.support.memory_fixtures import memory_output


async def test_message_edit_reapplies_card_formatting_without_state_changes(tmp_path):
    app = create_app(tmp_path)
    store = app.state.store
    document = json.loads(Path("samples/雾港来信.json").read_text(encoding="utf-8"))
    document["data"]["extensions"]["regex_scripts"] = [
        {"findRegex": r"/<UpdateVariable>[\s\S]*?<\/UpdateVariable>/g",
         "replaceString": "", "placement": [2], "markdownOnly": True},
        {"findRegex": r"/<secret>[\s\S]*?<\/secret>/g",
         "replaceString": "", "placement": [2], "promptOnly": True},
    ]
    card = store.import_card(json.dumps(document).encode(), "card.json")
    session = store.new_session(SessionCreate(card_id=card["id"]))
    message = session["messages"][-1]
    text = "**修改正文**\n<UpdateVariable>_.set('能量', 999);</UpdateVariable><secret>隐藏设定</secret>"
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as api:
            response = await api.put(
                f"/api/sessions/{session['id']}/messages/{message['id']}",
                json={"text": text, "expected_revision": 0},
            )
            assert response.status_code == 200
            edited = response.json()["messages"][-1]
            assert edited["raw"] == text
            assert edited["display"] == "**修改正文**\n<secret>隐藏设定</secret>"
            assert edited["model"] == "**修改正文**\n<UpdateVariable>_.set('能量', 999);</UpdateVariable>"
            assert edited["snapshot"]["state"] == message["snapshot"]["state"]
            assert edited["snapshot"]["scopes"] == message["snapshot"]["scopes"]
            assert response.json()["revision"] == 1
            saved = await api.get(f"/api/sessions/{session['id']}")
            assert saved.json()["messages"][-1]["display"] == edited["display"]
    finally:
        await app.state.service.close()


async def test_missing_model_fails_before_creating_turn_or_message(tmp_path):
    store = Store(tmp_path)
    card = store.import_card(Path("samples/雾港来信.json").read_bytes(), "card.json")
    session = store.new_session(SessionCreate(card_id=card["id"]))
    service = RoleplayRuntime(store)
    try:
        with pytest.raises(ValueError, match="模型名称"):
            service.submit(
                session["id"], TurnRequest(text="不会写入", operation_id="missing", expected_revision=0)
            )
        assert len(store.session(session["id"])["messages"]) == 1
        with store.connection() as db:
            assert db.execute("SELECT count(*) FROM turns").fetchone()[0] == 0
    finally:
        await service.close()


async def test_delete_session_removes_all_owned_state_and_run_files(tmp_path):
    store = Store(tmp_path)
    card = store.import_card(Path("samples/雾港来信.json").read_bytes(), "card.json")
    session = store.new_session(SessionCreate(card_id=card["id"]))
    turn_id, _ = store.create_turn(
        session["id"], TurnRequest(text="测试", operation_id="delete", expected_revision=0)
    )
    store.event(turn_id, "usage", {"usage": {"prompt_tokens": 10}})
    with store.connection() as db:
        db.execute(
            "INSERT INTO changes VALUES (?,?,?,?,?,?)",
            ("change", turn_id, session["head"], "{}", "{}", "test"),
        )
    runs = tmp_path / "runs"
    runs.mkdir()
    for suffix in (".kohakutr", ".kohakutr.drives", ".kohakutr.lock"):
        (runs / f"{turn_id}{suffix}").write_text("owned", encoding="utf-8")
    service = RoleplayRuntime(store)
    try:
        await service.delete_session(session["id"])
    finally:
        await service.close()
    assert not list(runs.glob(f"{turn_id}*"))
    with store.connection() as db:
        for table in ("sessions", "messages", "turns", "events", "changes"):
            assert db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0


async def test_real_kt_turn_and_branch_restore(tmp_path):
    store = Store(tmp_path)
    card = store.import_card(Path("samples/星航状态卡.json").read_bytes(), "card.json")
    with store.connection() as db:
        db.execute("UPDATE cards SET permission=1 WHERE id=?", (card["id"],))
    session = store.new_session(SessionCreate(card_id=card["id"]))
    service = RoleplayRuntime(store)
    service.provider_factory = lambda: ScriptedLLM(["洛伊领你走向观测窗。"])
    service.variable_provider_factory = lambda: ScriptedLLM(['[{"op":"replace","path":"/能量","value":4}]'])
    try:
        t = service.submit(
            session["id"], TurnRequest(text="走向观测窗", operation_id="a", expected_revision=0, update_variables=True)
        )
        await service.tasks[t["id"]]
        final = store.turn(t["id"])
        assert final["status"] == "committed", final.get("error")
        current = store.session(session["id"])
        assert current["snapshot"]["state"]["stat_data"]["能量"] == 4
        first_head = current["head"]
        assert "_.add" not in current["messages"][-1]["display"]
        t2 = service.submit(
            session["id"], TurnRequest(mode="regenerate", operation_id="b", expected_revision=1, update_variables=True)
        )
        await service.tasks[t2["id"]]
        assert store.turn(t2["id"])["status"] == "committed", store.turn(t2["id"])["error"]
        assert store.session(session["id"])["snapshot"]["state"]["stat_data"]["能量"] == 4
        switched = store.update_session(session["id"], 2, head=first_head)
        assert switched["snapshot"]["state"]["stat_data"]["能量"] == 4
        assert list((tmp_path / "runs").glob("*.kohakutr"))
    finally:
        await service.close()


async def test_script_failure_rolls_back(tmp_path):
    store = Store(tmp_path)
    doc = json.loads(Path("samples/星航状态卡.json").read_text(encoding="utf-8"))
    doc["data"]["extensions"]["tavern_helper"]["scripts"][0]["content"] = (
        "eventOn(Mvu.events.VARIABLE_UPDATE_ENDED, () => {throw Error('expected failure')});"
    )
    card = store.import_card(json.dumps(doc).encode(), "card.json")
    with store.connection() as db:
        db.execute("UPDATE cards SET permission=1 WHERE id=?", (card["id"],))
    session = store.new_session(SessionCreate(card_id=card["id"]))
    service = RoleplayRuntime(store)
    service.provider_factory = lambda: ScriptedLLM(["草稿"])
    service.variable_provider_factory = lambda: ScriptedLLM(['[{"op":"replace","path":"/能量","value":4}]'])
    try:
        t = service.submit(session["id"], TurnRequest(text="继续", operation_id="a", expected_revision=0, update_variables=True))
        await service.tasks[t["id"]]
        assert store.turn(t["id"])["status"] == "error"
        assert "草稿" in store.turn(t["id"])["draft"]
        assert store.session(session["id"])["head"] == session["head"]
    finally:
        await service.close()


async def test_api_local_boundary_and_secret_redaction(tmp_path):
    app = create_app(tmp_path)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as c:
        assert (await c.get("/api/health")).status_code == 200
        assert (await c.put("/api/settings", json={"providers":[{"id":"default","api_key":"secret"}]})).status_code == 200
        response = await c.get("/api/settings")
        assert response.json()["has_api_key"]
        assert "secret" not in response.text
        assert (await c.get("/api/settings", headers={"origin": "null"})).status_code == 403
        assert (await c.get("/api/settings", headers={"host": "evil.example"})).status_code == 403


async def test_preview_edit_and_cancel_are_atomic(tmp_path):
    store = Store(tmp_path)
    c = store.import_card(Path("samples/雾港来信.json").read_bytes(), "card.json")
    s = store.new_session(SessionCreate(card_id=c["id"], policy="preview"))
    service = RoleplayRuntime(store)
    service.provider_factory = lambda: ScriptedLLM(["first draft"])
    try:
        t = service.submit(s["id"], TurnRequest(text="hello", operation_id="one", expected_revision=0))
        await service.tasks[t["id"]]
        assert store.turn(t["id"])["status"] == "awaiting_review"
        assert store.session(s["id"])["head"] == s["head"]
        service.review(t["id"], True)
        head = store.session(s["id"])["head"]
        current = store.edit_message(s["id"], head, "edited reply", 1)
        assert current["messages"][-1]["role"] == "assistant"
        assert current["messages"][-1]["raw"] == "edited reply"
        assert len(current["messages"]) == 3
        service.provider_factory = lambda: ScriptedLLM(
            [ScriptEntry(response="a" * 100, delay_per_chunk=0.3, chunk_size=1)]
        )
        t = service.submit(
            s["id"], TurnRequest(text="continue", operation_id="three", expected_revision=current["revision"])
        )
        for _ in range(100):
            if store.turn(t["id"])["draft"]:
                break
            await asyncio.sleep(0.05)
        await service.cancel(t["id"])
        assert store.turn(t["id"])["status"] == "cancelled"
        assert store.session(s["id"])["head"] == current["head"]
    finally:
        await service.close()


async def test_saved_provider_payload_retains_mid_system(tmp_path):
    import httpx
    from openai import AsyncOpenAI

    from tavern.conversation.context import compile_context
    from tavern.core.models import Settings
    from tavern.scripting.agent import make_provider

    settings = Settings(api_key="test", model="fixture", use_proxy=False)
    settings.capability = "accepted"
    settings.capability_key = f"{settings.base_url}|{settings.model}|chat-completions"
    captured = []
    provider = await make_provider(settings)
    await provider._client.close()

    def transport(request):
        captured.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "test",
                "object": "chat.completion",
                "created": 0,
                "model": "fixture",
                "choices": [
                    {"index": 0, "message": {"role": "assistant", "content": "OK"}, "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )

    provider._client = AsyncOpenAI(
        api_key="test", http_client=httpx.AsyncClient(transport=httpx.MockTransport(transport))
    )
    store = Store(tmp_path)
    document = {"name": "fixture", "description": "character", "first_mes": "opening",
                "character_book": {"entries": [
                    {"id": "1", "keys": ["marker"], "enabled": True, "constant": False,
                     "comment": "场景标记", "content": "mid-scene",
                     "extensions": {"position": 4, "depth": 2, "role": 0}},
                ]}}
    card = store.import_card(json.dumps(document).encode(), "fixture.json")
    session = store.new_session(SessionCreate(card_id=card["id"]))
    # Constants are pinned at the top; use a triggered ST-depth entry for this wire test.
    session["lore_injection_mode"] = "st_top"
    history = session["messages"] + [
        {"id": "user", "role": "user", "raw": "a", "model": "a"},
        {"id": "assistant", "role": "assistant", "raw": "b", "model": "b"},
    ]
    compiled = compile_context(document, session, history, session["snapshot"], "marker", settings, "wire")
    messages = compiled["messages"]
    index = next(i for i, item in enumerate(compiled["trace"]) if item["source"] == "lore:1")
    assert messages[index] == {"role": "system", "content": "mid-scene"}
    assert messages[index - 1]["content"] == "a"
    assert messages[index + 1]["content"] == "b"
    try:
        assert (await provider.chat_complete(messages)).content == "OK"
        assert captured[0]["messages"] == messages
    finally:
        await provider.close()


async def test_native_tool_loop_uses_branch_data_and_preserves_pairs(tmp_path):
    from kohakuterrarium.llm.base import NativeToolCall

    store = Store(tmp_path)
    card = store.import_card(Path("samples/雾港来信.json").read_bytes(), "card.json")
    session = store.new_session(SessionCreate(card_id=card["id"]))

    class ToolLLM(ScriptedLLM):
        def __init__(self):
            super().__init__([""])
            self.native = []
            self.schemas = []

        @property
        def last_tool_calls(self):
            return self.native

        async def chat(self, messages, **kwargs):
            self.call_log.append(messages)
            self.schemas.append(kwargs.get("tools", []))
            steps = [
                [("search_lore", {"query": ""}), ("tavern_recall_history", {"turn": 1, "radius": 0})],
                [("read_lore", {"entry_ref": "L1"}),
                 ("write_memory", {"content": "书店提供毛巾", "source_ref": "H1", "kind": "fact"})],
            ]
            if self.call_count < len(steps):
                self.native = [
                    NativeToolCall(id=f"call-{self.call_count}-{i}", name=name, arguments=json.dumps(args))
                    for i, (name, args) in enumerate(steps[self.call_count])
                ]
                self.call_count += 1
                yield ""
            else:
                self.native = []
                self.call_count += 1
                yield "我们去灯塔。"

    provider = ToolLLM()
    service = RoleplayRuntime(store)
    service.provider_factory = lambda: provider
    try:
        turn = service.submit(
            session["id"],
            TurnRequest(text="整理设定", operation_id="tools", expected_revision=0, allow_content_edit=True),
        )
        await service.tasks[turn["id"]]
        final = store.turn(turn["id"])
        assert final["status"] == "committed", final["error"]
        assert provider.call_count == 4
        current = store.session(session["id"])
        assert current["snapshot"]["memory_document"]["records"][0]["sources"][0]["message_id"] == session["head"]
        wire = provider.call_log[-2]
        assert len([m for m in wire if m.get("role") == "tool"]) == 4
        assert session["head"] not in json.dumps(wire)
        ids = {m["tool_call_id"] for m in wire if m.get("role") == "tool"}
        calls = {c["id"] for m in wire for c in m.get("tool_calls", [])}
        assert ids == calls
        names = {tool.name for batch in provider.schemas for tool in (batch or [])}
        assert "read_lore" in names
        assert not {"bash", "read", "write", "group_spawn"} & names
        assert not provider.schemas[-1]
        assert not any(m.get("role") == "tool" for m in provider.call_log[-1])
    finally:
        await service.close()


async def test_narrative_is_single_call_and_retry_reuses_user_node(tmp_path):
    store = Store(tmp_path)
    card = store.import_card(Path("samples/雾港来信.json").read_bytes(), "card.json")
    session = store.new_session(SessionCreate(card_id=card["id"]))

    class Narrator(ScriptedLLM):
        def __init__(self):
            super().__init__(["故事正文"])
            self.tools_seen = []

        async def chat(self, messages, **kwargs):
            self.tools_seen.append(kwargs.get("tools", []))
            async for chunk in super().chat(messages, **kwargs):
                yield chunk

    provider = Narrator()
    service = RoleplayRuntime(store)
    service.provider_factory = lambda: provider
    try:
        t = service.submit(session["id"], TurnRequest(text="你好", operation_id="a", expected_revision=0))
        await service.tasks[t["id"]]
        assert store.turn(t["id"])["status"] == "committed", store.turn(t["id"])["error"]
        old = store.session(session["id"])
        user_id = old["messages"][-2]["id"]
        t = service.submit(
            session["id"],
            TurnRequest(
                mode="regenerate", regenerate_note="更克制一些", operation_id="b", expected_revision=1
            ),
        )
        await service.tasks[t["id"]]
        new = store.session(session["id"])
        assert new["messages"][-2]["id"] == user_id
        assert new["messages"][-1]["parent"] == user_id
        assert new["messages"][-2]["raw"] == "你好"
        assert provider.call_count == 2 and all(not x for x in provider.tools_seen)
        assert any("更克制一些" in m["content"] for m in provider.call_log[-1])
        assert all("更克制一些" not in m["raw"] for m in new["messages"])
    finally:
        await service.close()


async def test_persona_details_are_pinned_and_used_in_context(tmp_path):
    from tavern.core.models import PersonaEdit

    store = Store(tmp_path)
    persona = store.save_persona(None, PersonaEdit(name="玩家甲", description="医生，温和，说话简短。"))
    card = store.import_card(Path("samples/雾港来信.json").read_bytes(), "card.json")
    s = store.new_session(SessionCreate(card_id=card["id"], persona_id=persona["id"]))
    assert s["persona"] == "玩家甲" and s["persona_data"]["description"].startswith("医生")
    store.save_persona(persona["id"], PersonaEdit(name="玩家乙", description="新描述", expected_version=1))
    assert store.session(s["id"])["persona"] == "玩家甲"


async def test_reasoning_strength_is_serialized_without_silent_fallback():
    import httpx
    from openai import AsyncOpenAI

    from tavern.core.models import Settings
    from tavern.scripting.agent import make_provider

    for effort, style in [
        ("default", "reasoning_effort"),
        ("high", "reasoning_effort"),
        ("none", "reasoning_effort"),
        ("low", "reasoning"),
    ]:
        provider = await make_provider(
            Settings(
                api_key="test",
                model="fixture",
                use_proxy=False,
                reasoning_effort=effort,
                reasoning_format=style,
            )
        )
        await provider._client.close()
        captured = []

        def route(request):
            captured.append(json.loads(request.content))
            return httpx.Response(
                200,
                json={
                    "id": "test",
                    "object": "chat.completion",
                    "created": 0,
                    "model": "fixture",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "ok"},
                            "finish_reason": "stop",
                        }
                    ],
                },
            )

        provider._client = AsyncOpenAI(
            api_key="test", http_client=httpx.AsyncClient(transport=httpx.MockTransport(route))
        )
        try:
            await provider.chat_complete([{"role": "user", "content": "hello"}])
            if effort == "default":
                assert "reasoning_effort" not in captured[0] and "reasoning" not in captured[0]
            elif style == "reasoning_effort":
                assert captured[0]["reasoning_effort"] == effort
            else:
                assert captured[0]["reasoning"] == {"effort": effort}
        finally:
            await provider.close()


@pytest.mark.parametrize("invalid_evidence", [False, True])
async def test_regeneration_reuses_the_committed_chunk_window(tmp_path, invalid_evidence):
    store = Store(tmp_path)
    cfg = store.settings().model_copy(update={"history_message_cap": 8, "chunk_size": 2, "memory_interval_turns": 1})
    store.save_settings(cfg)
    card = store.import_card(Path("samples/雾港来信.json").read_bytes(), "card.json")
    s = store.new_session(SessionCreate(card_id=card["id"]))
    service = RoleplayRuntime(store)
    service.provider_factory = lambda: ScriptedLLM(["剧情继续。"])
    class ArchiveModel(ScriptedLLM):
        async def chat_complete(self, messages, **kwargs):
            from types import SimpleNamespace
            self.call_count += 1
            result = json.loads(memory_output(messages, invalid=invalid_evidence))
            return SimpleNamespace(content=json.dumps(result), usage={})

    memory_model = ArchiveModel()
    service.memory_provider_factory = lambda: memory_model
    try:
        for i in range(4):
            s = store.session(s["id"])
            turn = service.submit(
                s["id"],
                TurnRequest(text=f"行动{i}", operation_id=f"send-{i}", expected_revision=s["revision"]),
            )
            await service.tasks[turn["id"]]
            if invalid_evidence and store.turn(turn["id"])["status"] == "error":
                assert store.session(s["id"])["head"] == s["head"]
                assert "context_window" not in store.session(s["id"])["snapshot"]
                return
            assert store.turn(turn["id"])["status"] == "committed", store.turn(turn["id"])["error"]
        assert not invalid_evidence, "Invalid critical sources must block the forced cut"
        before = store.session(s["id"])
        window = before["snapshot"]["context_window"]
        calls_before = memory_model.call_count
        assert calls_before >= 2
        user_id = before["messages"][-2]["id"]
        turn = service.submit(
            s["id"],
            TurnRequest(
                mode="regenerate",
                regenerate_note="换一种措辞",
                operation_id="retry",
                expected_revision=before["revision"],
            ),
        )
        await service.tasks[turn["id"]]
        after = store.session(s["id"])
        assert after["snapshot"]["context_window"] == window
        assert after["messages"][-2]["id"] == user_id
        assert memory_model.call_count == calls_before
        assert len(after["messages"]) == len(before["messages"])
    finally:
        await service.close()


async def test_invalid_model_variable_update_keeps_state_and_can_retry(tmp_path):
    store = Store(tmp_path)
    card = store.import_card(json.dumps({"name": "变量校验", "first_mes": "欢迎"}).encode(), "card.json")
    session = store.new_session(SessionCreate(card_id=card["id"]))
    state = {"stat_data": {"hp": 5}, "schema": {"type": "object", "extensible": False,
             "properties": {"hp": {"type": "number"}}}}
    with store.connection() as db:
        snapshot = session["snapshot"] | {"state": state}
        db.execute("UPDATE messages SET snapshot=? WHERE id=?", (json.dumps(snapshot), session["head"]))
    before = store.session(session["id"])
    service = RoleplayRuntime(store)
    service.provider_factory = lambda: ScriptedLLM(['失败草稿'])
    service.variable_provider_factory = lambda: ScriptedLLM(['[{"op":"add","path":"/unknown","value":1}]'])
    try:
        failed = service.submit(session["id"], TurnRequest(
            text="继续", operation_id="failed", expected_revision=before["revision"], update_variables=True))
        await service.tasks[failed["id"]]
        failed = store.turn(failed["id"])
        assert failed["status"] == "error"
        assert "草稿已保留" in failed["error"]
        assert "Page.evaluate" not in failed["error"]
        assert "失败草稿" in failed["draft"]
        unchanged = store.session(session["id"])
        assert unchanged["head"] == before["head"] and unchanged["snapshot"] == before["snapshot"]
        service.provider_factory = lambda: ScriptedLLM(["成功回复"])
        service.variable_provider_factory = lambda: ScriptedLLM(['[{"op":"replace","path":"/hp","value":6}]'])
        retried = service.submit(session["id"], TurnRequest(
            **(failed["request"] | {"operation_id": "retry", "expected_revision": unchanged["revision"]})))
        await service.tasks[retried["id"]]
        assert store.turn(retried["id"])["status"] == "committed"
        after = store.session(session["id"])
        assert len(after["messages"]) == len(before["messages"]) + 2
        assert after["snapshot"]["state"]["stat_data"]["hp"] == 6
    finally:
        await service.close()
