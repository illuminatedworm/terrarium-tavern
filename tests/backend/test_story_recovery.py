"""Manual retries discard failed exchanges at the persisted and native boundaries."""

import copy
import json
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from tavern.core.models import TurnRequest
from tavern.storage.legacy_story import OLD_STORY_KEY
from tavern.storage.store import Store
from tavern.story.activity import turn_items
from tavern.story.context import load_trace
from tavern.story.tools import AgentTools, Story
from tests.backend.test_story import (
    Model,
    current_text,
    handoff,
    native_reply,
    runtime,
    story_reply,
    tool_exchanges,
    turn,
)
from tests.backend.test_story_alignment import game, transport


@pytest.mark.parametrize("api", ["chat_completions", "responses", "anthropic", "gemini"])
async def test_retry_filters_mixed_failures_from_native_wire_activity_and_committed_trace(
    tmp_path, monkeypatch, api
):
    import tavern.story.agent as agent_module

    store, service, session = game(tmp_path, api)
    if api == "gemini":
        settings = store.settings_document()
        settings.story.model.model = "gemini-3-pro-preview"
        settings.story.model.cache.mode = "cloud"  # no explicit cache request in this fixture
        store.save_settings(settings)
    requests = []

    def route(request):
        requests.append(request)
        if len(requests) == 3:
            raise httpx.ReadTimeout("synthetic interruption", request=request)
        if len(requests) == 1:
            payload = native_reply(
                api,
                [
                    ("bad", "read_history", {"reason": "BAD_ARGUMENT"}),
                    ("accepted", "write_handoff", handoff("MEMORY_ACCEPTED", through=None)),
                ],
                marker="MIXED_NATIVE",
            )
        elif len(requests) == 2:
            payload = native_reply(api, [("read", "read_state", {})], marker="DEPENDENT_NATIVE")
        elif len(requests) in (4, 6 if api in ("responses", "gemini") else 5):
            payload = story_reply(api, "完成正文")
        else:
            payload = native_reply(api, text="收尾完成。")
        return httpx.Response(200, json=payload, headers={"x-codex-turn-state": "source-route"})

    async def factory(settings, capture=None, *, request_context=None):
        return transport(settings, route, request_context, capture)

    monkeypatch.setattr(agent_module, "make_provider", factory)
    try:
        failed = service.story.submit(
            session["id"], TurnRequest(text="继续", operation_id=uuid4().hex, expected_revision=0)
        )
        await service.story.tasks[failed["id"]]
        failed = store.turn(failed["id"])
        assert failed["status"] == "error", failed["error"]
        saved = store.story_progress(failed["id"])
        assert "BAD_ARGUMENT" in requests[1].content.decode()  # same-turn correction still sees the error
        assert "expected_parameters" not in requests[1].content.decode()
        # SDK serialization may escape Unicode; assert the actual JSON text.
        assert "缺少必填参数 ref" in json.dumps(json.loads(requests[1].content), ensure_ascii=False)
        assert "errors.pydantic.dev" not in requests[1].content.decode()
        returned = [item["message"] for item in saved["new_trace"] if item["message"]["role"] == "tool"]
        assert [m.get("_tool_error", False) for m in returned] == [True, False, False]
        if api == "anthropic":
            messages = json.loads(requests[1].content)["messages"]
            results = [
                part
                for message in messages
                for part in message.get("content", [])
                if isinstance(part, dict) and part.get("type") == "tool_result"
            ]
            assert [part.get("is_error", False) for part in results] == [True, False]
        else:
            assert "is_error" not in requests[1].content.decode()
        assert "_tool_error" not in requests[1].content.decode()
        assert saved["snapshot"]["story"]["handoff"]["revision"] == 1
        assert any(i["status"] == "error" for i in turn_items(store, session["id"], failed["id"]))
    finally:
        await service.close()

    store = Store(store.root)
    service = runtime(store)
    try:
        resumed = service.story.submit(
            session["id"],
            TurnRequest(
                text="继续", operation_id=uuid4().hex, expected_revision=0, resume_turn_id=failed["id"]
            ),
        )
        copied = store.story_progress(resumed["id"])
        assert "BAD_ARGUMENT" not in json.dumps(copied["continuation"])
        assert "BAD_ARGUMENT" not in json.dumps(copied["new_trace"])
        assert store.story_progress(failed["id"]) == saved
        await service.story.tasks[resumed["id"]]
        result = store.turn(resumed["id"])
        assert result["status"] == "committed", result["error"]
        assert len(requests) == (5 if api in ("responses", "gemini") else 4)
        body = requests[3].content.decode()
        assert "BAD_ARGUMENT" not in body and '"bad"' not in body
        assert "MIXED_NATIVE" not in body and "DEPENDENT_NATIVE" not in body
        assert "MEMORY_ACCEPTED" in body
        assert '"is_error":true' not in body and '"_tool_error"' not in body
        if api == "responses":
            assert requests[2].headers["x-codex-turn-state"] == "source-route"
            assert "x-codex-turn-state" not in requests[3].headers
        items = [i for i in turn_items(store, session["id"], resumed["id"]) if i["kind"] == "tool"]
        assert [(i["name"], i["status"]) for i in items] == [
            ("write_handoff", "returned"),
            ("read_state", "returned"),
            ("submit_story", "returned"),
        ]
        current = store.session(session["id"])
        memory = current["snapshot"]["story"]
        assert current_text(memory) == "MEMORY_ACCEPTED" and memory["handoff"]["revision"] == 1
        trace = tool_exchanges(load_trace(store, session["id"], current["messages"]))
        assert len(trace) == 4 and "BAD_ARGUMENT" not in json.dumps(trace)
        assert any(i["status"] == "error" for i in turn_items(store, session["id"], failed["id"]))
        following = service.story.submit(
            session["id"],
            TurnRequest(text="继续", operation_id=uuid4().hex, expected_revision=current["revision"]),
        )
        await service.story.tasks[following["id"]]
        assert store.turn(following["id"])["status"] == "committed"
        assert "BAD_ARGUMENT" not in requests[5 if api in ("responses", "gemini") else 4].content.decode()
    finally:
        await service.close()


