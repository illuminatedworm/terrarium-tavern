import json
from pathlib import Path

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from openai import AsyncOpenAI

from tavern.application.api import create_app
from tavern.application.service import RoleplayRuntime
from tavern.conversation.cache_breakpoint import context_breakpoint_index
from tavern.core.models import SessionCreate, TurnRequest
from tavern.memories.memory import fingerprint
from tavern.scripting.agent import make_provider
from tavern.scripting.responses import ResponsesProvider
from tavern.storage.store import Store, dump, uid


@pytest.fixture
def story(tmp_path):
    store = Store(tmp_path)
    card = store.import_card(Path("samples/雾港来信.json").read_bytes(), "card.json")
    session = store.new_session(SessionCreate(card_id=card["id"]))
    parent = session["head"]
    replies = []
    with store.connection() as db:
        for turn in range(3):
            for role in ("user", "assistant"):
                mid = uid()
                text = f"{role} {turn}"
                db.execute(
                    "INSERT INTO messages(id,session,parent,role,raw,model,display,snapshot,created) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (mid, session["id"], parent, role, text, text, text, dump(session["snapshot"]), turn + 1),
                )
                parent = mid
                if role == "assistant":
                    replies.append(mid)
        db.execute("UPDATE sessions SET head=? WHERE id=?", (parent, session["id"]))
    return store, store.session(session["id"]), replies


def test_auto_second_latest_reply_and_manual_position_survive_fork(story):
    store, session, replies = story
    assert session["cache_breakpoint_auto"] is True
    assert session["cache_breakpoint_effective_id"] == replies[1]
    manual = store.set_cache_breakpoint(session["id"], session["revision"], False, replies[0])
    assert manual["cache_breakpoint_auto"] is False
    assert manual["cache_breakpoint_effective_id"] == replies[0]
    assert Store(store.root).session(session["id"])["cache_breakpoint_message"] == replies[0]
    fork = store.fork_session(session["id"], manual["head"], manual["revision"])
    assert fork["cache_breakpoint_auto"] is False
    assert fork["cache_breakpoint_effective_id"] == fork["messages"][2]["id"]
    automatic = store.set_cache_breakpoint(session["id"], manual["revision"], True, replies[0])
    assert automatic["cache_breakpoint_message"] is None
    assert automatic["cache_breakpoint_effective_id"] == replies[1]


def test_clipped_target_falls_back_and_invalid_manual_target_is_rejected(story):
    store, session, replies = story
    with pytest.raises(ValueError):
        store.set_cache_breakpoint(session["id"], session["revision"], False, session["messages"][1]["id"])
    snapshot = session["snapshot"] | {
        "context_window": {"covered_count": 5, "fingerprint": fingerprint(session["messages"][:5])}
    }
    with store.connection() as db:
        db.execute("UPDATE messages SET snapshot=? WHERE id=?", (dump(snapshot), session["head"]))
    assert store.session(session["id"])["cache_breakpoint_effective_id"] is None
    manual = store.set_cache_breakpoint(session["id"], session["revision"], False, replies[0])
    assert manual["cache_breakpoint_effective_id"] is None


def test_request_marks_reply_itself_before_later_memory_and_notes():
    trace = [
        {"source": "lore:a", "lore_region": "constant", "role": "system"},
        {"source": "history", "role": "assistant", "message_id": "reply"},
        {"source": "memory", "role": "system"},
        {"source": "author_note", "role": "system"},
    ]
    assert context_breakpoint_index(trace, "reply") == 1
    assert context_breakpoint_index(trace, None) == 0
    provider = ResponsesProvider(api_key="test", model="fixture")
    provider.session_breakpoint = True
    try:
        messages = [
            {"role": "system", "content": "constant lore"},
            {"role": "assistant", "content": "reply", "_cache_breakpoint": True},
            {"role": "system", "content": "memory"},
            {"role": "system", "content": "author note"},
        ]
        request = provider._request(messages)
        assert request["prompt_cache_options"] == {"mode": "explicit"}
        assert request["input"][1]["content"] == [
            {"type": "input_text", "text": "reply", "prompt_cache_breakpoint": {"mode": "explicit"}}
        ]
        assert request["input"][2]["content"] == "memory"
        assert request["input"][3]["content"] == "author note"
        assert messages[1]["content"] == "reply"
    finally:
        import asyncio

        asyncio.run(provider.close())


