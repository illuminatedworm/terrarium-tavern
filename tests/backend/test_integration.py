import asyncio
import json
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from kohakuterrarium.session.store import SessionStore
from kohakuterrarium.testing.llm import ScriptedLLM, ScriptEntry

from tavern.application.api import create_app
from tavern.application.service import RoleplayRuntime
from tavern.core.models import SessionCreate, TurnRequest
from tavern.storage.store import Store
from tests.support.history_labels import strip_label
from tests.support.memory_fixtures import memory_output
from tests.support.tracker_model import MemoryModel, PatchModel, TrackerModel, batch_payload


def _assert_run_closed(path):
    # Writer creates an engine per turn; shutdown must flush its output and
    # release the writer lock before Runtime can clean up run files on Windows.
    run_store = SessionStore.open_readonly(path)
    try:
        assert run_store.meta.get("status") == "paused"
    finally:
        run_store.close()
    reopened = SessionStore(path)
    reopened.close()
    renamed = path.with_suffix(".closed")
    path.rename(renamed)
    renamed.rename(path)


@pytest.mark.parametrize("mode", ["half_agent", "full_agent"])
async def test_input_replacement_replays_and_versions_the_whole_exchange(tmp_path, mode):
    import copy
    import hashlib

    import jsonpatch

    from tavern.storage.store import Conflict, dump, uid
    from tavern.storage.transfer import ExportSelection, export_package, import_package, read_package
    from tests.backend.test_full_agent import Model, Script, ScriptHost, handoff

    opened = []

    class ReplayScript(Script):
        async def call(self, method, *args):
            if method == "process" and args[1]:
                self.mirror["state"]["stat_data"] = jsonpatch.apply_patch(
                    self.mirror["state"]["stat_data"], args[1]
                )
            return await super().call(method, *args)

    class ReplayHost(ScriptHost):
        async def open(self, document, history, snapshot, *args, **kwargs):
            opened.append(copy.deepcopy(snapshot))
            script = ReplayScript(history, snapshot)
            script.mirror["lore"] = []
            script.mirror["state"]["script_runs"] += 1
            return script

    app = create_app(tmp_path)
    store, service = app.state.store, app.state.service
    service.half.runtime = service.full.runtime = ReplayHost()
    settings = store.settings()
    settings.memory_interval_turns = 1
    settings.full_agent_config.auto_retrieval = False
    settings.retrieval.channels = ["keyword"]
    store.save_settings(settings)
    card = store.import_card(
        b'{"name":"fixture","first_mes":"opening","description":"BASE_DESCRIPTION"}', "card.json"
    )
    session = store.new_session(SessionCreate(card_id=card["id"], agent_mode=mode, policy="automatic"))
    baseline = copy.deepcopy(session["snapshot"])
    baseline["state"] = {"stat_data": {"hp": 0}, "script_runs": 0}
    with store.connection() as db:
        db.execute("UPDATE messages SET snapshot=? WHERE id=?", (dump(baseline), session["head"]))
        if mode == "full_agent":
            db.execute("UPDATE sessions SET plot_enabled=1 WHERE id=?", (session["id"],))
    sid, opening = session["id"], session["head"]
    tracker_models = []

    def tracker():
        def first(messages):
            payload = next(
                json.loads(m["content"])
                for m in messages
                if m["role"] == "user" and '"current_input"' in m["content"]
            )
            value = payload["current_input"]
            if payload["task"]["phase"] == "pre":
                assert payload["character"]["description"] == "BASE_DESCRIPTION"
                return "revise_content", {
                    "operations": [{"op": "replace", "path": "/description", "value": "DESC_" + value}],
                    "comment": "本轮明确的设定要求",
                }
            batch = batch_payload(messages)
            return "write_memory", {
                "operations": [
                    {
                        "op": "add",
                        "record": {
                            "category": "episode",
                            "content": "MEMORY_" + value,
                            "sources": [batch["messages"][-1]["passages"][0]["ref"]],
                        },
                    }
                ],
                "questions": [],
            }

        def second(messages):
            payload = next(
                json.loads(m["content"])
                for m in messages
                if m["role"] == "user" and '"current_input"' in m["content"]
            )
            if payload["task"]["phase"] == "pre":
                return "完成"
            return "patch_state", {
                "operations": [
                    {
                        "op": "replace",
                        "path": "/hp",
                        "value": 1 if payload["current_input"] == "OLD_INPUT" else 2,
                    }
                ],
                "comment": "本轮变量",
            }

        model = TrackerModel(first, second, "完成")
        tracker_models.append(model)
        return model

    service.half.tracker_provider_factory = tracker

    def model(value, fail=False):
        if mode == "half_agent":
            return ScriptedLLM([] if fail else ["BODY_" + value])
        return Model(
            (
                "revise_content",
                {
                    "operations": [{"op": "replace", "path": "/description", "value": "DESC_" + value}],
                    "comment": "本轮设定",
                },
            ),
            ("write_note", {"key": "event", "content": "NOTE_" + value, "kind": "fact", "sources": ["H2"]}),
            ("write_handoff", handoff("HANDOFF_" + value, through=None)),
            ("write_plot", {"content": "PLOT_" + value}),
            ("read_state", {"path": ""}),
            (
                "patch_state",
                {
                    "operations": [
                        {"op": "replace", "path": "/hp", "value": 1 if value == "OLD_INPUT" else 2}
                    ],
                    "comment": "本轮变量",
                },
            ),
            RuntimeError("replacement failed") if fail else "BODY_" + value,
        )

    async def attempt(provider, **options):
        current = store.session(sid)
        service.provider_factory = lambda: provider
        request = TurnRequest(
            text="OLD_INPUT",
            operation_id=uid(),
            expected_revision=current["revision"],
            allow_content_edit=True,
            update_variables=True,
            **options,
        )
        queued = service.submit(sid, request)
        await service.tasks[queued["id"]]
        return store.turn(queued["id"])

    try:
        original_turn = await attempt(model("OLD_INPUT"))
        assert original_turn["status"] == "committed", original_turn["error"]
        original = store.session(sid)
        original_user = original["messages"][-2]
        assert original_user["snapshot"] == baseline
        assert original["snapshot"]["state"] == {"stat_data": {"hp": 1}, "script_runs": 1}
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as api:
            removed = await api.put(
                f"/api/sessions/{sid}/messages/{original['head']}",
                json={"text": "forbidden", "expected_revision": original["revision"]},
            )
            assert removed.status_code in (404, 405)
            for target in (opening, original_user["id"]):
                rejected = await api.post(
                    f"/api/sessions/{sid}/turns",
                    json={
                        "mode": "regenerate",
                        "message_id": target,
                        "replacement_text": "NEW_INPUT",
                        "operation_id": uid(),
                        "expected_revision": original["revision"],
                    },
                )
                assert rejected.status_code == 400

        replacement = {"mode": "regenerate", "message_id": original["head"], "replacement_text": "NEW_INPUT"}
        failed = await attempt(model("NEW_INPUT", fail=True), **replacement)
        assert failed["status"] == "error"
        assert store.session(sid)["snapshot"] == original["snapshot"]
        assert store.session(sid)["head"] == original["head"]
        if mode == "full_agent":
            with pytest.raises(Conflict, match="输入或授权已改变"):
                store.create_turn(
                    sid,
                    TurnRequest(
                        **{
                            **failed["request"],
                            "replacement_text": "THIRD_INPUT",
                            "operation_id": uid(),
                            "resume_turn_id": failed["id"],
                        }
                    ),
                )
        store.update_session(sid, original["revision"], policy="review")
        reviewed = await attempt(model("NEW_INPUT"), **replacement)
        assert reviewed["status"] == "awaiting_review", reviewed["error"]
        assert store.session(sid)["head"] == original["head"]
        assert reviewed["pending"]["messages"][0]["raw"] == "NEW_INPUT"
        service.review(reviewed["id"], False)
        assert store.session(sid)["snapshot"] == original["snapshot"]

        fresh_model = model("NEW_INPUT")
        accepted = await attempt(fresh_model, **replacement)
        assert accepted["status"] == "awaiting_review", accepted["error"]
        service.review(accepted["id"], True)
        store.commit_turn(accepted["id"], accepted["pending"])  # Duplicate delivery is idempotent.
        current = store.session(sid)
        assert current["messages"][-2]["id"] != original_user["id"]
        assert current["messages"][-2]["snapshot"] == baseline
        assert [m["raw"] for m in current["messages"]] == ["opening", "NEW_INPUT", "BODY_NEW_INPUT"]
        assert current["snapshot"]["state"] == {"stat_data": {"hp": 2}, "script_runs": 1}
        assert current["messages"][-1]["versions"] == [original["head"], current["head"]]
        assert (
            current["cache_breakpoint_follow"]["progress"] == original["cache_breakpoint_follow"]["progress"]
        )
        if mode == "half_agent":
            assert [r["content"] for r in current["memory_view"]["records"]] == ["MEMORY_NEW_INPUT"]
            assert current["snapshot"]["memory_archive"]["covered_count"] == 3
            sent = fresh_model.last_messages
        else:
            state = current["snapshot"]["full_agent"]
            assert state["notes"]["event"]["sources"] == [{"message_id": current["messages"][-2]["id"]}]
            assert state["notes"]["event"]["content"] == "NOTE_NEW_INPUT"
            assert state["plot"]["text"] == "PLOT_NEW_INPUT"
            assert state["handoff"]["current"][0]["text"] == "HANDOFF_NEW_INPUT"
            sent = fresh_model.requests[0]["messages"]
        wire = json.dumps(sent, ensure_ascii=False)
        assert "NEW_INPUT" in wire and "OLD_INPUT" not in wire
        assert "message_edit_notice" not in wire
        assert all(snapshot == baseline for snapshot in opened)
        selected = store.switch_message_version(sid, current["head"], original["head"], current["revision"])
        assert selected["snapshot"] == original["snapshot"]
        assert selected["messages"][-2]["raw"] == "OLD_INPUT"
        restored = store.switch_message_version(sid, selected["head"], current["head"], selected["revision"])
        assert restored["snapshot"] == current["snapshot"]
        assert restored["messages"][-2]["raw"] == "NEW_INPUT"
        fork = store.fork_session(sid, restored["head"], restored["revision"])
        assert fork["messages"][-2]["raw"] == "NEW_INPUT"
        assert len(fork["messages"][-1]["versions"]) == 1
        package, blobs = read_package(
            export_package(store, ExportSelection(settings=False, sessions=[sid])), store
        )
        imported = import_package(store, package, blobs)
        imported_story = store.session(imported["sessions"][0])
        assert imported_story["messages"][-2]["raw"] == "NEW_INPUT"
        assert len(imported_story["messages"][-1]["versions"]) == 2
        package["version"] = 2
        with pytest.raises(ValueError, match="旧对话"):
            import_package(store, package, blobs)
        with store.connection() as db:
            db.execute("UPDATE meta SET value='8' WHERE key='schema_version'")
        before = hashlib.sha256(store.path.read_bytes()).hexdigest()
        with pytest.raises(ValueError, match="旧对话"):
            Store(tmp_path)
        assert hashlib.sha256(store.path.read_bytes()).hexdigest() == before
    finally:
        await service.close()


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
    service.variable_provider_factory = lambda: PatchModel('[{"op":"replace","path":"/能量","value":4}]')
    try:
        t = service.submit(
            session["id"],
            TurnRequest(text="走向观测窗", operation_id="a", expected_revision=0, update_variables=True),
        )
        await service.tasks[t["id"]]
        final = store.turn(t["id"])
        assert final["status"] == "committed", final.get("error")
        current = store.session(session["id"])
        assert current["snapshot"]["state"]["stat_data"]["能量"] == 4
        first_head = current["head"]
        assert "_.add" not in current["messages"][-1]["display"]
        t2 = service.submit(
            session["id"],
            TurnRequest(mode="regenerate", operation_id="b", expected_revision=1, update_variables=True),
        )
        await service.tasks[t2["id"]]
        assert store.turn(t2["id"])["status"] == "committed", store.turn(t2["id"])["error"]
        assert store.session(session["id"])["snapshot"]["state"]["stat_data"]["能量"] == 4
        switched = store.update_session(session["id"], 2, head=first_head)
        assert switched["snapshot"]["state"]["stat_data"]["能量"] == 4
        run_files = list((tmp_path / "runs").glob("*.kohakutr"))
        assert len(run_files) == 2
        for path in run_files:
            _assert_run_closed(path)
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
    service.variable_provider_factory = lambda: PatchModel('[{"op":"replace","path":"/能量","value":4}]')
    try:
        t = service.submit(
            session["id"],
            TurnRequest(text="继续", operation_id="a", expected_revision=0, update_variables=True),
        )
        await service.tasks[t["id"]]
        assert store.turn(t["id"])["status"] == "error"
        assert "草稿" in store.turn(t["id"])["draft"]
        assert store.session(session["id"])["head"] == session["head"]
    finally:
        await service.close()


