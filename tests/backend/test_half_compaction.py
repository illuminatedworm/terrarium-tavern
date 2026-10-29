"""Half Agent compact: real provider wire, independent memory, and durable sources."""

import asyncio
import copy
import json

import httpx
import pytest

from tavern.application.context_operations import ContextOperationRequest
from tavern.application.modes import ModeRuntime
from tavern.conversation.compaction import completed_context, hashes
from tavern.conversation.context import compile_context
from tavern.conversation.half_compaction import STATE, install, source_lore
from tavern.conversation.half_context import finalize_record, load_trace
from tavern.core.message_adapter import bind_adapter
from tavern.core.models import SessionCreate, TurnRequest
from tavern.scripting.request_context import response_origin
from tavern.storage.store import Conflict, uid
from tests.backend.chatgpt_fixtures import completed, sse
from tests.backend.test_channels_compaction import compact_response, reported_pressure
from tests.backend.test_chatgpt_integration import connected_store, story
from tests.backend.test_half_agent_alignment import Story
from tests.support.history_labels import strip_label
from tests.support.memory_fixtures import memory_snapshot
from tests.support.tracker_model import TrackerModel


async def operate(service, session, kind):
    manager = service.context_operations
    operation = manager.start(
        session["id"],
        ContextOperationRequest(kind=kind, operation_id=uid(), expected_revision=session["revision"]),
    )
    await manager.tasks[operation["id"]]
    return manager.get(operation["id"]), service.store.session(session["id"])


def append(store, session, text, reply, *, snapshot=None):
    before = copy.deepcopy(session["snapshot"])
    draft = copy.deepcopy(snapshot or before)
    tid, _ = store.create_turn(
        session["id"], TurnRequest(text=text, operation_id=uid(), expected_revision=session["revision"])
    )
    store.set_turn(tid, status="running")
    settings = bind_adapter(
        store.session_settings(session).for_role("foreground"), store, "story", session["id"]
    )
    user_id, output_id = uid(), uid()
    context = compile_context(
        store.session_card(session["id"])["document"],
        session,
        session["messages"],
        draft,
        text,
        settings,
        tid,
        input_message_id=user_id,
        replay_records=load_trace(store, session["id"], session["messages"]),
    )
    user = {
        "id": user_id,
        "parent": session["head"],
        "role": "user",
        "raw": text,
        "model": text,
        "display": text,
        "snapshot": before,
    }
    draft["lore"], draft["lore_injections"] = context["next_lore"], context["next_lore_injections"]
    output = {
        **user,
        "id": output_id,
        "parent": user_id,
        "role": "assistant",
        "raw": reply,
        "model": reply,
        "display": reply,
        "snapshot": draft,
    }
    store.commit_turn(
        tid,
        {
            "messages": [user, output],
            "before": before,
            "half_agent_record": finalize_record(context["half_agent_record"], None, reply, output),
        },
    )
    return store.session(session["id"])


def checkpoint(context, settings):
    history = completed_context(context)
    return {
        "version": 1,
        "origin": response_origin(settings),
        "signature": context["_compact_signature"],
        "covered": hashes(history["_compact_body"]),
        "compressed_count": len(history["_compact_body"]),
        "items": [{"type": "compaction", "encrypted_content": "opaque-fixture"}],
    }


async def test_short_half_switches_do_not_create_provider_or_full_agent_state(tmp_path):
    store, _, _ = connected_store(tmp_path)
    _, session = story(store)
    service = ModeRuntime(store)

    def forbidden(settings):
        raise AssertionError("short switches must stay local")

    service.context_operations.provider_factory = forbidden
    try:
        for kind in ("enable_compact", "enable_local"):
            result, session = await operate(service, session, kind)
            assert result["status"] == "completed", result
            assert not result["converted"] and not result["compacted"]
            assert "full_agent" not in session["snapshot"]
        assert STATE not in session["snapshot"]
        assert session["context_strategy"] == "new_context"
    finally:
        await service.close()