async def test_retry_drops_failed_maintenance_and_survives_another_failure(tmp_path):
    store, service, session = game(tmp_path)
    try:
        failed, _ = await turn(
            service,
            session,
            Model(
                {
                    "content": "应保留的唯一正文",
                    "calls": [("write_handoff", {"content": "FAILED_PROPOSAL", "reason": "extra"})],
                },
                ("read_history", {"reason": "missing ref"}),
                RuntimeError("disconnected"),
            ),
        )
        saved = store.story_progress(failed["id"])
        retry_model = Model(RuntimeError("second interruption"))
        service.story.provider_factory = lambda: retry_model
        retry = service.story.submit(
            session["id"],
            TurnRequest(
                text=failed["request"]["text"],
                operation_id=uuid4().hex,
                expected_revision=0,
                resume_turn_id=failed["id"],
            ),
        )
        await service.story.tasks[retry["id"]]
        assert store.turn(retry["id"])["status"] == "error"
        assert not tool_exchanges(store.story_progress(retry["id"])["new_trace"])
        retained_calls = [
            c["function"]["name"]
            for m in retry_model.requests[0]["messages"]
            for c in m.get("tool_calls", [])
        ]
        assert retained_calls == ["submit_story"]
        assert "FAILED_PROPOSAL" not in json.dumps(retry_model.requests)
        stories = [
            json.loads(c["function"]["arguments"])["content"]
            for m in retry_model.requests[0]["messages"]
            for c in m.get("tool_calls", [])
            if c["function"]["name"] == "submit_story"
        ]
        assert stories == ["应保留的唯一正文"]
        assert store.story_progress(failed["id"]) == saved
        final_model = Model("")
        service.story.provider_factory = lambda: final_model
        final = service.story.submit(
            session["id"],
            TurnRequest(
                text=failed["request"]["text"],
                operation_id=uuid4().hex,
                expected_revision=0,
                resume_turn_id=retry["id"],
            ),
        )
        await service.story.tasks[final["id"]]
        assert store.turn(final["id"])["status"] == "committed"
        assert store.session(session["id"])["messages"][-1]["raw"] == "应保留的唯一正文"
        assert [
            c["function"]["name"]
            for m in final_model.requests[0]["messages"]
            for c in m.get("tool_calls", [])
        ] == ["submit_story"]
    finally:
        await service.close()