async def test_api_local_boundary_and_secret_redaction(tmp_path, monkeypatch):
    monkeypatch.delenv("TAVERN_PLATFORM", raising=False)
    monkeypatch.setenv("TAVERN_STARTUP_TOKEN", "leftover-android-host-token")
    app = create_app(tmp_path)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as c,
    ):
        assert (await c.get("/api/health")).status_code == 200
        assert (await c.get("/api/health", headers={"origin": "http://localhost:5173"})).status_code == 200
        # Desktop retains ordinary static-file validation and no Android host CSP.
        for path in ("/", "/static/runtime.js"):
            resource = await c.get(path)
            assert resource.status_code == 200
            assert "cache-control" not in resource.headers
            assert "content-security-policy" not in resource.headers
            assert (await c.get(path, headers={"if-none-match": resource.headers["etag"]})).status_code == 304
        assert (
            await c.patch(
                "/api/settings/shared",
                json={
                    "expected_revision": 0,
                    "changes": {"providers": [{"id": "default", "api_key": "secret"}]},
                },
            )
        ).status_code == 200
        response = await c.get("/api/settings")
        assert response.json()["shared"]["providers"][0]["has_api_key"]
        assert "secret" not in response.text
        assert (await c.get("/api/settings", headers={"origin": "null"})).status_code == 403
        assert (await c.get("/api/settings", headers={"host": "evil.example"})).status_code == 403

    token = "native-session-secret"
    monkeypatch.setenv("TAVERN_STARTUP_TOKEN", token)
    monkeypatch.setenv("TAVERN_PLATFORM", "android")
    app = create_app(tmp_path)
    monkeypatch.setenv("TAVERN_STARTUP_TOKEN", "changed-after-app-creation")
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:45831") as c,
    ):
        assert (await c.get("/api/health")).status_code == 403
        assert (await c.get("/runtime.js")).status_code == 403
        assert (await c.get("/api/health", headers={"X-Tavern-Token": "wrong"})).status_code == 403
        c.headers["X-Tavern-Token"] = token
        response = await c.get("/api/health")
        assert response.status_code == 200 and response.json()["platform"] == "android"
        assert token not in response.text
        assert not response.json()["capabilities"]["local_models"]
        assert (await c.get("/api/health", headers={"host": "localhost:45831"})).status_code == 403
        assert (await c.get("/api/health", headers={"origin": "http://127.0.0.1:5173"})).status_code == 403
        assert (await c.get("/api/health", headers={"origin": "null"})).status_code == 403
        assert (await c.get("/api/health", headers={"origin": "http://127.0.0.1:45831"})).status_code == 200
        del c.headers["X-Tavern-Token"]
        c.cookies.set("tavern_session", "wrong")
        assert (await c.get("/api/settings")).status_code == 403
        c.cookies.set("tavern_session", token)
        assert (await c.get("/api/settings")).status_code == 200
        # Extracted APK files share one mtime; unhashed entries must not survive an update.
        for path in ("/", "/static/runtime.js"):
            resource = await c.get(path)
            assert resource.headers["cache-control"] == "no-store"
            refreshed = await c.get(
                path,
                headers={
                    "if-none-match": resource.headers["etag"],
                    "if-modified-since": resource.headers["last-modified"],
                },
            )
            assert refreshed.status_code == 200 and refreshed.content == resource.content