@pytest.mark.parametrize("automatic", [True, False])
async def test_writer_compacts_once_before_story_without_local_popout(tmp_path, automatic, monkeypatch):
    store, _, _ = connected_store(tmp_path)
    config = store.settings_document()
    config.half_agent.chunk_mode = "messages"
    config.half_agent.history_message_cap = 8
    config.half_agent.chunk_size = 2
    config.half_agent.official_foreground.context_tokens = 8192
    config.half_agent.official_foreground.reply_tokens = 1024
    store.save_settings(config)
    _, session = story(store)
    for index in range(5):
        session = append(store, session, f"旧发言 {index}", f"旧回复 {index}")
    service = ModeRuntime(store)
    wire = []
    text = "本轮新输入不可送给整理请求。" * 3000

    def route(request):
        body = json.loads(request.content)
        wire.append(body)
        assert body["model"] == "fixture-writer", "cloud context must not force pre-story Tracker coverage"
        source = json.dumps(body["input"], ensure_ascii=False)
        assert session["id"] not in source
        if body.get("instructions"):
            assert text not in source
            assert not body.get("tools")
            assert "旧发言 0" in source and "旧回复 4" in source
            return sse(compact_response())
        assert "context_management" not in body
        assert text in source
        if automatic:
            assert any(item.get("type") == "compaction" for item in body["input"])
            assert "旧发言 0" not in source
        else:
            assert "旧发言 0" in source
        return sse(completed("实际正文<tavern_retrieval>门口</tavern_retrieval>"))

    store.chatgpt.http_transport = httpx.MockTransport(route)
    try:
        result, session = await operate(service, session, "enable_compact")
        assert result["status"] == "completed" and not wire
        session = store.update_session(session["id"], session["revision"], compact_auto=int(automatic))
        reported_pressure(monkeypatch)
        turn = service.submit(
            session["id"], TurnRequest(text=text, operation_id=uid(), expected_revision=session["revision"])
        )
        await service.tasks[turn["id"]]
        saved = store.turn(turn["id"])
        assert saved["status"] == "committed", saved.get("error")
        current = store.session(session["id"])
        assert current["messages"][-1]["raw"] == "实际正文"
        assert "context_window" not in current["snapshot"]
        assert "memory_archive" not in current["snapshot"]
        assert len(wire) == (2 if automatic else 1)
        if automatic:
            saved_checkpoint = current["snapshot"][STATE]["compaction"]
            assert saved_checkpoint["covered"]
            assert text not in json.dumps(saved_checkpoint, ensure_ascii=False)
            events = store.events(turn["id"])
            usage = [e for e in events if e["kind"] == "usage"]
            assert len(usage) == 2
    finally:
        await service.close()


async def test_failed_auto_compaction_never_sends_writer_or_commits(tmp_path, monkeypatch):
    store, _, _ = connected_store(tmp_path)
    _, session = story(store)
    service = ModeRuntime(store)
    wire = []

    def route(request):
        body = json.loads(request.content)
        wire.append(body)
        assert body.get("instructions")
        return sse(completed("OK without a checkpoint"))

    store.chatgpt.http_transport = httpx.MockTransport(route)
    try:
        _, session = await operate(service, session, "enable_compact")
        before = copy.deepcopy(session["snapshot"])
        reported_pressure(monkeypatch)
        turn = service.submit(
            session["id"],
            TurnRequest(
                text="Oversized pending input. " * 40000,
                operation_id=uid(),
                expected_revision=session["revision"],
            ),
        )
        await service.tasks[turn["id"]]
        assert store.turn(turn["id"])["status"] == "error"
        assert "未生成压缩状态" in store.turn(turn["id"])["error"]
        assert len(wire) == 1
        current = store.session(session["id"])
        assert current["head"] == session["head"] and current["snapshot"] == before
    finally:
        await service.close()


def test_checkpoint_reuse_current_state_updates_and_source_invalidations():
    scene = Story("responses")
    scene.cfg.context_strategy = "openai_compact"
    for index in range(2):
        scene.commit(scene.compile(), text=f"旧正文 {index}")
    full = scene.compile()
    assert full["context_ledger"]["rebuild_reason"] is None
    install(scene.snapshot, checkpoint(full, scene.cfg), full, scene.history)
    projected = scene.compile()
    assert projected["compaction_opaque"]
    assert "旧正文" not in json.dumps(projected["messages"], ensure_ascii=False)
    assert {"state", "post", "author_note"} <= {
        row["source"] for row in projected["half_agent_record"]["frames"]
    }
    scene.commit(projected, "压缩后的一次正文")
    assert scene.compile()["half_agent_record"]["frames"] == [], (
        "unchanged fields reassert only once per checkpoint"
    )
    scene.snapshot["state"]["stat_data"]["hp"] = 99
    current = scene.compile()
    assert current["compaction_opaque"]
    assert any("99" in message["content"] for message in current["messages"])
    original = copy.deepcopy(scene.snapshot)
    scene.history[1]["model"] = "编辑后的历史"
    invalid = scene.compile()
    assert not invalid["compaction_opaque"]
    assert "编辑后的历史" in json.dumps(invalid["messages"], ensure_ascii=False)
    assert scene.snapshot == original, "compilation/preview is read-only"


