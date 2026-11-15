import json
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from tavern.application.api import create_app
from tavern.application.service import StoryService
from tavern.core.models import SessionCreate, TurnRequest
from tavern.storage.legacy_story import OLD_STORY_KEY
from tavern.storage.store import Store


async def test_input_replacement_replays_and_versions_the_whole_exchange(tmp_path):
    import copy
    import hashlib

    import jsonpatch

    from tavern.core.ooc import ooc_block
    from tavern.storage.store import Conflict, dump, uid
    from tavern.storage.transfer import ExportSelection, export_package, import_package, read_package
    from tests.backend.test_story import Model, Script, ScriptHost, handoff

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
    service.story.runtime = ReplayHost()
    settings = store.settings()
    settings.story_config.auto_retrieval = False
    settings.retrieval.channels = ["keyword"]
    store.save_settings(settings)
    card = store.import_card(b'{"name":"fixture","first_mes":"opening","description":"BASE_DESCRIPTION"}')
    session = store.new_session(SessionCreate(card_id=card["id"], policy="automatic"))
    baseline = copy.deepcopy(session["snapshot"])
    baseline["state"] = {"stat_data": {"hp": 0}, "script_runs": 0}
    with store.connection() as db:
        db.execute("UPDATE messages SET snapshot=? WHERE id=?", (dump(baseline), session["head"]))
        db.execute("UPDATE sessions SET plot_enabled=1 WHERE id=?", (session["id"],))
    sid, opening = session["id"], session["head"]
    ooc = "NEW_OOC {{char}}"
    ooc_suffix = "\n\n" + ooc_block(ooc)

    def model(value, fail=False):
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
        service.story.provider_factory = lambda: provider
        request = TurnRequest(
            text="OLD_INPUT",
            operation_id=uid(),
            expected_revision=current["revision"],
            allow_content_edit=True,
            update_variables=True,
            **options,
        )
        queued = service.story.submit(sid, request)
        await service.story.tasks[queued["id"]]
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

        replacement = {
            "mode": "regenerate",
            "message_id": original["head"],
            "replacement_text": "NEW_INPUT",
            "ooc": ooc,
        }
        failed = await attempt(model("NEW_INPUT", fail=True), **replacement)
        assert failed["status"] == "error"
        assert store.session(sid)["snapshot"] == original["snapshot"]
        assert store.session(sid)["head"] == original["head"]
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
        store.review_turn(reviewed["id"], False)
        assert store.session(sid)["snapshot"] == original["snapshot"]

        fresh_model = model("NEW_INPUT")
        accepted = await attempt(fresh_model, **replacement)
        assert accepted["status"] == "awaiting_review", accepted["error"]
        store.review_turn(accepted["id"], True)
        store.commit_turn(accepted["id"], accepted["pending"])  # Duplicate delivery is idempotent.
        current = store.session(sid)
        assert current["messages"][-2]["id"] != original_user["id"]
        assert current["messages"][-2]["snapshot"] == baseline
        assert [m["raw"] for m in current["messages"]] == ["opening", "NEW_INPUT", "BODY_NEW_INPUT"]
        # Ooc text stays out of the card-processed raw/display layers and reaches models tagged.
        user = current["messages"][-2]
        assert user["ooc"] == ooc and "NEW_OOC" not in user["display"]
        assert user["model"].endswith(ooc_suffix)
        assert current["snapshot"]["state"] == {"stat_data": {"hp": 2}, "script_runs": 1}
        assert current["messages"][-1]["versions"] == [original["head"], current["head"]]
        assert (
            current["cache_breakpoint_follow"]["progress"] == original["cache_breakpoint_follow"]["progress"]
        )
        state = current["snapshot"]["story"]
        assert state["notes"]["event"]["sources"] == [{"message_id": current["messages"][-2]["id"]}]
        assert state["notes"]["event"]["content"] == "NOTE_NEW_INPUT"
        assert state["plot"]["text"] == "PLOT_NEW_INPUT"
        assert state["handoff"]["current"][0]["text"] == "HANDOFF_NEW_INPUT"
        sent = fresh_model.requests[0]["messages"]
        wire = json.dumps(sent, ensure_ascii=False)
        assert "NEW_INPUT" in wire and "OLD_INPUT" not in wire
        assert any(ooc_suffix in m["content"] for m in sent if m["role"] == "user")
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as api:
            preview = await api.post(f"/api/sessions/{sid}/messages-preview", json={"text": "NEXT_INPUT"})
        assert preview.status_code == 200
        assert any(ooc_suffix in m["content"] for m in preview.json() if m["role"] == "user")
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
        package["sessions"][0]["session"]["agent_mode"] = OLD_STORY_KEY
        with pytest.raises(ValueError, match="旧故事早于整轮重试格式"):
            import_package(store, package, blobs)
        with store.connection() as db:
            db.execute(f"ALTER TABLE sessions ADD COLUMN agent_mode TEXT NOT NULL DEFAULT '{OLD_STORY_KEY}'")
            db.execute("UPDATE meta SET value='8' WHERE key='schema_version'")
        before = hashlib.sha256(store.path.read_bytes()).hexdigest()
        with pytest.raises(ValueError, match="旧对话"):
            Store(tmp_path)
        assert hashlib.sha256(store.path.read_bytes()).hexdigest() == before
    finally:
        await service.close()