async def test_retry_keeps_unfinished_call_but_drops_its_failed_batch_sibling(tmp_path, monkeypatch):
    store, service, session = game(tmp_path)

    class Batch(Model):
        async def chat_complete(self, messages, **kwargs):
            from tavern.scripting.native_response import ResponseCompletion

            self.requests.append(copy.deepcopy(messages))
            calls = (
                [
                    ("story", "submit_story", {"content": "续接正文"}),
                    ("reused", "read_history", {"ref": "H2"}),
                ]
                if len(self.requests) == 1
                else [
                    ("bad", "read_history", {"reason": "missing ref"}),
                    ("accepted", "write_handoff", handoff("MEMORY_A", through=None)),
                    # A result in the previous response cannot complete this
                    # unfinished call merely because its provider ID is reused.
                    ("reused", "write_handoff", handoff("MEMORY_B", through=None)),
                ]
            )
            self.last_tool_calls = [
                SimpleNamespace(id=cid, name=name, arguments=json.dumps(args)) for cid, name, args in calls
            ]
            self.last_completion = ResponseCompletion(
                "tools" if len(self.requests) == 1 else "end", "fixture"
            )
            return SimpleNamespace(content="", usage={}, finish_reason="stop")

    original = AgentTools.execute
    interrupted, executed = False, []

    async def execute(tools, name, arguments):
        nonlocal interrupted
        content = next((p["text"] for p in json.loads(arguments).get("current", [])), None)
        if content == "MEMORY_B" and not interrupted:
            interrupted = True
            raise RuntimeError("interrupted before execution")
        if content:
            executed.append(content)
        return await original(tools, name, arguments)

    monkeypatch.setattr(AgentTools, "execute", execute)
    try:
        failed, _ = await turn(service, session, Batch())
        assert failed["status"] == "error"
        assert executed == ["MEMORY_A"]
        model = Model("")
        service.story.provider_factory = lambda: model
        resumed = service.story.submit(
            session["id"],
            TurnRequest(
                text=failed["request"]["text"],
                operation_id=uuid4().hex,
                expected_revision=0,
                resume_turn_id=failed["id"],
            ),
        )
        await service.story.tasks[resumed["id"]]
        result = store.turn(resumed["id"])
        assert result["status"] == "committed", result["error"]
        assert executed == ["MEMORY_A", "MEMORY_B"]
        assert len(model.requests) == 1
        sent = model.requests[0]["messages"]
        assert [(c["id"], c["function"]["name"]) for m in sent for c in m.get("tool_calls", [])] == [
            ("story", "submit_story"),
            ("reused", "read_history"),
            ("accepted", "write_handoff"),
            ("reused", "write_handoff"),
        ]
        assert [m["tool_call_id"] for m in sent if m["role"] == "tool"] == [
            "story",
            "reused",
            "accepted",
            "reused",
        ]
        assert store.session(session["id"])["messages"][-1]["raw"] == "续接正文"
        assert current_text(store.session(session["id"])["snapshot"]["story"]) == "MEMORY_B"
    finally:
        await service.close()


@pytest.mark.parametrize("old_checkpoint", [False, True], ids=["partial", "legacy-unicode-full"])
async def test_unicode_story_resume_decodes_unfinished_call_with_saved_format(
    tmp_path, monkeypatch, old_checkpoint
):
    store, service, session = game(tmp_path, "chat_completions")
    settings = store.settings_document()
    settings.shared.unicode_story_enabled = True
    store.save_settings(settings)
    with store.connection() as db:
        db.execute("UPDATE sessions SET unicode_story=1 WHERE id=?", (session["id"],))
    session = store.session(session["id"])
    first, last = "前段🙂", "再接一段。"
    if old_checkpoint:
        encoded_first, encoded_last = (
            " ".join(f"{ord(char):04X}" for char in text) for text in (first, last)
        )
    else:
        encoded_first, encoded_last = "前段⟦1F642⟧", "再接⟦4E00 6BB5⟧。"
    original = AgentTools.execute
    interrupted = False

    async def execute(tools, name, arguments):
        nonlocal interrupted
        if isinstance(arguments, Story) and arguments.content == last and not interrupted:
            interrupted = True
            raise RuntimeError("interrupted before receipt")
        return await original(tools, name, arguments)

    monkeypatch.setattr(AgentTools, "execute", execute)
    try:
        initial = Model(
            {
                "calls": [
                    ("submit_story", {"content": encoded_first}),
                    ("submit_story", {"content": encoded_last}),
                ]
            }
        )
        with monkeypatch.context() as settings_override:
            if old_checkpoint:
                settings_override.setattr("tavern.story.runtime.new_story_format", lambda *_: "unicode_full")
            failed, _ = await turn(service, session, initial)
        assert failed["status"] == "error"
        progress = store.story_progress(failed["id"])
        assert progress["story"]["content"] == first and progress["version"] == 7
        assert progress["story_format"] == ("unicode_full" if old_checkpoint else "unicode_partial")
        assert next(item["message"] for item in progress["new_trace"] if item["message"].get("tool_calls"))[
            "tool_calls"
        ][1]["function"]["arguments"] == json.dumps({"content": encoded_last})
        if old_checkpoint:
            progress["version"] = 6
            progress["unicode_story"] = True
            del progress["story_format"]
            store.save_story_progress(failed["id"], progress)
            with store.connection() as db:
                db.execute(
                    f"ALTER TABLE sessions ADD COLUMN agent_mode TEXT NOT NULL DEFAULT '{OLD_STORY_KEY}'"
                )
                db.execute("UPDATE meta SET value='24' WHERE key='schema_version'")
    finally:
        await service.close()

    store = Store(tmp_path)
    if old_checkpoint:
        assert list(store.root.glob("before-upgrade-schema24-*.sqlite3"))
    settings = store.settings_document()
    settings.shared.unicode_story_enabled = False
    store.save_settings(settings)
    service = runtime(store)
    model = Model("")
    service.story.provider_factory = lambda: model
    try:
        resumed = service.story.submit(
            session["id"],
            TurnRequest(
                text=failed["request"]["text"],
                operation_id=uuid4().hex,
                expected_revision=0,
                resume_turn_id=failed["id"],
            ),
        )
        await service.story.tasks[resumed["id"]]
        result = store.turn(resumed["id"])
        assert result["status"] == "committed", result["error"]
        sent = model.requests[0]
        assert sent["extra_body"]["tools"] == initial.requests[0]["extra_body"]["tools"]
        assert [
            json.loads(c["function"]["arguments"])["content"]
            for m in sent["messages"]
            for c in m.get("tool_calls", [])
            if c["function"]["name"] == "submit_story"
        ] == [first, last]
        current = store.session(session["id"])
        assert current["messages"][-1]["raw"] == first + "\n\n" + last
        assert current["unicode_story"]
        fork = store.fork_session(current["id"], current["head"], current["revision"])
        assert fork["unicode_story"] and fork["messages"][-1]["raw"] == current["messages"][-1]["raw"]
    finally:
        await service.close()