async def test_manual_compact_preserves_dynamic_tail_and_reuses_it_next_turn(tmp_path):
    store, _, _ = connected_store(tmp_path)
    _, session = story(store)
    session = append(store, session, "过去的问题", "过去的回答")
    service = ModeRuntime(store)
    wire = []

    def route(request):
        body = json.loads(request.content)
        wire.append(body)
        if body.get("instructions"):
            return sse(compact_response())
        assert any(item.get("type") == "compaction" for item in body["input"])
        assert "过去的回答" not in json.dumps(body["input"], ensure_ascii=False)
        return sse(completed("新回复"))

    store.chatgpt.http_transport = httpx.MockTransport(route)
    try:
        _, session = await operate(service, session, "enable_compact")
        result, session = await operate(service, session, "compact")
        assert result["status"] == "completed" and result["compacted"], result
        turn = service.submit(
            session["id"],
            TurnRequest(text="下一个问题", operation_id=uid(), expected_revision=session["revision"]),
        )
        await service.tasks[turn["id"]]
        assert store.turn(turn["id"])["status"] == "committed", store.turn(turn["id"])
        assert len(wire) == 2
        _, current = await operate(service, store.session(session["id"]), "enable_local")
        duplicate = service.context_operations.start(
            current["id"],
            ContextOperationRequest(
                operation_id=result["operation"],
                expected_revision=result["expected_revision"],
                kind="compact",
            ),
        )
        assert duplicate["id"] == result["id"] and duplicate["status"] == "completed"
        assert len(wire) == 2
    finally:
        await service.close()


def test_compressed_lore_cools_down_but_frozen_sources_survive_removal():
    scene = Story("responses")
    scene.cfg.context_strategy = "openai_compact"
    scene.document["character_book"] = {
        "entries": [
            {
                "id": "harbor",
                "keys": ["港口"],
                "content": "港口的完整旧设定。",
                "enabled": True,
                "cooldown": 2,
            }
        ]
    }
    row = {
        "id": "harbor",
        "anchor_id": "opening",
        "content": "港口的完整旧设定。",
        "title": "港口",
        "source_ids": [],
    }
    scene.snapshot["lore_injections"] = [row]
    scene.snapshot["lore"] = {"harbor": {"present": True}}
    scene.history[0]["snapshot"] = copy.deepcopy(scene.snapshot)
    scene.commit(scene.compile())
    context = scene.compile()
    install(scene.snapshot, checkpoint(context, scene.cfg), context, scene.history)
    assert scene.snapshot["lore"]["harbor"]["present"] is False
    assert "removed_at" in scene.snapshot["lore"]["harbor"]
    assert scene.snapshot["lore_injections"] == []
    assert source_lore(scene.history, scene.snapshot, scene.records) == [row]
    current = scene.compile()
    assert current["compaction_opaque"]
    assert current["next_lore_injections"] == []
    assert not current["next_lore"]["harbor"]["present"]
    scene.history[0]["snapshot"]["lore_injections"] = [
        {"id": "harbor", "anchor_id": "opening", "manually_removed": True}
    ]
    invalid = scene.compile()
    assert not invalid["compaction_opaque"]
    assert "港口的完整旧设定" not in json.dumps(invalid["messages"], ensure_ascii=False)