async def test_missing_model_fails_before_creating_turn_or_message(tmp_path):
    store = Store(tmp_path)
    card = store.import_card(Path("samples/雾港来信.json").read_bytes())
    session = store.new_session(SessionCreate(card_id=card["id"]))
    service = StoryService(store)
    try:
        with pytest.raises(ValueError, match="模型名称"):
            service.story.submit(
                session["id"], TurnRequest(text="不会写入", operation_id="missing", expected_revision=0)
            )
        assert len(store.session(session["id"])["messages"]) == 1
        with store.connection() as db:
            assert db.execute("SELECT count(*) FROM turns").fetchone()[0] == 0
    finally:
        await service.close()


async def test_unknown_turn_fields_are_rejected_before_queuing(tmp_path):
    app = create_app(tmp_path)
    store = app.state.store
    card = store.import_card(b'{"name":"fixture","first_mes":"opening"}')
    session = store.new_session(SessionCreate(card_id=card["id"]))
    body = {"operation_id": "legacy", "expected_revision": 0, "text": "继续"}
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as api:
            response = await api.post(f"/api/sessions/{session['id']}/turns", json=body | {"unknown": None})
            assert response.status_code == 422 and response.json()["detail"][0]["type"] == "extra_forbidden"
        assert store.session(session["id"]) == session
    finally:
        await app.state.service.close()


async def test_delete_session_removes_all_owned_state_and_run_files(tmp_path):
    store = Store(tmp_path)
    card = store.import_card(Path("samples/雾港来信.json").read_bytes())
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
    service = StoryService(store)
    try:
        await service.delete_session(session["id"])
    finally:
        await service.close()
    assert not list(runs.glob(f"{turn_id}*"))
    with store.connection() as db:
        for table in ("sessions", "messages", "turns", "events", "changes"):
            assert db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0


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


async def test_persona_details_are_pinned_and_used_in_context(tmp_path):
    from tavern.core.models import PersonaEdit

    store = Store(tmp_path)
    persona = store.save_persona(None, PersonaEdit(name="玩家甲", description="医生，温和，说话简短。"))
    card = store.import_card(Path("samples/雾港来信.json").read_bytes())
    s = store.new_session(SessionCreate(card_id=card["id"], persona_id=persona["id"]))
    assert s["persona"] == "玩家甲" and s["persona_data"]["description"].startswith("医生")
    store.save_persona(persona["id"], PersonaEdit(name="玩家乙", description="新描述", expected_version=1))
    assert store.session(s["id"])["persona"] == "玩家甲"


async def test_reasoning_strength_is_serialized_without_silent_fallback():
    import httpx
    from openai import AsyncOpenAI

    from tavern.core.models import Settings
    from tavern.core.provider import make_provider

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
