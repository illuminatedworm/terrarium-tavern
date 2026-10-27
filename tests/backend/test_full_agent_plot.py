"""Author outlines are optional, branch-local drafts, never story facts."""

import asyncio
import copy
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from tavern.application.api import create_app
from tavern.conversation.compaction import hashes
from tavern.core.message_adapter import AdapterRule
from tavern.core.models import SessionCreate, Settings, TurnRequest
from tavern.full_agent.activity import tool_item
from tavern.full_agent.context import compile_window, load_trace
from tavern.full_agent.state import full_state
from tavern.full_agent.tools import tool_definitions
from tavern.scripting.request_context import response_origin
from tavern.storage.store import Store, dump
from tavern.storage.transfer import import_package
from tests.backend import test_full_agent
from tests.backend.test_data_transfer import package
from tests.backend.test_full_agent import Model, handoff, runtime, turn

story = test_full_agent.story


def test_legacy_database_is_backed_up_before_additive_plot_migration(story):
    store, session = story
    original = copy.deepcopy(session["snapshot"])
    original["full_agent"].pop("plot", None)
    raw = store.settings_document().model_dump()
    raw["full_agent"]["tools"].pop("plot_tokens")
    raw["full_agent"]["system_prompt"] = Path("tests/fixtures/full_agent_prompt_before_plot.txt").read_text(
        encoding="utf-8"
    )
    with store.connection() as db:
        db.execute("ALTER TABLE sessions DROP COLUMN plot_enabled")
        db.execute("UPDATE messages SET snapshot=? WHERE id=?", (dump(original), session["head"]))
        db.execute("UPDATE meta SET value=? WHERE key='settings'", (dump(raw),))
    reopened = Store(store.root)
    assert not reopened.session(session["id"])["plot_enabled"]
    assert reopened.session(session["id"])["snapshot"] == original
    assert reopened.settings_document().full_agent.tools.plot_tokens == Settings().full_agent_config.plot_tokens
    backups = list(store.root.glob("before-upgrade-*.sqlite3"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as db:
        assert "plot_enabled" not in {r[1] for r in db.execute("PRAGMA table_info(sessions)")}
        assert json.loads(db.execute("SELECT value FROM meta WHERE key='settings'").fetchone()[0]) == raw
    Store(store.root)
    assert list(store.root.glob("before-upgrade-*.sqlite3")) == backups


def test_plot_activity_hides_spoilers_until_expanded():
    item = tool_item("p", 1, "write_plot", '{"content":"SPOILER_OUTLINE"}')
    assert item["summary"] == "包含未来剧情，展开查看"
    assert "SPOILER_OUTLINE" in item["details"]["arguments"]


def enable(store, session, text=""):
    snapshot = copy.deepcopy(session["snapshot"])
    full_state(snapshot)["plot"] = {"text": text, "revision": int(bool(text))}
    with store.connection() as db:
        db.execute("UPDATE sessions SET plot_enabled=1 WHERE id=?", (session["id"],))
        db.execute("UPDATE messages SET snapshot=? WHERE id=?", (dump(snapshot), session["head"]))
    return store.session(session["id"])


def outlines(messages):
    return [m["content"] for m in messages if m.get("content", "").startswith("<tavern_plot>\n")]


async def test_optional_plot_initial_gate_success_observation_and_no_per_turn_write(story):
    store, session = story
    assert session["plot_enabled"] is False
    service = runtime(store)
    try:
        disabled = Model(("write_plot", {"content": "UNAUTHORIZED_OUTLINE"}), "关闭时的正文")
        result, session = await turn(service, session, disabled)
        assert result["status"] == "committed", result["error"]
        assert not session["snapshot"]["full_agent"]["plot"]["text"]
        assert "write_plot" not in {
            t["function"]["name"] for t in disabled.requests[0]["extra_body"]["tools"]
        }
        assert any('"PERMISSION_DENIED"' in m.get("content", "") for m in disabled.requests[-1]["messages"])

        session = enable(store, session)
        model = Model(
            {
                "content": "先交付正文，等待大纲完成",
                "calls": [("write_plot", {"content": "近期港口交锋；远期依选择揭露旧信。"})],
            },
            ("read_state", {}),
            "",
        )
        result, session = await turn(service, session, model)
        assert result["status"] == "committed", result["error"]
        assert session["messages"][-1]["raw"] == "先交付正文，等待大纲完成"
        assert session["snapshot"]["full_agent"]["plot"] == {
            "text": "近期港口交锋；远期依选择揭露旧信。",
            "revision": 1,
        }
        assert len(model.requests) == 3
        assert (
            len(outlines(model.requests[-1]["messages"])) == 1
        )  # initial empty frame; accepted tool carries the update
        assert not session["snapshot"]["full_agent"]["notes"]
        follow = Model("接着写，无需每轮重写大纲。")
        result, current = await turn(service, session, follow)
        assert result["status"] == "committed", result["error"]
        assert current["snapshot"]["full_agent"]["plot"] == session["snapshot"]["full_agent"]["plot"]
    finally:
        await service.close()


async def test_failed_empty_and_oversized_plot_leave_current_draft_intact(story):
    store, session = story
    session = enable(store, session, "既有大纲")
    config = store.settings_document()
    config.full_agent.tools.plot_tokens = 128
    store.save_settings(config)
    service = runtime(store)
    try:
        model = Model(
            ("write_plot", {"content": "   "}), ("write_plot", {"content": "长大纲" * 1000}), "正文"
        )
        result, current = await turn(service, session, model)
        assert result["status"] == "committed", result["error"]
        errors = [
            e["payload"]["result"]["error"]["code"]
            for e in store.events(result["id"])
            if e["kind"] == "full_agent_tool" and "error" in e["payload"]["result"]
        ]
        assert errors == ["CONTENT_LIMIT", "CONTENT_LIMIT"]
        assert current["snapshot"]["full_agent"]["plot"] == session["snapshot"]["full_agent"]["plot"]
    finally:
        await service.close()


async def test_plot_failure_reopen_resume_and_rollover(story):
    store, session = story
    session = enable(store, session)
    service = runtime(store)
    try:
        result, unchanged = await turn(
            service,
            session,
            Model(
                ("write_plot", {"content": "独立保存的未来大纲"}),
                ("new_context", {"handoff": handoff("当前在港口。"), "keep_recent": 0}),
                RuntimeError("disconnected"),
            ),
        )
        assert result["status"] == "error"
        assert unchanged["snapshot"] == session["snapshot"]
        assert (
            store.full_agent_progress(result["id"])["snapshot"]["full_agent"]["plot"]["text"]
            == "独立保存的未来大纲"
        )
    finally:
        await service.close()
    reopened = Store(store.root)
    service = runtime(reopened)
    try:
        model = Model("恢复后的正文")
        service.full.provider_factory = lambda: model
        resumed = service.submit(
            session["id"],
            TurnRequest(
                text=result["request"]["text"],
                operation_id="resume-plot",
                expected_revision=session["revision"],
                resume_turn_id=result["id"],
            ),
        )
        await service.tasks[resumed["id"]]
        assert reopened.turn(resumed["id"])["status"] == "committed"
        assert len(model.requests) == 1
        assert any("独立保存的未来大纲" in text for text in outlines(model.requests[0]["messages"]))
        assert not any(m.get("tool_calls") for m in model.requests[0]["messages"])
        assert reopened.session(session["id"])["snapshot"]["full_agent"]["plot"]["revision"] == 1
    finally:
        await service.close()


async def test_cancelling_after_plot_write_does_not_commit(story):
    store, session = story
    session = enable(store, session, "已提交的大纲")
    service = runtime(store)
    waiting = asyncio.Event()

    class WaitingModel(Model):
        async def chat_complete(self, *args, **kwargs):
            if self.requests:
                waiting.set()
                await asyncio.Event().wait()
            return await super().chat_complete(*args, **kwargs)

    try:
        service.full.provider_factory = lambda: WaitingModel(("write_plot", {"content": "取消的草稿"}))
        pending = service.submit(
            session["id"], TurnRequest(text="继续", operation_id="cancel-plot", expected_revision=0)
        )
        await asyncio.wait_for(waiting.wait(), timeout=10)
        await service.cancel(pending["id"])
        assert store.turn(pending["id"])["status"] == "cancelled"
        assert store.session(session["id"])["snapshot"] == session["snapshot"]
    finally:
        await service.close()


@pytest.mark.parametrize("accept", [True, False])
async def test_plot_follows_whole_turn_review(story, accept):
    store, session = story
    session = enable(store, session, "审核前大纲")
    with store.connection() as db:
        db.execute("UPDATE sessions SET policy='review' WHERE id=?", (session["id"],))
    service = runtime(store)
    try:
        result, current = await turn(
            service,
            session,
            Model(
                ("write_plot", {"content": "待审核大纲"}),
                ("read_character", {"ref": "description"}),
                (
                    "revise_content",
                    {
                        "operations": [{"op": "set_character", "field": "description", "value": "新描述"}],
                        "comment": "调整角色",
                    },
                ),
                "待审核正文",
            ),
            allow_content_edit=True,
        )
        assert result["status"] == "awaiting_review", result["error"]
        assert current["snapshot"] == session["snapshot"]
        service.review(result["id"], accept)
        current = store.session(session["id"])
        assert current["snapshot"]["full_agent"]["plot"]["text"] == ("待审核大纲" if accept else "审核前大纲")
        if not accept:
            assert store.full_agent_progress(result["id"]) is None
    finally:
        await service.close()


async def test_plot_regeneration_fork_export_and_legacy_import(story, tmp_path):
    store, session = story
    session = enable(store, session, "生成前大纲")
    service = runtime(store)
    try:
        result, first = await turn(service, session, Model(("write_plot", {"content": "候选甲"}), "正文甲"))
        assert result["status"] == "committed", result["error"]
        model = Model(("write_plot", {"content": "候选乙"}), "正文乙")
        result, second = await turn(service, first, model, mode="regenerate")
        assert result["status"] == "committed", result["error"]
        assert "生成前大纲" in str(model.requests[0]["messages"])
        assert "候选甲" not in str(model.requests[0]["messages"])
        assert second["snapshot"]["full_agent"]["plot"]["revision"] == 2
        fork = store.fork_session(second["id"], first["head"], second["revision"])
        assert fork["plot_enabled"] and fork["snapshot"]["full_agent"]["plot"]["text"] == "候选甲"
        with store.connection() as db:
            db.execute("UPDATE sessions SET plot_enabled=0 WHERE id=?", (fork["id"],))
        assert store.session(second["id"])["plot_enabled"]

        payload, blobs = package(store, settings=False, sessions=[second["id"]])
        target = Store(tmp_path / "imported")
        imported = import_package(target, payload, blobs)
        restored = target.session(imported["sessions"][0])
        assert restored["plot_enabled"]
        assert restored["snapshot"]["full_agent"]["plot"] == second["snapshot"]["full_agent"]["plot"]
        payload["sessions"][0]["session"].pop("plot_enabled")
        for message in payload["sessions"][0]["messages"]:
            message["snapshot"]["full_agent"].pop("plot", None)
        imported = import_package(target, payload, blobs)
        legacy = target.session(imported["sessions"][0])
        assert not legacy["plot_enabled"]
        assert full_state(legacy["snapshot"])["plot"] == {"text": "", "revision": 0}
    finally:
        await service.close()


async def test_plot_api_edits_pause_clear_catalog_and_conflicts(story):
    store, session = story
    app = create_app(store.root)
    path = f"/api/sessions/{session['id']}/full-agent"
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
            state = (await client.get(path)).json()
            assert not state["plot_enabled"] and state["plot"] == {"text": "", "revision": 0}
            assert state["plot_tokens"] == store.settings("full_agent").full_agent_config.plot_tokens
            response = await client.put(
                path + "/plot", json={"expected_revision": 0, "enabled": True, "content": "玩家大纲"}
            )
            assert response.status_code == 200, response.text
            current = response.json()
            catalog = (await client.get(path + "/tools")).json()["tools"]
            tool = next(t for t in catalog if t["name"] == "write_plot")
            assert tool["enabled"] and tool["available"] and tool["controlled_by"] == "plot_enabled"
            assert (
                await client.put(
                    path + "/tools",
                    json={"expected_revision": current["revision"], "disabled_tools": ["write_plot"]},
                )
            ).status_code == 400
            assert (
                await client.put(path + "/plot", json={"expected_revision": 0, "content": "覆盖竞争"})
            ).status_code == 409
            paused = await client.put(
                path + "/plot", json={"expected_revision": current["revision"], "enabled": False}
            )
            assert paused.status_code == 200, paused.text
            current = paused.json()
            assert (
                not current["plot_enabled"]
                and current["snapshot"]["full_agent"]["plot"]["text"] == "玩家大纲"
            )
            edited = await client.put(
                path + "/plot", json={"expected_revision": current["revision"], "content": "暂停时编辑"}
            )
            assert edited.status_code == 200, edited.text
            current = edited.json()
            cleared = await client.put(
                path + "/plot",
                json={"expected_revision": current["revision"], "enabled": True, "content": "  "},
            )
            assert cleared.status_code == 200, cleared.text
            current = cleared.json()
            assert current["snapshot"]["full_agent"]["plot"] == {"text": "", "revision": 3}
            config = store.settings_document()
            config.full_agent.tools.plot_tokens = 128
            store.save_settings(config)
            excessive = await client.put(
                path + "/plot", json={"expected_revision": current["revision"], "content": "长大纲" * 1000}
            )
            assert excessive.status_code == 400
            assert store.session(session["id"])["revision"] == current["revision"]
            for status in ("queued", "running", "awaiting_review"):
                tid, _ = store.create_turn(
                    session["id"],
                    TurnRequest(text="继续", operation_id=status, expected_revision=current["revision"]),
                )
                store.set_turn(tid, status=status)
                denied = await client.put(
                    path + "/plot", json={"expected_revision": current["revision"], "enabled": False}
                )
                assert denied.status_code == 409
                store.set_turn(tid, status="cancelled")
            with store.connection() as db:
                db.execute(
                    "INSERT INTO context_operations(id,session,operation,kind,status,expected_revision,created) VALUES (?,?,?,?,?,?,0)",
                    ("context", session["id"], "context", "compact", "running", current["revision"]),
                )
            assert (
                await client.put(
                    path + "/plot", json={"expected_revision": current["revision"], "enabled": False}
                )
            ).status_code == 409
    finally:
        await app.state.service.close()


async def test_empty_story_can_enable_plot_but_not_save_text_and_half_agent_is_rejected(story):
    store, session = story
    card = store.import_card(json.dumps({"name": "Blank"}).encode(), "blank.json")
    empty = store.new_session(SessionCreate(card_id=card["id"], agent_mode="full_agent"))
    empty = store.update_session(empty["id"], empty["revision"], head=None)
    half = store.new_session(SessionCreate(card_id=card["id"], agent_mode="half_agent"))
    app = create_app(store.root)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
            path = f"/api/sessions/{empty['id']}/full-agent/plot"
            assert not empty["head"]
            response = await client.put(path, json={"expected_revision": empty["revision"], "enabled": True})
            assert response.status_code == 200 and response.json()["plot_enabled"]
            assert (
                await client.put(
                    path, json={"expected_revision": response.json()["revision"], "content": "尚无节点"}
                )
            ).status_code == 400
            assert (
                await client.put(
                    f"/api/sessions/{half['id']}/full-agent/plot",
                    json={"expected_revision": 0, "enabled": True},
                )
            ).status_code == 400
    finally:
        await app.state.service.close()


def context_fixture(*, compact=False):
    settings = Settings(
        model="fixture", api_mode="responses", context_strategy="openai_compact" if compact else "new_context"
    )
    session = {"id": "test-plot", "revision": 0, "persona": "玩家", "messages": [], "plot_enabled": True}
    snapshot = {"state": {}, "scopes": {}}
    full_state(snapshot)["plot"] = {"text": "完整大纲的尾部_MARKER", "revision": 42}
    req = TurnRequest(operation_id="plot", expected_revision=0)
    records = []

    def compile_current():
        tools = tool_definitions(req, session, settings.full_agent_config)
        return compile_window(
            {}, session, [], snapshot, "继续", settings, tools, input_id="input", continuation=records
        )

    return SimpleNamespace(
        settings=settings, session=session, snapshot=snapshot, records=records, compile=compile_current
    )


def test_plot_frames_follow_edits_disable_clear_and_do_not_expose_metadata():
    ctx = context_fixture()
    first = ctx.compile()
    ctx.records.extend(first["context_additions"])
    assert ctx.compile()["messages"] == first["messages"]
    for text in ("新大纲", "完整大纲的尾部_MARKER", ""):
        ctx.snapshot["full_agent"]["plot"]["text"] = text
        current = ctx.compile()
        additions = [m for m in current["context_additions"] if m.get("source") == "plot"]
        assert len(additions) == 1
        assert text in additions[0]["content"]
        ctx.records.extend(current["context_additions"])
    assert "本轮结束前成功调用 write_plot" in outlines(current["messages"])[-1]
    ctx.snapshot["full_agent"]["plot"]["text"] = "暂停后重开大纲"
    ctx.session["plot_enabled"] = False
    paused = ctx.compile()
    assert "剧情规划已关闭" in outlines(paused["messages"])[-1]
    assert "暂停后重开大纲" not in str(paused["messages"])
    ctx.records.extend(paused["context_additions"])
    ctx.session["plot_enabled"] = True
    opened = ctx.compile()
    assert "暂停后重开大纲" in outlines(opened["messages"])[-1]
    for forbidden in ('"revision"', '"checkpoint"', ctx.snapshot["full_agent"]["window"]["id"]):
        assert forbidden not in json.dumps(opened["messages"])


def test_cloud_checkpoint_restores_complete_current_plot_once_per_checkpoint():
    ctx = context_fixture(compact=True)
    first = ctx.compile()
    ctx.records.extend(first["context_additions"])
    checkpoint = {
        "version": 1,
        "origin": response_origin(ctx.settings),
        "signature": first["_compact_signature"],
        "covered": hashes(first["_compact_body"]),
        "items": [{"type": "compaction", "encrypted_content": "opaque-old-outline"}],
    }
    ctx.snapshot["full_agent"]["compaction"] = checkpoint
    after = ctx.compile()
    assert after["compaction_opaque"]
    assert len(outlines(after["messages"])) == 1
    assert "完整大纲的尾部_MARKER" in outlines(after["messages"])[0]
    assert any(m.get("source") == "plot" for m in after["context_additions"])
    ctx.records.extend(after["context_additions"])
    repeated = ctx.compile()
    assert repeated["messages"] == after["messages"]
    assert not any(m.get("source") == "plot" for m in repeated["context_additions"])
    ctx.snapshot["full_agent"]["plot"]["text"] = "压缩后玩家新大纲_MARKER"
    edited = ctx.compile()
    assert edited["compaction_opaque"]
    assert "压缩后玩家新大纲_MARKER" in outlines(edited["messages"])[-1]
    ctx.records.extend(edited["context_additions"])
    ctx.settings.model = "different-model"
    switched = ctx.compile()
    assert not switched["compaction_opaque"]
    assert "压缩后玩家新大纲_MARKER" in outlines(switched["messages"])[-1]


@pytest.mark.parametrize("api", ["chat_completions", "responses", "anthropic", "gemini"])
def test_long_plot_remains_complete_and_supports_message_adaptation(api):
    ctx = context_fixture()
    ctx.settings.api_mode = api
    text = "长阶段构思，包含人物动机和条件。" * 3000 + "FULL_TAIL"
    ctx.snapshot["full_agent"]["plot"]["text"] = text
    ctx.settings.full_agent_config.plot_tokens = 128  # lowering write limits cannot truncate stored text
    ctx.settings.message_adapter.mappings[api] = {"plot": AdapterRule(role="user", wrap=False)}
    ctx.settings._adapter_lane = "full_agent"
    result = ctx.compile()
    plot = next(m for m in result["trace"] if m["source"] == "plot")
    assert plot["role"] == "user" and plot["content"].endswith(text)
    assert "<tavern_plot>" not in plot["content"]
    assert plot["content"] in [m["content"] for m in result["messages"]]
    assert result["budget_sources"]["plot"] > 128


async def test_player_changes_replace_old_effective_plot_after_real_trace_replay(story):
    store, session = story
    session = enable(store, session, "原大纲")
    service = runtime(store)
    try:
        result, current = await turn(
            service, session, Model(("write_plot", {"content": "模型旧大纲"}), "正文")
        )
        assert result["status"] == "committed"
        current = enable(store, current, "玩家新大纲")
        compiled = compile_window(
            store.session_card(current["id"])["document"],
            current,
            current["messages"],
            current["snapshot"],
            "继续",
            store.settings().for_role("full_agent"),
            tool_definitions(
                TurnRequest(operation_id="preview", expected_revision=current["revision"]), current
            ),
            trace=load_trace(store, current["id"], current["messages"]),
        )
        assert "玩家新大纲" in outlines(compiled["messages"])[-1]
        assert compiled["messages"][-1]["role"] == "system"
        assert "模型旧大纲" in str(
            compiled["messages"]
        )  # historical tool arguments are retained, not retroactively changed
    finally:
        await service.close()


async def test_context_mode_conversion_preserves_plot_without_writing_or_replanning(tmp_path):
    from tests.backend.test_channels_compaction import operate
    from tests.backend.test_chatgpt_integration import connected_store
    from tests.backend.test_chatgpt_integration import story as official_story

    store, _, _ = connected_store(tmp_path)
    _, session = official_story(store, "full_agent")
    session = enable(store, session, "当前阶段大纲；尚未发生的伏笔。")
    original = copy.deepcopy(session["snapshot"]["full_agent"]["plot"])
    service = runtime(store)

    def unexpected_provider(_):
        raise AssertionError("Short mode conversion must not request a model or replan")

    service.context_operations.provider_factory = unexpected_provider
    try:
        for operation in ("enable_compact", "enable_local"):
            result, session = await operate(service, session, operation)
            assert not result["usage"]
            assert session["plot_enabled"] and session["snapshot"]["full_agent"]["plot"] == original
            settings = store.session_settings(session).for_role("full_agent")
            tools = tool_definitions(
                TurnRequest(operation_id="preview", expected_revision=session["revision"]), session
            )
            compiled = compile_window(
                store.session_card(session["id"])["document"],
                session,
                session["messages"],
                session["snapshot"],
                "继续",
                settings,
                tools,
                trace=load_trace(store, session["id"], session["messages"]),
            )
            assert original["text"] in outlines(compiled["messages"])[-1]
    finally:
        await service.close()