def test_pending_input_inside_history_cannot_split_an_exchange_for_compaction():
    scene = Story("responses")
    scene.cfg.context_strategy = "openai_compact"
    scene.commit(scene.compile(), "首轮完整回复")
    scene.commit(scene.compile(), "末轮完整回复")
    block = next(block for block in scene.session["recipe"]["blocks"] if block["source"] == "input")
    block["depth"] = 1
    compiled = scene.compile()
    replay = completed_context(compiled)["_compact_body"]
    content = json.dumps(replay, ensure_ascii=False)
    assert "首轮完整回复" in content
    assert "末轮完整回复" not in content
    rows = compiled["_compact_rows"][: compiled["_replay_complete"]]
    assert rows[-1]["message_id"] == scene.history[2]["id"]
    assert scene.history[3]["id"] not in {row.get("message_id") for row in rows}


def test_role_remapping_does_not_separate_lore_from_its_source_exchange():
    from tavern.conversation.compaction import replay_groups
    from tavern.core.message_adapter import MessageAdapterConfig

    scene = Story("responses")
    scene.cfg.context_strategy = "openai_compact"
    scene.cfg.message_adapter = MessageAdapterConfig.model_validate(
        {"mappings": {"responses": {"lore_injected": {"role": "user", "wrap": False}}}}
    )
    scene.document["character_book"] = {
        "entries": [{"id": "place", "keys": ["港口"], "enabled": True, "content": "完整锚定资料"}]
    }
    scene.commit(scene.compile(), "首轮完整回复")
    scene.snapshot["lore_injections"] = [
        {"id": "place", "anchor_id": scene.history[1]["id"], "content": "完整锚定资料", "title": "地点"}
    ]
    scene.snapshot["lore"] = {"place": {"present": True}}
    scene.commit(scene.compile(), "末轮完整回复")
    compiled = completed_context(scene.compile())
    groups = replay_groups(compiled["_compact_body"], compiled["_replay_starts"])
    assert len(groups) == 3
    lore_group = next(group for group in groups if any(row["content"] == "完整锚定资料" for row in group))
    assert lore_group[0]["role"] == "user" and lore_group[0]["content"] == "完整锚定资料"
    assert strip_label(lore_group[-1]["content"]) == "首轮完整回复"


def test_reinjected_lore_gets_a_new_cooldown_on_its_next_compaction():
    scene = Story("responses")
    scene.cfg.context_strategy = "openai_compact"
    scene.document["character_book"] = {
        "entries": [
            {"id": "place", "keys": ["港口"], "enabled": True, "content": "港口的现行设定", "cooldown": 0}
        ]
    }
    old = {"id": "place", "anchor_id": "opening", "content": "港口的旧设定", "title": "港口"}
    scene.snapshot["lore_injections"] = [old]
    scene.snapshot["lore"] = {"place": {"present": True}}
    scene.history[0]["snapshot"] = copy.deepcopy(scene.snapshot)
    compiled = scene.compile()
    install(scene.snapshot, checkpoint(compiled, scene.cfg), compiled, scene.history)
    first_exit = scene.snapshot["lore"]["place"]["removed_at"]
    for _ in range(2):
        scene.commit(scene.compile())
    fresh = {**old, "anchor_id": scene.history[-2]["id"], "content": "港口的现行设定"}
    scene.snapshot["lore_injections"] = [fresh]
    scene.snapshot["lore"]["place"] = {"present": True}
    scene.history[-1]["snapshot"] = copy.deepcopy(scene.snapshot)
    compiled = scene.compile()
    install(scene.snapshot, checkpoint(compiled, scene.cfg), compiled, scene.history)
    assert scene.snapshot["lore"]["place"]["removed_at"] == 3 > first_exit
    assert not scene.snapshot["lore"]["place"]["present"]


def test_removed_lore_reservation_exits_with_its_compressed_exchange():
    scene = Story("responses")
    scene.cfg.context_strategy = "openai_compact"
    scene.document["character_book"] = {
        "entries": [{"id": "place", "keys": ["港口"], "enabled": True, "content": "不应恢复的旧注入"}]
    }
    removed = {"id": "place", "anchor_id": "opening", "manually_removed": True}
    scene.snapshot["lore_injections"] = [removed]
    scene.snapshot["lore"] = {"place": {"present": True}}
    scene.history[0]["snapshot"] = copy.deepcopy(scene.snapshot)
    compiled = scene.compile()
    assert "不应恢复的旧注入" not in json.dumps(compiled["messages"], ensure_ascii=False)
    install(scene.snapshot, checkpoint(compiled, scene.cfg), compiled, scene.history)
    assert not scene.snapshot["lore"]["place"]["present"]
    assert scene.snapshot["lore"]["place"]["removed_at"] == 1
    assert scene.snapshot["lore_injections"] == []
    assert source_lore(scene.history, scene.snapshot) == [removed]