async def test_schema12_pending_delivery_resumes_without_repeating_saved_work(tmp_path):
    store, service, session = game(tmp_path)
    try:
        failed, _ = await turn(
            service,
            session,
            Model(("write_handoff", handoff("已完成的记忆", through=None)), RuntimeError("offline")),
        )
        legacy = store.story_progress(failed["id"])
        legacy["version"] = 2
        legacy["roleplay"] = legacy.pop("story")
        legacy["finish_requested"] = legacy.pop("model_completed")
        legacy.pop("legacy_protocol")
        message = {
            "role": "assistant",
            "content": "旧协议的过程说明",
            "tool_calls": [
                {"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}
                for cid, name, args in (
                    ("body", "roleplay", {"content": "旧进度中的唯一正文"}),
                    (
                        "note",
                        "write_note",
                        {"key": "本轮", "content": "人物推门进入", "kind": "fact", "sources": ["H3"]},
                    ),
                    ("end", "finish_turn", {}),
                )
            ],
        }
        legacy["continuation"].append(copy.deepcopy(message))
        legacy["new_trace"].append(
            {"window_id": legacy["snapshot"]["story"]["window"]["id"], "message": message}
        )
        for snapshot in (legacy["snapshot"], legacy["user"]["snapshot"]):
            snapshot[OLD_STORY_KEY] = snapshot.pop("story")
        store.save_story_progress(failed["id"], legacy)
        with store.connection() as db:
            db.execute(f"ALTER TABLE sessions ADD COLUMN agent_mode TEXT NOT NULL DEFAULT '{OLD_STORY_KEY}'")
            db.execute(
                "UPDATE turns SET request=json_set(request,'$.generate',NULL,'$.scene_refresh',NULL) WHERE id=?",
                (failed["id"],),
            )
            db.execute("UPDATE meta SET value='12' WHERE key='schema_version'")
    finally:
        await service.close()

    reopened = Store(store.root)
    service = runtime(reopened)

    def unexpected_model_request():
        raise AssertionError("Saved legacy delivery and finish require no model request")

    service.story.provider_factory = unexpected_model_request
    try:
        assert list(reopened.root.glob("before-upgrade-schema12-*.sqlite3"))
        resumed = service.story.submit(
            session["id"],
            TurnRequest(
                text=failed["request"]["text"],
                operation_id=uuid4().hex,
                expected_revision=0,
                resume_turn_id=failed["id"],
            ),
        )
        await service.story.tasks[resumed["id"]]
        result = reopened.turn(resumed["id"])
        assert result["status"] == "committed", result["error"]
        current = reopened.session(session["id"])
        assert current["messages"][-1]["raw"] == "旧进度中的唯一正文"
        assert current["messages"][-1]["id"] == legacy["output_id"]
        state = current["snapshot"]["story"]
        assert state["handoff"]["revision"] == 1
        assert state["notes"]["本轮"]["sources"] == [{"message_id": legacy["output_id"]}]
        assert "roleplay" in json.dumps(load_trace(reopened, session["id"], current["messages"]))
        follow = Model("下一轮正文")
        next_turn, _ = await turn(service, current, follow)
        assert next_turn["status"] == "committed", next_turn["error"]
        sent = follow.requests[0]["messages"]
        assert not any(
            c["function"]["name"] in ("roleplay", "finish_turn")
            for m in sent
            for c in m.get("tool_calls", [])
        )
        assert json.dumps(sent, ensure_ascii=False).count("旧进度中的唯一正文") == 1
    finally:
        await service.close()
