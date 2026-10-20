import asyncio
import json
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from tavern.application.api import create_app
from tavern.core.models import SessionCreate, TurnRequest
from tavern.storage.store import Conflict, dump, uid
from tests.support.tracker_model import MemoryModel, batch_payload


@pytest.fixture
def story(tmp_path):
    app = create_app(tmp_path)
    store = app.state.store
    settings = store.settings()
    settings.foreground.model = settings.background.model = "fixture"
    settings.providers[0].api_key = "local"
    store.save_settings(settings)
    card = store.import_card(Path("samples/雾港来信.json").read_bytes(), "card.json")
    session = store.new_session(SessionCreate(card_id=card["id"]))
    snapshot = session["snapshot"] | {
        "state": {"keys": 1},
        "scopes": {"character": {"keep": True}},
    }
    with store.connection() as db:
        parent = session["head"]
        for role, raw in [("user", "钥匙交给乙"), ("assistant", "乙收好钥匙")]:
            mid = uid()
            db.execute(
                "INSERT INTO messages(id,session,parent,role,raw,model,display,snapshot,created) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    mid,
                    session["id"],
                    parent,
                    role,
                    raw,
                    raw,
                    raw,
                    dump(snapshot),
                    1,
                ),
            )
            parent = mid
        db.execute("UPDATE sessions SET head=? WHERE id=?", (parent, session["id"]))
    return app, store.session(session["id"])


def output(kind, session, source=None):
    source = source or "H2#1"
    return json.dumps(
        {
            "operations": [
                {
                    "op": "add",
                    "id": "N1",
                    "record": {"category": "current", "content": "乙持有钥匙", "sources": [source]},
                }
            ],
            "questions": [],
        }
    )


async def refresh(app, session, kind):
    service = app.state.service
    turn = service.submit(
        session["id"],
        TurnRequest(
            operation_id=uid(),
            expected_revision=session["revision"],
            scene_refresh=kind,
        ),
    )
    await service.tasks[turn["id"]]
    return app.state.store.turn(turn["id"])


@pytest.mark.parametrize("kind", ["memory"])
async def test_manual_refresh_rebuilds_from_full_history_without_story_generation(story, kind):
    app, before = story
    provider = MemoryModel([output(kind, before)])
    app.state.service.memory_provider_factory = lambda: provider
    try:
        turn = await refresh(app, before, kind)
        assert turn["status"] == "committed", turn["error"]
        after = app.state.store.session(before["id"])
        assert after["head"] == before["head"] and after["revision"] == before["revision"] + 1
        assert [{k: v for k, v in m.items() if k != "snapshot"} for m in after["messages"]] == [
            {k: v for k, v in m.items() if k != "snapshot"} for m in before["messages"]
        ]
        assert after["messages"][:-1] == before["messages"][:-1]
        assert {
            k: v for k, v in after["snapshot"].items() if k not in ("memory_archive", "memory_document")
        } == {k: v for k, v in before["snapshot"].items() if k not in ("memory_archive", "memory_document")}
        assert (
            after["snapshot"]["memory_document"]["records"][0]["sources"][0]["message_id"]
            == before["messages"][-2]["id"]
        )
        assert after["snapshot"]["memory_document"]["records"][0]["content"] == "乙持有钥匙"
        assert provider.call_count == 2
        payload = batch_payload(provider.call_log[0])
        assert payload["previous_result"] == {"version": 1, "records": []}
        assert ["".join(p["text"] for p in m["passages"]) for m in payload["messages"]] == [
            m["model"] for m in before["messages"]
        ]
        assert app.state.service.runtime.browser is None
        assert not any(e["kind"] == "text" for e in app.state.store.events(turn["id"]))
    finally:
        await app.state.service.close()


@pytest.mark.parametrize("kind", ["memory"])
async def test_invalid_critical_source_blocks_refresh(story, kind):
    app, before = story
    app.state.service.memory_provider_factory = lambda: MemoryModel([output(kind, before, "foreign-id")])
    try:
        turn = await refresh(app, before, kind)
        assert turn["status"] == "error"
        assert app.state.store.session(before["id"])["snapshot"] == before["snapshot"]
    finally:
        await app.state.service.close()


@pytest.mark.parametrize("kind", ["memory"])
async def test_memory_refresh_does_not_require_content_review(story, kind):
    app, before = story
    store, service = app.state.store, app.state.service
    before = store.update_session(before["id"], before["revision"], policy="preview")
    service.memory_provider_factory = lambda: MemoryModel([output(kind, before)])
    try:
        turn = await refresh(app, before, kind)
        assert turn["status"] == "committed"
        after = store.session(before["id"])
        assert len(after["messages"]) == len(before["messages"])
        assert after["snapshot"] != before["snapshot"]
        assert after["revision"] == before["revision"] + 1
    finally:
        await service.close()


async def test_refresh_can_be_cancelled_and_rejects_duplicate_clicks(story):
    app, before = story
    entered = asyncio.Event()

    class BlockedProvider:
        async def chat_complete(self, messages, **kwargs):
            entered.set()
            await asyncio.Event().wait()

        async def close(self):
            pass

    service = app.state.service
    service.memory_provider_factory = BlockedProvider
    try:
        req = TurnRequest(operation_id="first", expected_revision=before["revision"], scene_refresh="memory")
        turn = service.submit(before["id"], req)
        await asyncio.wait_for(entered.wait(), 2)
        assert service.submit(before["id"], req)["id"] == turn["id"]
        with pytest.raises(Conflict):
            service.submit(before["id"], req.model_copy(update={"operation_id": "second"}))
        await service.cancel(turn["id"])
        assert app.state.store.turn(turn["id"])["status"] == "cancelled"
        assert app.state.store.session(before["id"])["snapshot"] == before["snapshot"]
    finally:
        await service.close()


async def test_scene_regeneration_endpoint(story):
    app, before = story
    app.state.service.memory_provider_factory = lambda: MemoryModel([output("memory", before)])
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
            url = f"/api/sessions/{before['id']}/scene"
            req = {"operation_id": "manual", "expected_revision": before["revision"]}
            assert (await client.post(f"{url}/other/regenerate", json=req)).status_code == 422
            response = await client.post(f"{url}/memory/regenerate", json=req)
            assert response.status_code == 202
            turn = response.json()
            await app.state.service.tasks[turn["id"]]
            assert app.state.store.turn(turn["id"])["status"] == "committed"
    finally:
        await app.state.service.close()