async def test_context_operation_cancellation_blocks_story_and_restores_original(tmp_path):
    store, _, _ = connected_store(tmp_path)
    _, session = story(store)
    service = ModeRuntime(store)
    started = asyncio.Event()
    release = asyncio.Event()

    async def route(request):
        started.set()
        await release.wait()
        return sse(compact_response())

    store.chatgpt.http_transport = httpx.MockTransport(route)
    try:
        _, session = await operate(service, session, "enable_compact")
        before = copy.deepcopy(session["snapshot"])
        manager = service.context_operations
        operation = manager.start(
            session["id"],
            ContextOperationRequest(
                kind="compact", operation_id=uid(), expected_revision=session["revision"]
            ),
        )
        await asyncio.wait_for(started.wait(), 10)
        with pytest.raises(Conflict):
            service.submit(
                session["id"],
                TurnRequest(text="不能开始", operation_id=uid(), expected_revision=session["revision"]),
            )
        await manager.cancel(operation["id"])
        assert store.session(session["id"])["snapshot"] == before
        release.set()
        manager.resume(operation["id"])
        await manager.tasks[operation["id"]]
        assert manager.get(operation["id"])["status"] == "completed"
    finally:
        release.set()
        await service.close()


async def test_switch_back_uses_tracker_coverage_and_keeps_manual_memory(tmp_path):
    store, _, _ = connected_store(tmp_path)
    config = store.settings_document()
    config.half_agent.chunk_mode = "messages"
    config.half_agent.history_message_cap = 8
    config.half_agent.chunk_size = 2
    store.save_settings(config)
    _, session = story(store)
    snapshot = {**session["snapshot"], **memory_snapshot("不可覆盖的手工记忆")}
    session = append(store, session, "首轮", "首轮结束", snapshot=snapshot)
    for i in range(5):
        session = append(store, session, f"交流 {i}", f"回复 {i}")
    service = ModeRuntime(store)
    models = []

    def tracker(settings):
        assert settings.model == "fixture-tracker"
        model = TrackerModel(("write_memory", {"operations": [], "questions": []}), "整理完成")
        models.append(model)
        return model

    service.context_operations.provider_factory = tracker
    try:
        before = copy.deepcopy(session["snapshot"]["memory_document"])
        result, session = await operate(service, session, "enable_compact")
        assert result["status"] == "completed" and not models
        result, session = await operate(service, session, "enable_local")
        assert result["status"] == "completed", result
        assert result["converted"] and models
        snapshot = session["snapshot"]
        assert snapshot["memory_archive"]["covered_count"] >= snapshot["context_window"]["covered_count"] > 0
        assert snapshot["context_window"]["covered_count"] <= len(session["messages"]) - 4
        assert [
            {k: row[k] for k in old}
            for row, old in zip(snapshot["memory_document"]["records"], before["records"], strict=True)
        ] == before["records"]
        assert STATE not in snapshot and "full_agent" not in snapshot
        for model in models:
            assert model.closed
            for call in model.requests:
                assert "_compact_threshold" not in call
                assert all(t["function"]["name"] != "prepare_handoff" for t in call["extra_body"]["tools"])
    finally:
        await service.close()


async def test_periodic_tracker_and_variables_still_run_in_compact_mode(tmp_path):
    store, _, _ = connected_store(tmp_path)
    config = store.settings_document()
    config.half_agent.memory_interval_turns = 1
    store.save_settings(config)
    _, session = story(store)
    service = ModeRuntime(store)
    tracker = TrackerModel(
        ("write_memory", {"operations": [], "questions": []}),
        ("patch_state", {"operations": [], "comment": "无需修改"}),
        "维护完成",
    )
    service.half.tracker_provider_factory = lambda: tracker
    wire = []

    def route(request):
        body = json.loads(request.content)
        wire.append(body)
        assert not body.get("context_management") and not body.get("tools")
        return sse(completed("本轮完整正文"))

    store.chatgpt.http_transport = httpx.MockTransport(route)
    try:
        _, session = await operate(service, session, "enable_compact")
        turn = service.submit(
            session["id"],
            TurnRequest(
                text="继续", update_variables=True, operation_id=uid(), expected_revision=session["revision"]
            ),
        )
        await service.tasks[turn["id"]]
        saved = store.turn(turn["id"])
        assert saved["status"] == "committed", saved.get("error")
        current = store.session(session["id"])
        assert current["snapshot"]["memory_archive"]["covered_count"] == len(current["messages"])
        assert len(wire) == 1
        assert tracker.call_count == 3
        assert all("_compact_threshold" not in call for call in tracker.requests)
    finally:
        await service.close()


