import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from httpx import ASGITransport, AsyncClient

from tavern.application.api import create_app
from tavern.conversation.work_records import background_records
from tavern.core.models import SessionCreate, TurnRequest
from tavern.storage.store import uid


def story(tmp_path):
    app = create_app(tmp_path)
    store = app.state.store
    card = store.import_card(Path("samples/雾港来信.json").read_bytes(), "card.json")
    session = store.new_session(SessionCreate(card_id=card["id"]))
    return app, session


def test_turn_pagination_is_stable_with_equal_timestamps_and_newer_turns(tmp_path):
    app, session = story(tmp_path)
    store = app.state.store
    ids = []
    for i in range(5):
        tid, _ = store.create_turn(session["id"], TurnRequest(operation_id=str(i), expected_revision=0))
        store.set_turn(tid, status="cancelled")
        ids.append(tid)
    with store.connection() as db:
        db.execute("UPDATE turns SET created=1 WHERE session=?", (session["id"],))
    first = store.work_turns(session["id"], limit=2)
    tid, _ = store.create_turn(session["id"], TurnRequest(operation_id="new", expected_revision=0))
    pages = first["items"][:]
    cursor = first["next"]
    while cursor:
        page = store.work_turns(session["id"], before=cursor, limit=2)
        pages.extend(page["items"])
        cursor = page["next"]
    assert [t["id"] for t in pages] == sorted(ids, reverse=True)
    assert tid not in {t["id"] for t in pages}


def test_event_list_is_compact_paged_and_excludes_narrative_data(tmp_path):
    app, session = story(tmp_path)
    store = app.state.store
    tid, _ = store.create_turn(session["id"], TurnRequest(operation_id="events", expected_revision=0))
    store.event(tid, "maintenance_started", {})
    secret_large_content = "FULL_INPUT_DATA" * 10000
    seq = store.event(
        tid,
        "provider_request",
        {
            "purpose": "maintenance",
            "model": "fixture",
            "messages": [{"role": "user", "content": secret_large_content}],
            "tools": [],
        },
    )
    store.event(tid, "activity", {"kind": "tool_started", "message": "read_state"})
    store.event(tid, "provider_request", {"purpose": "narrative", "messages": []})
    store.event(tid, "activity", {"kind": "narrative_call"})
    store.event(tid, "text", {"text": "正文片段"})
    store.event(tid, "usage", {"purpose": "narrative", "usage": {}})
    store.event(tid, "committed", {})
    first = store.work_events(session["id"], tid, limit=2)
    second = store.work_events(session["id"], tid, after=first["next"], limit=2)
    assert [e["kind"] for e in first["items"] + second["items"]] == [
        "maintenance_started",
        "provider_request",
        "activity",
        "committed",
    ]
    assert first["items"][1]["bytes"] > 100000
    assert "FULL_INPUT_DATA" not in json.dumps(first)
    assert second["next"] is None
    assert store.work_event_payload(session["id"], tid, seq)["messages"][0]["content"] == secret_large_content


def test_explicit_activity_purpose_and_archive_events_are_classified():
    events = [
        {"seq": 1, "kind": "maintenance_started", "payload": {}},
        {"seq": 2, "kind": "activity", "payload": {"purpose": "narrative"}},
        {"seq": 3, "kind": "memory_ready", "payload": {"memory": {}}},
        {"seq": 4, "kind": "memory_proposal", "payload": {"record": {}}},
        {"seq": 5, "kind": "scene_refresh_ready", "payload": {"purpose": "memory", "result": []}},
    ]
    records = list(background_records(events))
    assert [e["seq"] for e in records] == [1, 3, 4, 5]
    assert [e["purpose"] for e in records] == ["maintenance", "memory", "memory", "memory"]


async def test_work_record_endpoints_reject_foreign_turns_and_bound_page_sizes(tmp_path):
    app, session = story(tmp_path)
    store = app.state.store
    other = store.new_session(SessionCreate(card_id=session["card"]))
    tid, _ = store.create_turn(session["id"], TurnRequest(operation_id="api", expected_revision=0))
    seq = store.event(tid, "memory_ready", {"memory": {"records": []}})
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        base = f"/api/sessions/{session['id']}/work-records"
        assert (await client.get(f"{base}?limit=1000")).status_code == 422
        assert (await client.get(base)).json()["items"][0]["id"] == tid
        assert (await client.get(f"{base}/{tid}/events")).json()["items"][0]["purpose"] == "memory"
        assert (await client.get(f"{base}/{tid}/events/{seq}")).json()["memory"]["records"] == []
        foreign = f"/api/sessions/{other['id']}/work-records/{tid}/events"
        assert (await client.get(foreign)).status_code == 404
        assert (await client.get(f"{foreign}/{seq}")).status_code == 404


async def test_event_stream_delivers_commit_that_arrives_while_sending_a_batch(tmp_path, monkeypatch):
    app, session = story(tmp_path)
    store = app.state.store
    tid, _ = store.create_turn(session["id"], TurnRequest(operation_id="stream", expected_revision=0))
    store.set_turn(tid, status="running")
    store.write_turn_draft(tid, "reply")
    endpoint = next(route.endpoint for route in app.routes if route.path == "/api/turns/{tid}/events")
    request = SimpleNamespace(headers={}, is_disconnected=AsyncMock(return_value=False))
    monkeypatch.setattr("tavern.application.api.asyncio.sleep", AsyncMock())
    response = await endpoint(tid, request, after=0)
    first = await anext(response.body_iterator)
    assert '"kind": "text"' in first

    # Sending a frame yields to the producer. Its atomic commit may become
    # visible after this batch was read but before the observer checks status.
    messages, parent = [], session["head"]
    for role, text in (("user", "continue"), ("assistant", "reply")):
        message = {
            "id": uid(),
            "parent": parent,
            "role": role,
            "raw": text,
            "model": text,
            "display": text,
            "snapshot": session["snapshot"],
        }
        messages.append(message)
        parent = message["id"]
    store.commit_turn(tid, {"before": session["snapshot"], "messages": messages})
    tail = [frame async for frame in response.body_iterator]
    assert any('"kind": "committed"' in frame for frame in tail)
    assert tail[-1].startswith("event: end")