async def test_preview_and_cancel_are_atomic(tmp_path):
    store = Store(tmp_path)
    c = store.import_card(Path("samples/雾港来信.json").read_bytes(), "card.json")
    s = store.new_session(SessionCreate(card_id=c["id"], policy="preview"))
    service = RoleplayRuntime(store)
    service.provider_factory = lambda: ScriptedLLM(["first draft"])
    service.tracker_provider_factory = lambda: TrackerModel(
        (
            "revise_content",
            {
                "operations": [{"op": "add", "path": "/description", "value": "New setting"}],
                "comment": "Explicit request",
            },
        ),
        "done",
    )
    try:
        t = service.submit(
            s["id"],
            TurnRequest(text="hello", operation_id="one", expected_revision=0, allow_content_edit=True),
        )
        await service.tasks[t["id"]]
        assert store.turn(t["id"])["status"] == "awaiting_review"
        assert store.session(s["id"])["head"] == s["head"]
        service.review(t["id"], True)
        current = store.session(s["id"])
        assert current["messages"][-1]["role"] == "assistant"
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
        _assert_run_closed(tmp_path / "runs" / f"{t['id']}.kohakutr")
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
    document = {
        "name": "fixture",
        "description": "character",
        "first_mes": "opening",
        "character_book": {
            "entries": [
                {
                    "id": "1",
                    "keys": ["marker"],
                    "enabled": True,
                    "constant": False,
                    "comment": "场景标记",
                    "content": "mid-scene",
                    "extensions": {"position": 4, "depth": 2, "role": 0},
                },
            ]
        },
    }
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
    assert messages[index] == {"role": "system", "content": "<lorebook>\nmid-scene\n</lorebook>"}
    assert strip_label(messages[index - 1]["content"]) == "a"
    assert strip_label(messages[index + 1]["content"]) == "b"
    try:
        assert (await provider.chat_complete(messages)).content == "OK"
        assert captured[0]["messages"] == messages
    finally:
        await provider.close()