async def test_regeneration_uses_original_input_snapshot_and_not_replaced_reply(tmp_path):
    store, _, _ = connected_store(tmp_path)
    _, session = story(store)
    session = append(store, session, "需要重写的问题", "必须排除的旧回复")
    service = ModeRuntime(store)
    wire = []

    def route(request):
        body = json.loads(request.content)
        wire.append(body)
        assert "必须排除的旧回复" not in json.dumps(body["input"], ensure_ascii=False)
        assert "需要重写的问题" in json.dumps(body["input"], ensure_ascii=False)
        return sse(completed("替代回复"))

    store.chatgpt.http_transport = httpx.MockTransport(route)
    try:
        _, session = await operate(service, session, "enable_compact")
        original_user = session["messages"][-2]["id"]
        count = len(session["messages"])
        turn = service.submit(
            session["id"],
            TurnRequest(
                mode="regenerate",
                message_id=session["head"],
                operation_id=uid(),
                expected_revision=session["revision"],
            ),
        )
        await service.tasks[turn["id"]]
        assert store.turn(turn["id"])["status"] == "committed", store.turn(turn["id"])
        current = store.session(session["id"])
        assert len(current["messages"]) == count and current["messages"][-2]["id"] == original_user
        assert current["messages"][-1]["raw"] == "替代回复" and len(wire) == 1
    finally:
        await service.close()


async def test_fork_import_and_log_cleanup_keep_replay_sources(tmp_path):
    from tavern.storage.transfer import ExportSelection, export_package, import_package, read_package

    store, _, _ = connected_store(tmp_path)
    _, session = story(store)
    for index in range(3):
        session = append(store, session, f"问题 {index}", f"回答 {index}")
    service = ModeRuntime(store)
    store.chatgpt.http_transport = httpx.MockTransport(lambda request: sse(compact_response()))
    try:
        _, session = await operate(service, session, "enable_compact")
        result, session = await operate(service, session, "compact")
        assert result["status"] == "completed", result
        original = copy.deepcopy(session["snapshot"][STATE]["compaction"])
        with store.connection() as db:
            db.execute("DELETE FROM events")
            db.execute("UPDATE turns SET preview=NULL")
            db.execute("DELETE FROM context_operations WHERE status='completed'")
        assert store.session(session["id"])["snapshot"][STATE]["compaction"] == original
        fork = store.fork_session(session["id"], session["messages"][-3]["id"], session["revision"])
        assert len(fork["messages"]) == len(session["messages"]) - 2
        assert not fork["snapshot"].get(STATE, {}).get("compaction")
        assert len(load_trace(store, fork["id"], fork["messages"])) == 2
        package = read_package(export_package(store, ExportSelection(sessions=[session["id"]])))
        result = import_package(store, *package)
        imported = store.session(result["sessions"][0])
        assert not imported["snapshot"][STATE].get("compaction")
        assert len(load_trace(store, imported["id"], imported["messages"])) == 3
    finally:
        await service.close()


