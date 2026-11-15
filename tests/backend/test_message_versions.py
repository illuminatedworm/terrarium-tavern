import asyncio
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from tavern.application.api import create_app
from tavern.core.models import OpeningEdit, SessionCreate, TurnRequest
from tavern.storage.store import Conflict, Store, uid


@pytest.fixture
def story(tmp_path):
    store = Store(tmp_path)
    card = store.import_card(Path("samples/雾港来信.json").read_bytes())
    return store, store.new_session(SessionCreate(card_id=card["id"]))


def generate(store, session, text="相同正文", regenerate=False):
    req = TurnRequest(
        text="继续",
        expected_revision=session["revision"],
        operation_id=uid(),
        mode="regenerate" if regenerate else "send",
        message_id=session["head"] if regenerate else None,
    )
    tid, _ = store.create_turn(session["id"], req)
    store.set_turn(tid, status="running")
    parent = session["messages"][-1]["parent"] if regenerate else uid()
    snapshot = {**session["snapshot"], "state": {"generation": tid}}
    reply = dict(
        id=uid(), parent=parent, role="assistant", raw=text, model=text, display=text, snapshot=snapshot
    )
    messages = (
        [reply]
        if regenerate
        else [
            dict(
                id=parent,
                parent=session["head"],
                role="user",
                raw="继续",
                model="继续",
                display="继续",
                snapshot=session["snapshot"],
            ),
            reply,
        ]
    )
    pending = dict(messages=messages, before=session["snapshot"])
    store.commit_turn(tid, pending)
    store.commit_turn(tid, pending)  # Retrying a committed transaction must not add a version.
    return store.session(session["id"])


def test_versions_persist_include_identical_generations_and_restore_state(story):
    store, session = story
    first = generate(store, session)
    second = generate(store, first, regenerate=True)
    ids = [first["head"], second["head"]]
    assert second["messages"][-1]["versions"] == ids
    store = Store(store.root)
    selected = store.switch_message_version(second["id"], second["head"], first["head"], second["revision"])
    assert selected["snapshot"] == first["snapshot"]
    assert selected["messages"][-1]["version_index"] == 0
    third = generate(store, selected, regenerate=True)
    assert third["messages"][-1]["versions"] == ids + [third["head"]]
    restored = store.switch_message_version(third["id"], third["head"], second["head"], third["revision"])
    continued = generate(store, restored)
    assert continued["messages"][-2]["parent"] == second["head"]
    assert continued["messages"][-1]["versions"] == [continued["head"]]
    with pytest.raises(ValueError, match="最新"):
        store.switch_message_version(continued["id"], second["head"], first["head"], continued["revision"])
    with pytest.raises(ValueError, match="不属于"):
        store.switch_message_version(continued["id"], continued["head"], first["head"], continued["revision"])


def test_failed_cancelled_and_active_turns(story):
    store, session = story
    first = generate(store, session)
    second = generate(store, first, regenerate=True)
    for status in ("error", "cancelled", "running", "awaiting_review"):
        tid, _ = store.create_turn(
            second["id"], TurnRequest(text="继续", expected_revision=second["revision"], operation_id=uid())
        )
        store.set_turn(tid, status=status, draft="未提交草稿")
        if status in ("running", "awaiting_review"):
            with pytest.raises(Conflict):
                store.switch_message_version(second["id"], second["head"], first["head"], second["revision"])
        store.set_turn(tid, status="cancelled")
    assert store.session(second["id"])["messages"][-1]["versions"] == [first["head"], second["head"]]
    with pytest.raises(Conflict):
        store.switch_message_version(second["id"], second["head"], first["head"], first["revision"])


async def test_version_endpoint(story):
    store, session = story
    first = generate(store, session)
    second = generate(store, first, regenerate=True)
    app = create_app(store.root)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.put(
            f"/api/sessions/{second['id']}/messages/{second['head']}/version",
            json={"head": first["head"], "expected_revision": second["revision"]},
        )
    assert response.status_code == 200
    assert response.json()["head"] == first["head"]


async def test_opening_save_cannot_overwrite_failed_or_concurrent_work(tmp_path):
    from tavern.advisor.runtime import ConsultationRequest
    from tavern.application.context_operations import ContextOperationRequest
    from tests.backend.test_story import Model, ScriptHost

    app = create_app(tmp_path)
    store, service, advisor = app.state.store, app.state.service, app.state.advisor
    card = store.import_card(b'{"name":"fixture","first_mes":"opening"}')
    session = store.new_session(SessionCreate(card_id=card["id"]))
    started, release = asyncio.Event(), asyncio.Event()

    class OpeningHost(ScriptHost):
        fail = True

        async def open(self, *args, **kwargs):
            if self.fail:
                raise ValueError("invalid opening")
            started.set()
            await release.wait()
            return await super().open(*args, **kwargs)

    service.story.runtime = host = OpeningHost()
    edit = OpeningEdit(text="new opening", expected_revision=0)
    try:
        with pytest.raises(ValueError, match="invalid opening"):
            await service.edit_opening(session["id"], edit)
        assert store.session(session["id"])["messages"] == session["messages"]
        host.fail = False
        save = asyncio.create_task(service.edit_opening(session["id"], edit))
        await started.wait()
        turn, _ = store.create_turn(
            session["id"], TurnRequest(text="continue", operation_id=uid(), expected_revision=0)
        )
        store.set_turn(turn, status="cancelled")
        release.set()
        with pytest.raises(Conflict, match="已启动模型任务"):
            await save
        assert store.session(session["id"])["messages"] == session["messages"]
        reopened = Store(tmp_path)
        reopened.recover()
        assert not reopened.session(session["id"])["opening_editable"]
        fork = reopened.fork_session(session["id"], session["head"], 0)
        assert not fork["opening_editable"]

        # Consultation and context operations own independent task tables. Their
        # cancellation/clearing must not reopen text they have already captured.
        consultation = store.new_session(SessionCreate(card_id=card["id"]))
        advisor.provider_factory = lambda: Model("advice")
        run = advisor.submit(
            consultation["id"], ConsultationRequest(text="discuss", operation_id=uid(), expected_revision=0)
        )
        await advisor.cancel(run["id"])
        advisor.data.clear(consultation["id"], advisor.data.get(consultation["id"])["revision"])
        assert not store.session(consultation["id"])["opening_editable"]
        official = store.new_session(SessionCreate(card_id=card["id"], channel_origin="chatgpt"))
        operation = service.context_operations.start(
            official["id"],
            ContextOperationRequest(kind="enable_compact", operation_id=uid(), expected_revision=0),
        )
        await service.context_operations.cancel(operation["id"])
        assert not store.session(official["id"])["opening_editable"]
    finally:
        release.set()
        await advisor.close()
        await service.close()