async def test_native_tool_loop_uses_branch_data_and_preserves_pairs(tmp_path):
    store = Store(tmp_path)
    card = store.import_card(Path("samples/雾港来信.json").read_bytes(), "card.json")
    session = store.new_session(SessionCreate(card_id=card["id"]))
    provider = TrackerModel(
        [("search_lore", {"query": "*"}), ("tavern_recall_history", {"ref": "H1", "radius": 0})],
        ("search_lore", {"query": "灯塔"}),
        "维护完成",
        "后置检查完成",
    )
    writer = ScriptedLLM(["我们去灯塔。"])
    service = RoleplayRuntime(store)
    service.provider_factory = lambda: writer
    service.tracker_provider_factory = lambda: provider
    try:
        turn = service.submit(
            session["id"],
            TurnRequest(text="整理设定", operation_id="tools", expected_revision=0, allow_content_edit=True),
        )
        await service.tasks[turn["id"]]
        final = store.turn(turn["id"])
        assert final["status"] == "committed", final["error"]
        assert provider.call_count == 4 and writer.call_count == 1
        current = store.session(session["id"])
        assert current["snapshot"]["memory_document"]["records"] == []
        wire = provider.call_log[2]
        assert len([m for m in wire if m.get("role") == "tool"]) == 3
        assert session["head"] not in json.dumps(wire)
        ids = {m["tool_call_id"] for m in wire if m.get("role") == "tool"}
        calls = {c["id"] for m in wire for c in m.get("tool_calls", [])}
        assert ids == calls
        names = {tool["function"]["name"] for tool in provider.requests[0]["extra_body"]["tools"]}
        assert "search_lore" in names
        assert not {"read_lore", "bash", "read", "write", "group_spawn", "write_memory"} & names
        assert not any(m.get("role") == "tool" for m in writer.last_messages)
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
    cfg = store.settings().model_copy(
        update={"history_message_cap": 8, "chunk_size": 2, "memory_interval_turns": 1}
    )
    store.save_settings(cfg)
    card = store.import_card(Path("samples/雾港来信.json").read_bytes(), "card.json")
    s = store.new_session(SessionCreate(card_id=card["id"]))
    service = RoleplayRuntime(store)
    service.provider_factory = lambda: ScriptedLLM(["剧情继续。"])

    def proposal(messages):
        payload = batch_payload(messages)
        return memory_output(
            [{"role": "system", "content": ""}, {"role": "user", "content": json.dumps(payload)}],
            invalid=invalid_evidence,
        )

    memory_model = MemoryModel([proposal])
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
        assert memory_model.call_count > calls_before
        assert len(after["messages"]) == len(before["messages"])
    finally:
        await service.close()


