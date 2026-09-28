import json
from pathlib import Path

from httpx import ASGITransport, AsyncClient

from tavern.application.api import create_app
from tavern.core.models import SessionCreate, TurnRequest
from tavern.full_agent.activity import from_trace
from tavern.full_agent.context import load_trace
from tests.backend.test_full_agent import Model, runtime, turn


def setup(tmp_path):
    app = create_app(tmp_path)
    store = app.state.store
    card = store.import_card(Path("samples/雾港来信.json").read_bytes(), "card.json")
    settings = store.settings()
    settings.full_agent_config.auto_retrieval = False
    store.save_settings(settings)
    session = store.new_session(SessionCreate(card_id=card["id"], agent_mode="full_agent"))
    return app, store, runtime(store), session


async def test_dialogue_uses_durable_trace_and_pages_results_without_reasoning(tmp_path):
    app, store, service, session = setup(tmp_path)
    try:
        model = Model(("read_history", {"ref": "H2"}), "她点头应下。")
        model.last_assistant_extra_fields = {"reasoning_content": "PRIVATE_REASONING"}
        result, current = await turn(service, session, model)
        assert result["status"] == "committed"
        message_id = current["head"]
        large_result = "原始结果<>\n" * 5000
        with store.connection() as db:
            row = db.execute("SELECT payload FROM full_agent_trace WHERE message_id=? AND seq=2", (message_id,)).fetchone()
            payload = json.loads(row["payload"])
            payload["_archive_content"] = large_result
            db.execute("UPDATE full_agent_trace SET payload=? WHERE message_id=? AND seq=2",
                       (json.dumps(payload), message_id))
            db.execute("DELETE FROM events WHERE turn=?", (result["id"],))
        recovered = load_trace(store, session["id"], current["messages"])
        assert recovered[1]["message"]["content"] == large_result
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
            prefix = f"/api/sessions/{session['id']}/full-agent"
            response = await client.post(prefix + "/activity", json={"message_ids": [message_id]})
            assert response.status_code == 200
            items = response.json()["messages"][message_id]
            assert [item["kind"] for item in items] == ["assistant", "tool"]
            assert items[1]["status"] == "returned"
            assert "PRIVATE_REASONING" not in response.text and large_result[:500] not in response.text
            endpoint = f"{prefix}/messages/{message_id}/activity/{items[1]['id']}"
            arguments = await client.get(endpoint, params={"part": "arguments"})
            assert json.loads(arguments.json()["text"]) == {"ref": "H2"}
            offset, chunks = 0, []
            while offset is not None:
                page = (await client.get(endpoint, params={"part": "result", "offset": offset})).json()
                assert len(page["text"]) <= 12000
                chunks.append(page["text"])
                offset = page["next"]
            assert "".join(chunks) == large_result
            assert (await client.get(endpoint, params={"part": "reasoning"})).status_code == 422
    finally:
        await service.close()


async def test_dialogue_activity_follows_selected_versions_and_fork_identity(tmp_path):
    app, store, service, session = setup(tmp_path)
    try:
        _, first = await turn(service, session, Model(("read_history", {"ref": "H2"}), "第一版"))
        fork = store.fork_session(first["id"], first["head"], first["revision"])
        _, second = await turn(service, first, Model(("write_checkpoint", {"content": "新的交接"}), "第二版"), mode="regenerate")
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
            async def fetch(sid, mid):
                return await client.post(f"/api/sessions/{sid}/full-agent/activity", json={"message_ids": [mid]})
            assert (await fetch(first["id"], first["head"])).status_code == 404
            current = (await fetch(second["id"], second["head"])).json()["messages"][second["head"]]
            assert [item["name"] for item in current if item["kind"] == "tool"] == ["write_checkpoint"]
            inherited = (await fetch(fork["id"], fork["head"])).json()["messages"][fork["head"]]
            assert [item["name"] for item in inherited if item["kind"] == "tool"] == ["read_history"]
            assert (await fetch(fork["id"], first["head"])).status_code == 404
            switched = store.switch_message_version(second["id"], second["head"], first["head"], second["revision"])
            assert (await fetch(switched["id"], first["head"])).status_code == 200
            assert (await fetch(switched["id"], second["head"])).status_code == 404
    finally:
        await service.close()


async def test_pending_activity_survives_reopen_and_marks_unfinished_calls(tmp_path):
    app, store, service, session = setup(tmp_path)
    try:
        tid, _ = store.create_turn(session["id"], TurnRequest(operation_id="partial", expected_revision=0))
        store.event(tid, "full_agent_tool_started", {"activity_id": "call-1-0", "order": 11,
                    "name": "search_lore", "arguments": '{"query":"钟楼"}'})
        store.set_turn(tid, status="cancelled")
        other = store.new_session(SessionCreate(card_id=session["card"], agent_mode="full_agent"))
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
            path = f"/api/sessions/{session['id']}/full-agent/turns/{tid}/activity"
            items = (await client.get(path)).json()["items"]
            assert len(items) == 1 and items[0]["status"] == "interrupted"
            assert items[0]["summary"] == "钟楼"
            args = (await client.get(path + "/call-1-0?part=arguments")).json()
            assert "钟楼" in args["text"]
            assert (await client.get(path.replace(session["id"], other["id"]))).status_code == 404
    finally:
        await service.close()


def test_batch_calls_pair_results_by_id_and_preserve_request_order():
    def row(seq, message):
        return {"seq": seq, "payload": json.dumps(message)}
    items = from_trace([
        row(1, {"role": "assistant", "tool_calls": [
            {"id": "a", "function": {"name": "read_state", "arguments": "{}"}},
            {"id": "b", "function": {"name": "read_note", "arguments": '{"key":"钟楼"}'}}]}),
        row(2, {"role": "tool", "tool_call_id": "b", "content": '{"error":"未找到"}'}),
        row(3, {"role": "tool", "tool_call_id": "a", "content": '{"energy":4}'}),
    ])
    assert [item["name"] for item in items] == ["read_state", "read_note"]
    assert [item["status"] for item in items] == ["returned", "error"]
    assert json.loads(items[0]["details"]["result"]) == {"energy": 4}