@pytest.mark.parametrize("accept", [False, True])
async def test_review_decides_story_variables_and_checkpoint_as_one_transaction(
    tmp_path, accept, monkeypatch
):
    store, _, _ = connected_store(tmp_path)
    config = store.settings_document()
    config.half_agent.chunk_mode = "messages"
    config.half_agent.official_foreground.context_tokens = 8192
    config.half_agent.official_foreground.reply_tokens = 1024
    store.save_settings(config)
    card = store.import_card(
        json.dumps(
            {
                "name": "Fixture",
                "first_mes": "开场",
                "character_book": {
                    "entries": [
                        {
                            "id": "initial",
                            "comment": "[InitVar]",
                            "content": "hp: 5",
                            "enabled": True,
                            "keys": [],
                        }
                    ]
                },
            }
        ).encode(),
        "card.json",
    )
    session = store.new_session(SessionCreate(card_id=card["id"]))
    session = store.update_session(session["id"], session["revision"], policy="review")
    service = ModeRuntime(store)
    tracker = TrackerModel(
        ("patch_state", {"operations": [{"op": "add", "path": "/hp", "value": 3}], "comment": "本轮变化"}),
        "完成",
    )
    service.half.tracker_provider_factory = lambda: tracker
    store.chatgpt.http_transport = httpx.MockTransport(
        lambda request: sse(
            compact_response() if json.loads(request.content).get("instructions") else completed("待审核正文")
        )
    )
    try:
        _, session = await operate(service, session, "enable_compact")
        before = copy.deepcopy(session["snapshot"])
        reported_pressure(monkeypatch)
        turn = service.submit(
            session["id"],
            TurnRequest(
                text="完整输入。" * 4000,
                update_variables=True,
                operation_id=uid(),
                expected_revision=session["revision"],
            ),
        )
        await service.tasks[turn["id"]]
        staged = store.turn(turn["id"])
        assert staged["status"] == "awaiting_review", staged.get("error")
        assert store.session(session["id"])["snapshot"] == before
        with pytest.raises(Conflict):
            service.context_operations.start(
                session["id"],
                ContextOperationRequest(
                    operation_id=uid(), expected_revision=session["revision"], kind="enable_local"
                ),
            )
        service.review(turn["id"], accept)
        saved = store.session(session["id"])
        if accept:
            assert saved["head"] != session["head"]
            assert saved["snapshot"]["state"]["stat_data"]["hp"] == 3
            assert saved["snapshot"][STATE]["compaction"]["items"][0]["type"] == "compaction"
        else:
            assert saved["head"] == session["head"] and saved["snapshot"] == before
    finally:
        await service.close()


async def test_setting_change_during_compact_prevents_publish_and_stale_resume(tmp_path):
    store, _, _ = connected_store(tmp_path)
    _, session = story(store)
    service = ModeRuntime(store)
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def route(request):
        calls.append(request)
        entered.set()
        await release.wait()
        return sse(compact_response())

    store.chatgpt.http_transport = httpx.MockTransport(route)
    try:
        _, session = await operate(service, session, "enable_compact")
        before = copy.deepcopy(session["snapshot"])
        manager = service.context_operations
        operation = manager.start(
            session["id"],
            ContextOperationRequest(
                operation_id=uid(), kind="compact", expected_revision=session["revision"]
            ),
        )
        task = manager.tasks[operation["id"]]
        await asyncio.wait_for(entered.wait(), 10)
        config = store.settings_document()
        config.half_agent.system_prompt += "\n新的正文规则。"
        store.save_settings(config)
        release.set()
        await task
        assert manager.get(operation["id"])["status"] == "error"
        assert store.session(session["id"])["snapshot"] == before
        manager.resume(operation["id"])
        await manager.tasks[operation["id"]]
        assert manager.get(operation["id"])["status"] == "error"
        assert len(calls) == 1
    finally:
        release.set()
        await service.close()


async def test_cancel_during_automatic_compaction_keeps_committed_snapshot(tmp_path, monkeypatch):
    store, _, _ = connected_store(tmp_path)
    _, session = story(store)
    service = ModeRuntime(store)
    entered = asyncio.Event()

    async def route(request):
        assert json.loads(request.content).get("instructions")
        entered.set()
        await asyncio.Event().wait()

    store.chatgpt.http_transport = httpx.MockTransport(route)
    try:
        _, session = await operate(service, session, "enable_compact")
        before = copy.deepcopy(session["snapshot"])
        reported_pressure(monkeypatch)
        turn = service.submit(
            session["id"],
            TurnRequest(
                text="Long pending input. " * 40000, operation_id=uid(), expected_revision=session["revision"]
            ),
        )
        await asyncio.wait_for(entered.wait(), 15)
        await service.cancel(turn["id"])
        assert store.turn(turn["id"])["status"] == "cancelled"
        assert store.session(session["id"])["snapshot"] == before
    finally:
        await service.close()