async def test_invalid_model_variable_update_keeps_state_and_can_retry(tmp_path):
    store = Store(tmp_path)
    card = store.import_card(json.dumps({"name": "变量校验", "first_mes": "欢迎"}).encode(), "card.json")
    session = store.new_session(SessionCreate(card_id=card["id"]))
    state = {
        "stat_data": {"hp": 5},
        "schema": {"type": "object", "extensible": False, "properties": {"hp": {"type": "number"}}},
    }
    with store.connection() as db:
        snapshot = session["snapshot"] | {"state": state}
        db.execute("UPDATE messages SET snapshot=? WHERE id=?", (json.dumps(snapshot), session["head"]))
    before = store.session(session["id"])
    service = RoleplayRuntime(store)
    service.provider_factory = lambda: ScriptedLLM(["失败草稿"])
    service.variable_provider_factory = lambda: PatchModel('[{"op":"add","path":"/unknown","value":1}]')
    try:
        failed = service.submit(
            session["id"],
            TurnRequest(
                text="继续",
                operation_id="failed",
                expected_revision=before["revision"],
                update_variables=True,
            ),
        )
        await service.tasks[failed["id"]]
        failed = store.turn(failed["id"])
        assert failed["status"] == "error"
        assert "草稿已保留" in failed["error"]
        assert "Page.evaluate" not in failed["error"]
        assert "失败草稿" in failed["draft"]
        unchanged = store.session(session["id"])
        assert unchanged["head"] == before["head"] and unchanged["snapshot"] == before["snapshot"]
        service.provider_factory = lambda: ScriptedLLM(["成功回复"])
        service.variable_provider_factory = lambda: PatchModel('[{"op":"replace","path":"/hp","value":6}]')
        retried = service.submit(
            session["id"],
            TurnRequest(
                **(failed["request"] | {"operation_id": "retry", "expected_revision": unchanged["revision"]})
            ),
        )
        await service.tasks[retried["id"]]
        assert store.turn(retried["id"])["status"] == "committed"
        after = store.session(session["id"])
        assert len(after["messages"]) == len(before["messages"]) + 2
        assert after["snapshot"]["state"]["stat_data"]["hp"] == 6
    finally:
        await service.close()