async def test_cache_breakpoint_endpoint_and_schema_migration(story):
    store, session, replies = story
    app = create_app(store.root)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        result = await client.put(
            f"/api/sessions/{session['id']}/cache-breakpoint",
            json={"automatic": False, "message_id": replies[2], "expected_revision": session["revision"]},
        )
        assert result.status_code == 200
        assert result.json()["cache_breakpoint_effective_id"] == replies[2]
    with store.connection() as db:
        db.execute("ALTER TABLE sessions DROP COLUMN cache_breakpoint_message")
        db.execute("ALTER TABLE sessions DROP COLUMN cache_breakpoint_auto")
    reopened = Store(store.root)
    assert reopened.session(session["id"])["cache_breakpoint_auto"] is True
    assert reopened.session(session["id"])["cache_breakpoint_message"] is None


async def test_writer_turn_marks_second_latest_reply_in_responses_request(story):
    store, session, replies = story
    settings = store.settings()
    settings.foreground.api_mode = "responses"
    settings.providers[0].api_key = "test"
    settings.network.api_proxy = False
    settings.foreground.model = "gpt-fixture"
    settings.memory_interval_turns = 100
    store.save_settings(settings)
    provider = await make_provider(settings.for_role("foreground"))
    await provider._client.close()
    requests = []

    def route(request):
        requests.append(json.loads(request.content))
        final = {
            "id": "resp_test",
            "object": "response",
            "created_at": 0,
            "status": "completed",
            "model": "fixture",
            "usage": {"input_tokens": 100, "output_tokens": 10, "total_tokens": 110},
            "output": [
                {
                    "type": "message",
                    "id": "msg_test",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": "故事继续。", "annotations": []}],
                }
            ],
        }
        events = [
            {
                "type": "response.output_text.delta",
                "delta": "故事继续。",
                "item_id": "msg_test",
                "output_index": 0,
                "content_index": 0,
                "sequence_number": 1,
            },
            {"type": "response.completed", "response": final, "sequence_number": 2},
        ]
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text="".join(f"data: {json.dumps(event, ensure_ascii=False)}\n\n" for event in events),
        )

    provider._client = AsyncOpenAI(
        api_key="test", max_retries=0, http_client=httpx.AsyncClient(transport=httpx.MockTransport(route))
    )
    service = RoleplayRuntime(store)
    service.provider_factory = lambda: provider
    try:
        turn = service.submit(
            session["id"],
            TurnRequest(text="继续", operation_id="cache-breakpoint", expected_revision=session["revision"]),
        )
        await service.tasks[turn["id"]]
        assert store.turn(turn["id"])["status"] == "committed"
        committed = store.session(session["id"])
        assert committed["cache_breakpoint_effective_id"] == replies[1]
        assert committed["cache_breakpoint_follow"] == {
            "interval": 2,
            "offset": 2,
            "anchor": replies[1],
            "progress": 1,
        }
        assert len(requests) == 1
        wire = requests[0]
        assert wire["prompt_cache_options"] == {"mode": "explicit"}
        marked = [
            item
            for item in wire["input"]
            if isinstance(item.get("content"), list)
            and any(part.get("prompt_cache_breakpoint") for part in item["content"])
        ]
        assert len(marked) == 1
        assert marked[0]["role"] == "assistant"
        assert marked[0]["content"][0]["text"] == next(
            message["model"] for message in session["messages"] if message["id"] == replies[1]
        )
    finally:
        await service.close()
        await provider.close()
