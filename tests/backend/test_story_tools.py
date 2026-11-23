import asyncio
import io
import json
import re
import sqlite3

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from tavern.application.api import create_app
from tavern.core.models import SessionCreate, TurnRequest
from tavern.media.config import SPECS as MEDIA_SPECS
from tavern.novel.tools import MAIN_SPECS
from tavern.storage.legacy_story import OLD_STORY_KEY
from tavern.storage.store import Store
from tavern.story.activity import turn_items
from tavern.story.agent import StoryAgent
from tavern.story.argument_errors import ToolArgumentError
from tavern.story.handoff import (
    HandoffError,
    HandoffWrite,
    apply_write,
    render_text,
    render_written_current,
    require_coverage,
)
from tavern.story.tools import SPECS
from tests.backend import test_story
from tests.backend.test_story import Model, current_text, handoff, runtime, turn

story = test_story.story


def test_android_calculations_serialize_on_each_backend_loop(monkeypatch):
    from tavern.scripting import calculation

    busy = False

    async def render(payload):
        nonlocal busy
        assert not busy
        busy = True
        try:
            await asyncio.sleep(0)  # Let the other story queue on the same renderer.
            return {"data": {"value": payload["code"]}}
        finally:
            busy = False

    monkeypatch.setattr(calculation, "is_android", lambda: True)
    monkeypatch.setattr(calculation, "_android", render)

    async def two_stories():
        return await asyncio.gather(calculation.calculate("1", False), calculation.calculate("2", False))

    for _ in range(2):  # Android restarts the server in the same interpreter.
        assert asyncio.run(two_stories()) == [{"value": "1"}, {"value": "2"}]


async def test_dice_receipt_is_durable_before_publication_and_only_regeneration_redraws(story, monkeypatch):
    from tavern.scripting.calculation import calculate

    store, session = story
    service = runtime(store)
    draws = []
    original_event = store.event

    async def draw(code, summary):
        result = await calculate(code, summary)
        draws.append(result)
        return result

    def disconnect_after_first_roll(tid, kind, payload, **kwargs):
        event = original_event(tid, kind, payload, **kwargs)
        if kind == "story_tool" and payload["name"] == "calculate" and len(draws) == 1:
            raise ConnectionError("interrupted immediately after the visible roll")
        return event

    monkeypatch.setattr("tavern.story.tools.calculate", draw)
    monkeypatch.setattr(store, "event", disconnect_after_first_roll)
    call = (
        "calculate",
        {
            "code": "const check=roll('2d20kh1'); check.pools[0].dice=[]; "
            "return {source:'a potion of healing', damage_after_absorbing_the_armor:check.total+3, "
            f"checksum:'{session['card'][:16]}'+'{session['card'][16:]}'}};"
        },
    )
    try:
        failed, _ = await turn(service, session, Model(call))
        assert failed["status"] == "error" and len(draws) == 1
        resumed_model = Model("第一次检定")
        resumed, current = await turn(
            service, session, resumed_model, resume_turn_id=failed["id"], operation_id="resume-roll"
        )
        assert resumed["status"] == "committed", resumed["error"]
        actual = [
            json.loads(m["content"]) for m in resumed_model.requests[0]["messages"] if m["role"] == "tool"
        ]
        assert len(draws) == 1
        value = draws[0]["value"]
        ref = actual[-1]["data"]["value"]["checksum"]
        assert re.fullmatch(r"@D\d+", ref)
        assert current["snapshot"]["story"]["model_references"][ref] == value["checksum"] == session["card"]
        assert actual[-1]["data"] == {**draws[0], "value": {**value, "checksum": ref}}
        # The program edited its own copy. The original faces must survive
        # both that edit and the interruption.
        dice = draws[0]["rolls"][0]["pools"][0]["dice"]
        assert len(dice) == 2 and sum(not d.get("dropped") for d in dice) == 1
        assert value["damage_after_absorbing_the_armor"] == max(d["value"] for d in dice) + 3
        regenerated, _ = await turn(service, current, Model(call, "另一版检定"), mode="regenerate")
        assert regenerated["status"] == "committed", regenerated["error"]
        assert len(draws) == 2
    finally:
        await service.close()


def media_story(store, session):
    from tavern.media.config import MediaConfig
    from tavern.media.storage import save_config

    settings = store.settings()
    settings.providers[0].api_key = "media-fixture-key"
    store.save_settings(settings)
    config = MediaConfig()
    config.speech.enabled = config.image.enabled = config.view_images = True
    config.speech.provider_id = config.image.provider_id = settings.providers[0].id
    config.image.quality = "low"
    save_config(store, session["id"], session["revision"], config)
    return store.session(session["id"])


async def test_media_intents_commit_with_review_and_keep_original_parameters(story):
    """Rejected drafts must not create paid work or overwrite the saved speaker table."""
    from tavern.media.storage import config, jobs, save_config

    store, session = story
    session = media_story(store, session)
    with store.connection() as db:
        db.execute("UPDATE sessions SET policy='review' WHERE id=?", (session["id"],))
    session = store.session(session["id"])
    service = runtime(store)
    spoken = "  单独交给配音的文本。\n保留尾部空白  "

    class ReviewScript(test_story.Script):
        async def call(self, method, *args):
            if method == "process" and args[1]:
                self.mirror["state"]["stat_data"] = {"energy": 3}
            return await super().call(method, *args)

    class ReviewHost(test_story.ScriptHost):
        async def open(self, document, history, snapshot, *args, **kwargs):
            return ReviewScript(history, snapshot)

    service.story.runtime = ReviewHost()

    def model():
        return Model(
            ("generate_speech", {"text": spoken, "speaker": "艾琳", "voice": "marin", "delivery": "轻声"}),
            ("generate_image", {"prompt": "雨中的灯塔", "size": "1536x1024"}),
            ("read_state", {}),
            (
                "patch_state",
                {"operations": [{"op": "add", "path": "/energy", "value": 3}], "comment": "体力变化"},
            ),
            "正文独立交付。",
        )

    try:
        rejected, _ = await turn(
            service, session, model(), update_variables=True, operation_id="media-reject"
        )
        assert rejected["status"] == "awaiting_review", rejected["error"]
        with store.connection() as db:
            assert db.execute("SELECT count(*) FROM media_jobs").fetchone()[0] == 0
        assert config(store, session["id"]).voices == {}
        store.review_turn(rejected["id"], False)
        accepted, _ = await turn(
            service, session, model(), update_variables=True, operation_id="media-accept"
        )
        assert accepted["status"] == "awaiting_review", accepted["error"]
        pending = accepted["pending"]
        store.review_turn(accepted["id"], True)
        store.commit_turn(accepted["id"], pending)
        current = store.session(session["id"])
        tasks = jobs(store, session["id"], [current["head"]])
        assert len(tasks) == 2 and {job["status"] for job in tasks} == {"queued"}
        assert tasks[0]["request"]["text"] == spoken
        assert current["messages"][-1]["raw"] == "正文独立交付。"
        assert config(store, session["id"]).voices == {"艾琳": "marin"}
        edited = config(store, session["id"])
        edited.image.quality = "high"
        save_config(store, session["id"], current["revision"], edited)
        assert jobs(store, session["id"], [current["head"]])[1]["request"]["connection"]["quality"] == "low"
    finally:
        await service.close()


async def test_media_recovery_and_manual_operations_do_not_duplicate_paid_work(story, monkeypatch):
    """An uncertain request never auto-replays; duplicate manual operations share one intent."""
    import wave

    from tavern.media import storage
    from tavern.media.providers import ADAPTERS, MediaResult
    from tavern.media.service import MediaService

    store, session = story
    session = media_story(store, session)
    config = storage.config(store, session["id"])
    config.voices["艾琳"] = "marin"
    storage.save_config(store, session["id"], session["revision"], config)
    request = {
        "connection": storage.connection_snapshot(store.settings(), config.speech),
        "text": "请留下。",
        "speaker": "艾琳",
        "voice": "marin",
        "delivery": "平静",
    }
    with store.connection() as db:
        identity = storage.create_job(db, session["id"], session["head"], "speech", request)
        db.execute("UPDATE media_jobs SET status='running' WHERE id=?", (identity,))
    restarted = MediaService(store)
    await restarted.start()
    assert storage.job(store, session["id"], identity)["status"] == "interrupted"
    await restarted.close()
    calls = []
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
        audio.writeframes(b"\0\0" * 64)

    class Adapter:
        async def generate(self, payload, **kwargs):
            calls.append(payload)
            if len(calls) > 1:
                raise ConnectionError("响应丢失")
            return MediaResult(output.getvalue(), "audio/wav")

    monkeypatch.setitem(ADAPTERS, "openai_realtime", Adapter())
    worker = MediaService(store)

    async def execute(jid):
        with store.connection() as db:
            db.execute("UPDATE media_jobs SET status='running' WHERE id=?", (jid,))
            row = dict(db.execute("SELECT * FROM media_jobs WHERE id=?", (jid,)).fetchone())
        await worker.run(row)

    try:
        first = storage.retry(store, session["id"], identity, operation_id="retry-one")
        assert storage.retry(store, session["id"], identity, operation_id="retry-one")["id"] == first["id"]
        await execute(first["id"])
        original = storage.job(store, session["id"], identity)
        assert original["status"] == "completed" and len(calls) == 1
        config.voices["艾琳"] = "cedar"
        storage.save_config(store, session["id"], store.session(session["id"])["revision"], config)
        redo = storage.retry(store, session["id"], identity, regenerate=True, operation_id="redo-one")
        assert (
            storage.retry(store, session["id"], identity, regenerate=True, operation_id="redo-one")["id"]
            == redo["id"]
        )
        assert redo["request"]["voice"] == "cedar" and original["request"]["voice"] == "marin"
        await execute(redo["id"])
        assert storage.job(store, session["id"], redo["id"])["status"] == "failed"
        assert storage.job(store, session["id"], identity)["output"] == original["output"]
        assert len(calls) == 2
    finally:
        await worker.close()


def queue_media_images(store, session, *prompts):
    from tavern.media import storage

    sid = session["id"]
    connection = storage.connection_snapshot(store.settings(), storage.config(store, sid).image)
    with store.connection() as db:
        return [
            storage.create_job(
                db, sid, session["head"], "image", {"connection": connection, "prompt": prompt}
            )
            for prompt in prompts
        ]


async def test_media_delete_prevents_claims_until_the_story_is_removed(story, monkeypatch):
    from tavern.media import storage
    from tavern.media.providers import ADAPTERS

    store, session = story
    session = media_story(store, session)
    other = media_story(store, store.new_session(SessionCreate(card_id=session["card"])))
    _, _, queued = queue_media_images(store, session, "first", "second", "queued")
    queue_media_images(store, other, "other")
    two_started, other_started = asyncio.Event(), asyncio.Event()
    calls = []

    class Adapter:
        async def generate(self, payload, **kwargs):
            calls.append(payload["prompt"])
            if len(calls) == 2:
                two_started.set()
            if payload["prompt"] == "other":
                other_started.set()
            await asyncio.Future()

    monkeypatch.setitem(ADAPTERS, "openai_images", Adapter())
    service = runtime(store)
    deleting = None
    try:
        await service.media.start()
        await asyncio.wait_for(two_started.wait(), timeout=1)
        # Hold the final deletion so the dispatcher has a chance to claim both
        # the soon-to-be-deleted job and an unrelated job.
        async with service.dependencies.lock:
            deleting = asyncio.create_task(service.delete_session(session["id"]))
            await asyncio.wait_for(other_started.wait(), timeout=1)
            assert storage.job(store, session["id"], queued)["status"] == "queued"
        await deleting
        assert "queued" not in calls
    finally:
        if deleting:
            await asyncio.gather(deleting, return_exceptions=True)
        await service.close()


async def test_media_cancel_before_workers_start_frees_the_queue(story, monkeypatch):
    from tavern.media import storage
    from tavern.media.providers import ADAPTERS
    from tavern.media.service import MediaService

    store, session = story
    session = media_story(store, session)
    sid = session["id"]
    first, second, _ = queue_media_images(store, session, "first", "second", "next")
    next_started, calls = asyncio.Event(), []

    class Adapter:
        async def generate(self, payload, **kwargs):
            calls.append(payload["prompt"])
            next_started.set()
            await asyncio.Future()

    monkeypatch.setitem(ADAPTERS, "openai_images", Adapter())
    worker = MediaService(store)
    try:
        await worker.start()
        # This task runs after the dispatcher claims jobs and before the
        # worker tasks it schedules get their first instructions.
        await asyncio.create_task(worker.cancel_running(sid))
        assert [storage.job(store, sid, jid)["status"] for jid in (first, second)] == ["cancelled"] * 2
        await asyncio.wait_for(next_started.wait(), timeout=1)
        assert calls == ["next"]
    finally:
        await worker.close()


def test_media_retry_checks_the_switch_under_the_enqueue_lock(story, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from contextlib import contextmanager
    from threading import Event

    from tavern.media import storage

    store, session = story
    session = media_story(store, session)
    sid = session["id"]
    (jid,) = queue_media_images(store, session, "retry")
    with store.connection() as db:
        db.execute("UPDATE media_jobs SET status='failed' WHERE id=?", (jid,))
    config = storage.config(store, sid)
    config.image.enabled = False
    enqueuing = Event()
    connection = store.connection

    @contextmanager
    def observe_enqueue():
        with connection() as db:
            db.set_trace_callback(lambda sql: enqueuing.set() if sql == "BEGIN IMMEDIATE" else None)
            yield db

    monkeypatch.setattr(store, "connection", observe_enqueue)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with connection() as db:
            db.execute("UPDATE sessions SET media_config=? WHERE id=?", (config.model_dump_json(), sid))
            retry = pool.submit(storage.retry, store, sid, jid, operation_id="disabled-retry")
            assert enqueuing.wait(timeout=1)
        with pytest.raises(ValueError, match="请先开启"):
            retry.result(timeout=1)
    assert storage.job(store, sid, jid)["status"] == "failed"


async def test_media_card_reference_sends_only_the_image_without_logging_its_bytes(story):
    import base64
    import hashlib

    import httpx
    from fastapi import FastAPI
    from PIL import Image

    from tavern.character.cards import export_card
    from tavern.core.message_adapter import provider_messages
    from tavern.media.api import register_routes
    from tavern.media.providers import reference_bytes
    from tavern.media.service import MediaService
    from tavern.media.storage import assets
    from tavern.scripting.model_transport import ModelTransport
    from tests.backend.test_image_protocols import response

    store, _ = story
    image = io.BytesIO()
    Image.new("RGBA", (2, 2), (20, 40, 60, 120)).save(image, format="PNG")
    cover = image.getvalue()
    raw, _, _ = export_card({"name": "Card", "description": "Private card text"}, "png", cover, {})
    card = store.import_card(raw)
    session = store.new_session(SessionCreate(card_id=card["id"]))
    sid = session["id"]

    app = FastAPI()
    register_routes(app, MediaService(store))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        catalog = (await client.get(f"/api/sessions/{sid}/media")).json()
        chosen = await client.post(
            f"/api/sessions/{sid}/media/card-reference",
            json={
                "expected_revision": session["revision"],
                "key": catalog["card_images"][0],
                "name": "Cover",
            },
        )
        chosen.raise_for_status()
    (asset,) = assets(store, sid)
    assert asset["blob"] == hashlib.sha256(cover).hexdigest()
    # An existing asset or imported package can still point to the card blob.
    with store.connection() as db:
        db.execute(
            "UPDATE media_assets SET blob=?,size=? WHERE id=?", (card["original"], len(raw), asset["id"])
        )
    assert reference_bytes(store, sid, {"references": [asset["id"]]}) == [(cover, "image/png")]

    wire, records = [], []
    cfg = store.session_settings(session).model_copy(
        update={"model": "fixture", "api_mode": "chat_completions", "model_request_mode": "non_stream"}
    )

    def send(request):
        wire.append(json.loads(request.content))
        return response("chat_completions", False)

    http = AsyncClient(transport=httpx.MockTransport(send), trust_env=False)
    provider = ModelTransport(cfg, "fixture-key", records.append, http_client=http)
    try:
        history = [{"role": "user", "content": "Reference cover", "_media_image_refs": [f"I{asset['ref']}"]}]
        await provider.chat_complete(provider_messages(provider, history, cfg))
    finally:
        await provider.close()
    url = wire[0]["messages"][0]["content"][1]["image_url"]["url"]
    assert base64.b64decode(url.split(",", 1)[1]) == cover
    logged = json.dumps(records[0], ensure_ascii=False)
    assert url not in logged and "图片二进制已省略" in logged and "Reference cover" in logged


async def test_story_image_views_survive_resume_fork_transfer_and_owner_deletion(story):
    """A viewed image must keep its bytes and ownership through replay and copying."""
    import base64

    from PIL import Image

    from tavern.conversation.compaction import canonical
    from tavern.core.tokens import request_tokens
    from tavern.media.images import materialize
    from tavern.media.providers import MediaResult
    from tavern.media.storage import add_asset, assets, config, connection_snapshot, create_job
    from tavern.storage.transfer import ExportSelection, export_package, import_package, read_package
    from tavern.story.context import load_trace

    store, session = story
    session = media_story(store, session)
    image = io.BytesIO()
    Image.new("RGB", (2, 2), "blue").save(image, format="PNG")
    with store.connection() as db:
        asset_id = add_asset(
            store,
            db,
            session["id"],
            data=image.getvalue(),
            kind="image",
            mime="image/png",
            name="人物参考",
            selected=True,
        )
    asset = assets(store, session["id"])[0]
    service = runtime(store)
    try:
        failed_model = Model(("view_image", {"ref": "I1"}), ValueError("模型连接中断"))
        failed, current = await turn(service, session, failed_model, operation_id="image-read")
        assert failed["status"] == "error"
        images = [
            part
            for message in failed_model.requests[-1]["messages"]
            if isinstance(message.get("content"), list)
            for part in message["content"]
            if part.get("type") == "image_url"
        ]
        assert len(images) == 1
        assert base64.b64decode(images[0]["image_url"]["url"].split(",", 1)[1]) == image.getvalue()
        resumed_model = Model("看过参考图后的正文。")
        result, current = await turn(
            service, current, resumed_model, resume_turn_id=failed["id"], operation_id="image-resume"
        )
        assert result["status"] == "committed", result["error"]
        sent = json.dumps(resumed_model.requests, ensure_ascii=False)
        assert asset_id not in sent and asset["blob"] not in sent
        with store.connection() as db:
            generated = create_job(
                db,
                current["id"],
                current["head"],
                "image",
                {
                    "connection": connection_snapshot(store.settings(), config(store, current["id"]).image),
                    "prompt": "人物在灯塔前",
                    "size": "1024x1024",
                    "references": [asset_id],
                    "reference_names": [{"ref": "I1", "name": "人物参考"}],
                },
            )
            db.execute("UPDATE media_jobs SET status='running' WHERE id=?", (generated,))
            generated_row = dict(db.execute("SELECT * FROM media_jobs WHERE id=?", (generated,)).fetchone())
        service.media.finish(generated_row, MediaResult(image.getvalue(), "image/png"))
        with store.connection() as db:
            former = add_asset(
                store,
                db,
                current["id"],
                data=image.getvalue(),
                kind="image",
                mime="image/png",
                name="曾选入目录的参考图",
                selected=True,
            )
        result, current = await turn(service, current, Model("继续这段故事。"), operation_id="image-catalog")
        assert result["status"] == "committed", result["error"]
        with store.connection() as db:
            db.execute("UPDATE media_assets SET selected=0 WHERE id=?", (former,))
        fork = store.fork_session(current["id"], current["head"], current["revision"])
        assert {a["ref"] for a in assets(store, fork["id"])} == {1, 2, 3}
        package, blobs = read_package(
            export_package(store, ExportSelection(settings=False, sessions=[fork["id"]])), store
        )
        imported_id = import_package(store, package, blobs)["sessions"][0]
        imported = store.session(imported_id)
        frames = [
            canonical(item["message"])
            for item in load_trace(store, imported_id, imported["messages"])
            if item["message"].get("_media_image_refs")
        ]
        assert len(frames) == 1 and frames[0]["_media_image_refs"] == ["I1"]
        hydrated = materialize(frames, store, imported_id)
        assert request_tokens(frames) == request_tokens(hydrated)
        assert hydrated[0]["content"][1]["image_url"]["url"] == images[0]["image_url"]["url"]
        path = store.root / "blobs" / asset["blob"]
        store.delete_session(current["id"])
        store.delete_session(fork["id"])
        assert path.is_file()
        store.delete_session(imported_id)
        assert not path.exists()
    finally:
        await service.close()


async def test_novel_research_isolates_reads_and_delivers_cross_turn_once(story):
    """A late read-only report must not leak its reading context or rewrite a committed turn."""
    from tavern.novel.source import import_file
    from tavern.storage.store import uid

    store, session = story
    service = runtime(store)
    research = service.story.novel
    source_id = uid()
    source_marker = "3d83956c0ac84bc4976828fbfe6e8362"
    original = "原著独有证据尾 " + source_marker + " " + "无关原文" * 20000
    metadata = import_file(store.root, source_id, io.BytesIO(original.encode()), "原著.txt")
    research.data.bind(session["id"], 0, source_id, metadata)
    research.data.configure(session["id"], 1, enabled=True)
    entered, release = asyncio.Event(), asyncio.Event()

    class Investigator(Model):
        async def chat_complete(self, messages, **kwargs):
            if not self.requests:
                entered.set()
                await release.wait()
            return await super().chat_complete(messages, **kwargs)

    investigator = Investigator(
        ("read_novel", {"start": 0, "length": 50}),
        ("search_novel", {"query": "@D1", "limit": 1}),
        ("read_novel", {"start": 0, "length": 7}),
        ValueError("maximum context length exceeded"),
        "已核实 N1 的 0–7 字符；后续正文尚未通读。",
        "调查报告：证据位于 N1 的 0–7 字符，不能推断全书。",
    )
    research.provider_factory = lambda cfg: investigator
    first = Model(
        ("start_novel_research", {"question": "查明该处证据", "background": "本局尚未决定去向"}),
        "先独立完成本轮正文。",
    )
    try:
        result, current = await turn(service, store.session(session["id"]), first)
        assert result["status"] == "committed", result["error"]
        await asyncio.wait_for(entered.wait(), 1)
        job = research.data.resolve(session["id"], "R1")
        assert job["status"] == "running"
        assert "原著独有证据尾" not in json.dumps(first.requests, ensure_ascii=False)
        status = next(
            message
            for message in first.requests[0]["messages"]
            if message.get("content", "").startswith("资料调查运行状态：")
        )
        assert status["role"] == "system"
        assert current["messages"][-1]["raw"] == "先独立完成本轮正文。"
        release.set()
        await asyncio.gather(*list(research.tasks.values()))
        job = research.data.job(job["id"])
        assert job["status"] == "completed", job["error"]
        assert job["requests"] == 6  # A rejected request and the explicit compaction both consume quota.
        assert investigator.closed
        assert len(research.data.inbox(session["id"])) == 1
        assert job["progress"]["context"]["summary"].startswith("已核实")
        trace = research.data.trace(job["id"])
        assert any("原著独有证据尾" in row["message"]["content"] for row in trace)
        assert source_marker not in json.dumps(investigator.requests, ensure_ascii=False)
        matches = [json.loads(row["message"]["content"]) for row in trace if row["message"]["role"] == "tool"]
        assert matches[1]["matches"][0]["start"] == 8
        assert job["progress"]["model_references"]["@D1"] == source_marker
        assert session["messages"][0]["raw"] not in json.dumps(investigator.requests, ensure_ascii=False)

        investigator.responses.append("追问报告：同一出处仍不足以推断全书。")
        adapter = store.message_adapter_settings("story", session["id"])
        adapter["config"]["story"]["mappings"] = {
            "chat_completions": {"novel_research": {"role": "user", "wrap": False}}
        }
        store.save_message_adapter("story", session["id"], adapter["revision"], adapter["config"])
        second = Model(
            ("list_novel_research", {}),
            ("message_novel_research", {"ref": "R1", "message": "复核是否能据此推断全书"}),
            ("wait_novel_research", {"ref": "R1", "timeout_seconds": 1}),
            ("destroy_novel_research", {"ref": "R1"}),
            "根据摘要继续正文。",
        )
        result, latest = await turn(service, current, second)
        assert result["status"] == "committed", result["error"]
        sent = json.dumps(second.requests, ensure_ascii=False)
        assert "调查报告：证据位于" in sent
        assert "追问报告：同一出处" in sent
        reports = [
            message
            for request in second.requests
            for message in request["messages"]
            if message.get("content", "").startswith("资料调查消息：")
        ]
        assert reports and all(message["role"] == "user" for message in reports)
        assert "原著独有证据尾" not in sent and original not in sent
        assert source_id not in sent and job["id"] not in sent
        assert research.data.inbox(session["id"]) == []
        assert research.data.job(job["id"])["status"] == "destroyed"
        assert research.data.job(job["id"])["requests"] == 7
        assert latest["messages"][2]["raw"] == "先独立完成本轮正文。"
        stored = store.story_progress(result["id"])
        with store.connection() as db:
            deliveries = db.execute(
                "SELECT count(*) FROM story_trace WHERE session=? AND json_extract(payload,'$._novel_mail') IS NOT NULL",
                (session["id"],),
            ).fetchone()[0]
        assert stored is None and deliveries == 2  # Each distinct report is delivered exactly once.
    finally:
        release.set()
        await service.close()


async def test_novel_limits_fork_authorization_and_portable_paused_progress(story):
    """Slots, retry-start counts and consumed quota are durable; a fork never shares a worker."""
    from fastapi import FastAPI

    from tavern.novel.api import register_routes
    from tavern.novel.service import NovelService
    from tavern.novel.source import NovelText
    from tavern.storage.store import Conflict
    from tavern.storage.transfer import ExportSelection, export_package, import_package, read_package

    store, session = story
    research = NovelService(store)
    app = FastAPI()
    register_routes(app, research, None)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.post(
            f"/api/sessions/{session['id']}/novel/source",
            params={"filename": "原著.md", "expected_revision": 0},
            headers={"Content-Type": "application/octet-stream"},
            content="第一章\n证据完整。".encode(),
        )
    assert response.status_code == 200, response.text
    source_id = response.json()["source"]["id"]
    assert not response.json()["enabled"]  # Imports supply a source for the master switch.
    config = research.snapshot_config(session["id"])
    with pytest.raises(ValueError, match="尚未授权"):
        research.data.create(session["id"], "off", 2, "", "问题", "", config)
    research.data.configure(session["id"], 1, enabled=True)
    jobs = [
        research.data.create(session["id"], f"call-{i}", 2, "", "问题", "背景", config)[0] for i in range(3)
    ]
    with pytest.raises(ValueError, match="同时最多三个"):
        research.data.create(session["id"], "fourth", 2, "", "问题", "", config)
    with pytest.raises(Conflict, match="尚未销毁"):
        research.data.configure(session["id"], 2, unbind=True)
    research.data.configure_advisor(session["id"], 2, True)
    with pytest.raises(Conflict, match="确认后"):
        store.fork_session(session["id"], session["head"], session["revision"])
    fork = store.fork_session(session["id"], session["head"], session["revision"], confirm_novel=True)
    assert fork["novel_enabled"] and not fork["novel_active"]
    assert research.data.binding(fork["id"])["source"] == source_id
    assert research.data.binding(fork["id"])["advisor_enabled"]
    assert research.data.job(jobs[0])["status"] == "running"
    for i in range(2):
        research.data.destroy(jobs[i])
        jobs.append(research.data.create(session["id"], f"replacement-{i}", 2, "", "问题", "", config)[0])
    research.data.destroy(jobs[2])
    with pytest.raises(ValueError, match="本轮已启动五次"):
        research.data.create(session["id"], "sixth", 2, "", "问题", "", config)
    saved = research.data.job(jobs[3])
    saved["requests"], saved["elapsed"] = 7, 13.5
    research.data.save(saved)
    # An older database has no per-investigator destruction policy. Opening it
    # must back up first and preserve the existing manual lifetime.
    with store.connection() as db:
        db.execute(f"ALTER TABLE sessions ADD COLUMN agent_mode TEXT NOT NULL DEFAULT '{OLD_STORY_KEY}'")
        db.execute("UPDATE meta SET value='17' WHERE key='schema_version'")
    store = Store(store.root)
    research.store = research.data.store = store
    assert research.data.job(jobs[3])["config"]["auto_destroy"] is False
    assert list(store.root.glob("before-upgrade-schema17-*.sqlite3"))
    store.recover()
    restored = research.data.job(saved["id"])
    assert (restored["status"], restored["requests"], restored["elapsed"]) == ("paused", 7, 13.5)
    with store.connection() as db:
        db.execute(
            "UPDATE novel_jobs SET config=json_set(config,'$.auto_destroy',json('true')) WHERE id=?",
            (saved["id"],),
        )
    archive = export_package(store, ExportSelection(settings=False, sessions=[session["id"]]))
    package, blobs = read_package(archive, store)
    imported = import_package(store, package, blobs)["sessions"][0]
    imported_job = research.data.resolve(imported, "R4")
    assert (imported_job["status"], imported_job["requests"], imported_job["elapsed"]) == ("paused", 7, 13.5)
    assert imported_job["config"]["auto_destroy"] is True
    imported_source = research.data.source(research.data.binding(imported)["source"])
    assert not research.data.binding(imported)["advisor_enabled"]
    assert imported_source["id"] != source_id
    assert NovelText(store.root, imported_source).read(0, 30)["text"] == "第一章\n证据完整。"
    assert research.data.create(fork["id"], "own", 2, "", "独立问题", "", config)[1]
    changed = store.update_session(session["id"], session["revision"], head=session["head"])
    assert not changed["novel_active"]
    assert research.data.resolve(fork["id"], "R4")["status"] == "running"
    # A stale background response cannot resurrect a revoked investigator or send mail.
    saved["status"] = "completed"
    assert not research.data.save(saved, mail={"kind": "report", "content": "迟到", "operation": "late"})


def test_novel_file_loss_preserves_revocable_authorization(story):
    from tavern.novel.service import NovelService
    from tavern.novel.source import directory, import_file
    from tavern.storage.store import uid

    store, session = story
    sid, source = session["id"], uid()
    research = NovelService(store)
    metadata = import_file(store.root, source, io.BytesIO(b"Reference text."), "reference.txt")
    research.data.bind(sid, 0, source, metadata)
    research.data.configure(sid, 1, enabled=True)

    folder = directory(store.root, source)
    missing = folder.rename(folder.with_name("offline-copy"))
    try:
        state = research.public(sid)
        assert state["authorized"] and not state["enabled"] and not state["can_enable"]
        research.data.configure(sid, state["revision"], enabled=False)
    finally:
        missing.rename(folder)
    state = research.public(sid)
    assert state["can_enable"] and not state["authorized"] and not state["enabled"]


async def test_novel_web_only_uses_shared_tools_and_holds_report_while_disabled(story, monkeypatch):
    """A fileless job can verify a public source without handing the web service its background."""
    from httpx import MockTransport, Response

    from tavern.storage.transfer import ExportSelection, export_package, import_package, read_package

    store, session = story
    sid = session["id"]
    settings = store.settings_document()
    settings.shared.web.exa.enabled = False
    store.save_settings(settings)
    service = runtime(store)
    research = service.story.novel
    extract = asyncio.Event()
    url = "https://work.example/chapters/794aab4ce5774fce83edd69e5406fd65"
    private_ref = "471655ab2f354253a3f772b0f9fdb054"
    unauthorized_url = "https://work.example/?ref="
    guidance = "《旧港纪事》\n优先核对作者网站。"
    external = []

    async def public_url(candidate):
        assert candidate in {
            url,
            unauthorized_url + private_ref,
            "https://api.tavily.com/search",
            "https://api.tavily.com/extract",
        }
        return candidate

    async def web(request):
        external.append((request.url.path, json.loads(request.content)))
        if request.url.path == "/search":
            return Response(200, json={"results": [{"url": url, "title": "旧港纪事", "content": "摘要"}]})
        await extract.wait()  # Keep the report out of the already-running first story turn.
        return Response(200, json={"results": [{"url": url, "raw_content": "完整的网页片段尾部。"}]})

    monkeypatch.setattr("tavern.web.client.public_url", public_url)
    research.web_transport = MockTransport(web)
    investigator = Model(
        ("read_webpage", {"url": unauthorized_url + "@D1"}),
        ("web_search", {"query": "旧港纪事 作者"}),
        ("read_webpage", {"url": "https://work.example/chapters/@D2"}),
        "据作者网页 https://work.example/chapters/@D2 核对了钟声的来源。",
    )
    research.provider_factory = lambda cfg: investigator
    first = Model(
        ("start_novel_research", {"question": "查钟声来源", "background": "本局原创去向 " + private_ref}),
        "继续剧情。",
    )
    try:
        research.data.configure(sid, 0, web_enabled=True)
        with pytest.raises(ValueError, match="调查指引"):
            research.data.configure(sid, 1, enabled=True)
        research.data.configure(sid, 1, enabled=True, web_guidance=guidance)
        result, current = await turn(service, store.session(sid), first)
        assert result["status"] == "committed"
        extract.set()
        await asyncio.gather(*research.tasks.values())
        job = research.data.resolve(sid, "R1")
        assert job["source"] is None and job["web_enabled"] and job["status"] == "completed", job["error"]
        assert {tool["function"]["name"] for tool in investigator.requests[0]["extra_body"]["tools"]} == {
            "web_search",
            "read_webpage",
            "ask_delegator",
        }
        assert json.loads(investigator.requests[0]["messages"][1]["content"])["web_guidance"] == guidance
        assert "794aab4ce5774fce83edd69e5406fd65" not in json.dumps(investigator.requests, ensure_ascii=False)
        assert [path for path, _ in external] == ["/search", "/extract"]
        assert external[0][1]["query"] == "旧港纪事 作者" and external[1][1]["urls"] == [url]
        assert "本局原创去向" not in json.dumps(external, ensure_ascii=False)
        records = research.data.trace(job["id"])
        assert "须完整引用" in records[1]["message"]["content"]
        assert records[-2]["message"]["web_sources"][0]["url"] == url
        assert "完整的网页片段尾部。" in records[-2]["message"]["content"]
        assert url in research.data.inbox(sid)[0]["content"]

        fork = store.fork_session(sid, current["head"], current["revision"], confirm_novel=True)
        assert research.data.binding(fork["id"])["web_guidance"] == guidance
        assert fork["novel_enabled"] and not fork["novel_active"]
        archive = export_package(store, ExportSelection(settings=False, sessions=[sid]))
        package, blobs = read_package(archive, store)
        imported = import_package(store, package, blobs)["sessions"][0]
        assert research.data.binding(imported)["web_guidance"] == guidance
        assert research.data.resolve(imported, "R1")["status"] == "paused"

        await research.cancel_paused(research.data.configure(sid, 2, enabled=False))
        off = Model("调查者关闭时继续剧情。")
        await turn(service, store.session(sid), off)
        assert all(
            tool["function"]["name"] not in MAIN_SPECS
            for request in off.requests
            for tool in request["extra_body"]["tools"]
        )
        assert len(research.data.inbox(sid)) == 1
        research.data.configure(sid, 3, enabled=True)
        on = Model("根据报告继续剧情。")
        await turn(service, store.session(sid), on)
        sent = json.dumps(on.requests, ensure_ascii=False)
        assert "核对了钟声的来源" in sent and "完整的网页片段尾部" not in sent
        assert research.data.inbox(sid) == []
    finally:
        extract.set()
        await service.close()


async def test_novel_web_revocation_cancels_inflight_tool_and_keeps_frozen_sources(story, monkeypatch):
    """Revoking the web permission must stop in-flight usage and fence off stale saves."""
    from tavern.novel.source import import_file
    from tavern.storage.store import uid

    store, session = story
    sid = session["id"]
    service = runtime(store)
    research, entered, cancelled = service.story.novel, asyncio.Event(), asyncio.Event()
    guidance = "按旧版本查钟声"

    async def search(_client, _query, _limit, _objective):
        if entered.is_set():
            return {"results": [], "service": "tavily"}
        entered.set()
        try:
            await asyncio.Future()
        finally:
            cancelled.set()

    monkeypatch.setattr("tavern.novel.worker.WebTools.search", search)
    investigator = Model(("web_search", {"query": "旧港纪事"}), "结论：资料不足。")
    research.provider_factory = lambda cfg: investigator
    research.data.configure(sid, 0, enabled=True, web_enabled=True, web_guidance=guidance)
    config = research.snapshot_config(sid)
    old, _ = research.data.create(sid, "web", 1, "", "问钟声", "", config)
    try:
        research.launch(old)
        await asyncio.wait_for(entered.wait(), 1)
        source = uid()
        metadata = import_file(store.root, source, io.BytesIO("钟声来自旧港。".encode()), "原著.txt")
        research.data.bind(sid, 1, source, metadata)
        research.data.configure(sid, 2, web_guidance="优先作者网站")
        mixed, _ = research.data.create(sid, "mixed", 1, "", "问原著", "", config)
        stale = research.data.job(old)
        assert stale["source"] is None and stale["web_guidance"] == guidance
        assert research.data.job(mixed)["source"] == source
        assert research.data.job(mixed)["web_guidance"] == "优先作者网站"

        await research.cancel_paused(research.data.configure(sid, 3, web_enabled=False))
        paused = research.data.job(old)
        assert cancelled.is_set() and paused["status"] == "paused" and paused["requests"] == 1
        assert paused["progress"]["pending"]
        assert research.public(sid)["enabled"]  # The local file still supplies a source.
        stale["status"] = "completed"
        assert not research.data.save(stale, mail={"kind": "report", "content": "迟到", "operation": "late"})
        with pytest.raises(ValueError, match="重新开启联网"):
            research.launch(old)
        research.data.configure(sid, 4, web_enabled=True)
        assert research.data.job(old)["status"] == "paused"
        research.launch(old)
        await research.tasks[old]
        assert research.data.job(old)["status"] == "completed"
        assert json.loads(investigator.requests[-1]["messages"][1]["content"])["web_guidance"] == guidance

        await research.destroy(mixed)
        research.data.configure(sid, 5, web_enabled=False)
        research.data.configure(sid, 6, unbind=True)
        assert not research.data.binding(sid)["enabled"]
        assert research.data.job(old)["status"] == "completed"
    finally:
        await service.close()


def test_novel_schema_29_migration_preserves_job_references(story):
    """Making the source nullable must preserve children and old file authorization."""
    from tavern.novel.service import NovelService
    from tavern.novel.source import import_file
    from tavern.storage.store import uid

    store, session = story
    sid, source = session["id"], uid()
    research = NovelService(store)
    metadata = import_file(store.root, source, io.BytesIO("保留这份原著".encode()), "原著.txt")
    research.data.bind(sid, 0, source, metadata)
    research.data.configure(sid, 1, enabled=True)
    identity, _ = research.data.create(sid, "old", 1, "", "查旧来源", "", research.snapshot_config(sid))
    job = research.data.job(identity)
    research.data.save(
        job,
        message={"role": "assistant", "content": "旧记录"},
        mail={
            "kind": "report",
            "content": "旧报告",
            "operation": "old-report",
        },
    )
    with store.connection() as db:
        db.executescript("""
            PRAGMA foreign_keys=OFF;
            BEGIN IMMEDIATE;
            ALTER TABLE novel_bindings DROP COLUMN web_enabled;
            ALTER TABLE novel_bindings DROP COLUMN web_guidance;
            ALTER TABLE novel_jobs DROP COLUMN web_enabled;
            ALTER TABLE novel_jobs DROP COLUMN web_guidance;
            PRAGMA writable_schema=ON;
            UPDATE sqlite_schema SET sql=replace(sql,'source TEXT REFERENCES','source TEXT NOT NULL REFERENCES')
            WHERE name='novel_jobs';
            PRAGMA writable_schema=OFF;
            UPDATE meta SET value='29' WHERE key='schema_version';
        """)
    with store.connection() as db:
        assert next(
            row["notnull"] for row in db.execute("PRAGMA table_info(novel_jobs)") if row["name"] == "source"
        )
    migrated = Store(store.root)
    research = NovelService(migrated)
    assert list(store.root.glob("before-upgrade-schema29-*.sqlite3"))
    assert research.public(sid)["enabled"] and not research.public(sid)["web_enabled"]
    assert research.data.job(identity)["source"] == source and not research.data.job(identity)["web_enabled"]
    assert research.data.trace(identity)[0]["message"]["content"] == "旧记录"
    assert research.data.inbox(sid)[0]["content"] == "旧报告"
    with migrated.connection() as db:
        assert db.execute("PRAGMA foreign_key_check").fetchone() is None


async def test_novel_auto_destroy_delivers_report_and_allows_clarification(story):
    """One-shot research releases its slot only after its answer reaches the main context."""
    from tavern.novel.source import import_file
    from tavern.storage.store import uid

    store, session = story
    service = runtime(store)
    research = service.story.novel
    source = uid()
    metadata = import_file(store.root, source, io.BytesIO("原著证据".encode()), "原著.txt")
    research.data.bind(session["id"], 0, source, metadata)
    research.data.configure(session["id"], 1, enabled=True)
    investigator = Model(("ask_delegator", {"question": "需要查哪个阶段？"}), "最终报告：早年阶段的证据。")
    research.provider_factory = lambda cfg: investigator
    main = Model(
        ("start_novel_research", {"question": "查人物经历", "auto_destroy": True}),
        ("wait_novel_research", {"ref": "R1", "timeout_seconds": 1}),
        ("message_novel_research", {"ref": "R1", "message": "只查早年阶段"}),
        ("wait_novel_research", {"ref": "R1", "timeout_seconds": 1}),
        ("list_novel_research", {}),
        "据调查结论继续正文。",
    )
    try:
        result, _ = await turn(service, store.session(session["id"]), main)
        assert result["status"] == "committed", result["error"]
        job = research.data.resolve(session["id"], "R1")
        assert job["config"]["auto_destroy"] is True
        assert job["status"] == "destroyed" and investigator.closed
        assert research.data.inbox(session["id"]) == []
        report_request = next(
            request for request in main.requests if "最终报告：早年阶段的证据。" in str(request["messages"])
        )
        assert "本报告送达后调查者自动销毁" in str(report_request["messages"])
        results = [
            json.loads(message["content"])
            for message in main.requests[-1]["messages"]
            if message["role"] == "tool"
        ]
        listing = next(item["data"] for item in results if "slots_remaining" in item.get("data", {}))
        assert listing["investigators"] == [] and listing["slots_remaining"] == 3
        assert research.data.starts(session["id"], job["round_position"]) == 1
        with store.connection() as db:
            assert (
                db.execute(
                    "SELECT count(*) FROM novel_mail WHERE job=? AND delivered IS NOT NULL AND revoked=0",
                    (job["id"],),
                ).fetchone()[0]
                == 3
            )  # Clarification, answer and final report all survive.
    finally:
        await service.close()


def test_novel_auto_destroy_receipt_is_atomic_and_not_replayed(story):
    """A failed destruction must not consume the report; saving old receipts must not trigger it again."""
    from tavern.novel.service import NovelService
    from tavern.novel.source import import_file
    from tavern.storage.store import uid

    store, session = story
    research = NovelService(store)
    source = uid()
    metadata = import_file(store.root, source, io.BytesIO("证据".encode()), "原著.txt")
    research.data.bind(session["id"], 0, source, metadata)
    research.data.configure(session["id"], 1, enabled=True)
    config = {**research.snapshot_config(session["id"]), "auto_destroy": True}
    job_id, _ = research.data.create(session["id"], "auto", 2, "", "问题", "", config)
    tid, _ = store.create_turn(
        session["id"], TurnRequest(text="继续", operation_id="receipt", expected_revision=0)
    )
    job = research.data.job(job_id)
    progress = {"new_trace": []}
    for kind, status in (("question", "waiting"), ("error", "failed"), ("report", "completed")):
        job["status"] = status
        research.data.save(job, mail={"kind": kind, "content": kind, "operation": kind})
        packet = research.data.inbox(session["id"])[0]
        progress["new_trace"].append({"message": {"_novel_mail": packet["seq"], "content": kind}})
        if kind != "report":
            assert store.save_story_progress(tid, progress) == set()
            assert research.data.job(job_id)["status"] == status
            continue
        with store.connection() as db:
            before = db.execute("SELECT payload FROM story_progress WHERE turn=?", (tid,)).fetchone()[0]
            db.execute(
                "CREATE TRIGGER reject_auto_destroy BEFORE UPDATE OF status ON novel_jobs "
                "WHEN NEW.status='destroyed' BEGIN SELECT RAISE(ABORT,'injected failure'); END"
            )
        with pytest.raises(sqlite3.IntegrityError, match="injected failure"):
            store.save_story_progress(tid, progress)
        assert research.data.job(job_id)["status"] == "completed"
        assert research.data.inbox(session["id"])[0]["kind"] == "report"
        with store.connection() as db:
            assert (
                db.execute("SELECT payload FROM story_progress WHERE turn=?", (tid,)).fetchone()[0] == before
            )
            db.execute("DROP TRIGGER reject_auto_destroy")
    assert store.save_story_progress(tid, progress) == {job_id}
    assert research.data.job(job_id)["status"] == "destroyed"
    assert research.data.inbox(session["id"]) == []
    assert store.save_story_progress(tid, progress) == set()
    with store.connection() as db:
        assert (
            db.execute(
                "SELECT count(*) FROM novel_events WHERE job=? AND kind='destroyed'", (job_id,)
            ).fetchone()[0]
            == 1
        )


def test_novel_character_ranges_preserve_long_lines_and_unicode(tmp_path):
    """Sparse offsets must survive long single lines, multibyte characters and block-spanning hits."""
    from tavern.novel.source import NovelText, import_file
    from tavern.storage.store import uid

    source_id = uid()
    text = "序章\r\n" + "甲" * 65527 + "跨界证据🦉" + "乙" * 9000 + "\n# 第二章\n尾部"
    source = {
        "id": source_id,
        **import_file(tmp_path, source_id, io.BytesIO(text.encode("utf-8-sig")), "小说.txt"),
    }
    book = NovelText(tmp_path, source)
    offset = text.index("跨界证据")
    assert book.read(offset - 3, 12)["text"] == text[offset - 3 : offset + 9]
    search = book.search("跨界证据🦉", 0, 1)
    assert search["matches"][0]["start"] == offset
    assert search["matches"][0]["end"] == offset + len("跨界证据🦉")
    assert search["has_more"] is False
    # Bounded counting must stop at end and still report every hit inside the range.
    bounded = book.search("乙", offset, 1, offset + 20)
    assert (bounded["total_matches"], bounded["has_more"], bounded["next_start"]) == (15, True, offset + 6)
    assert [h["title"] for h in book.outline(0, 50)["headings"]] == ["序章", "# 第二章"]
    assert book.read(len(text), 20)["text"] == ""
    with pytest.raises(ValueError, match="超出参考文件范围"):
        book.read(len(text) + 1, 20)
    with pytest.raises(ValueError, match="解码失败"):
        import_file(tmp_path, uid(), io.BytesIO(b"\xffinvalid"), "wrong.txt")


@pytest.mark.parametrize("name", ["patch_state", "revise_content"])
async def test_tool_comment_contract_repairs_old_arguments_and_preserves_activity(story, name):
    store, session = story
    service = runtime(store)
    operations = (
        []
        if name == "patch_state"
        else [{"op": "set_character", "field": "description", "value": "新的角色描述"}]
    )
    calls = [] if name == "patch_state" else [("read_character", {"ref": "description"})]
    comment = "本轮操作备注"
    model = Model(
        *calls,
        (name, {"operations": operations, "reason": "旧参数备注"}),
        (name, {"operations": operations, "comment": comment}),
        "继续正文",
    )
    try:
        result, current = await turn(
            service,
            session,
            model,
            update_variables=name == "patch_state",
            allow_content_edit=name == "revise_content",
        )
        assert result["status"] == "committed", result["error"]
        schema = next(
            tool["function"]["parameters"]
            for tool in model.requests[0]["extra_body"]["tools"]
            if tool["function"]["name"] == name
        )
        assert set(schema["properties"]) == {"operations", "comment"}
        assert "comment" in schema["required"]
        outputs = [
            json.loads(message["content"])
            for message in model.requests[-1]["messages"]
            if message["role"] == "tool"
        ]
        feedback, successful = outputs[-2:]
        assert feedback["error"]["code"] == "INVALID_ARGUMENTS"
        assert "缺少必填参数 comment" in feedback["error"]["message"]
        assert "不接受参数 reason" in feedback["error"]["message"]
        assert "expected_parameters" not in feedback
        assert successful["ok"] and successful["data"]["saved_to_draft"]
        if name == "revise_content":
            content = current["snapshot"]["content"]
            assert content["document"]["data"]["description"] == "新的角色描述"
            assert content["reason"] == comment
        else:
            assert successful["data"]["patch_state_completed"]
        items = [
            item
            for item in turn_items(store, session["id"], result["id"])
            if item["kind"] == "tool" and item["name"] == name
        ]
        assert [item["summary"] for item in items] == ["旧参数备注", comment]
        assert [item["status"] for item in items] == ["error", "returned"]
        assert json.loads(items[0]["details"]["arguments"])["reason"] == "旧参数备注"
        assert json.loads(items[1]["details"]["arguments"])["comment"] == comment
    finally:
        await service.close()


@pytest.mark.parametrize(
    "name,arguments,expected",
    [
        (
            "read_history",
            {"reason": "PRIVATE_INPUT_48c2515daf9742308a8b50777d39dd75"},
            ["缺少必填参数 ref", "不接受参数 reason"],
        ),
        (
            "read_history",
            {"ref": "H2", "48c2515daf9742308a8b50777d39dd75": "PRIVATE_INPUT"},
            ["不接受参数 @D", "请删除"],
        ),
        (
            "new_context",
            {"handoff": handoff("不得换窗"), "keep_recent": 99},
            ["参数 keep_recent 必须小于或等于 12"],
        ),
        (
            "write_note",
            {"key": "test", "content": "未接受", "kind": "wrong"},
            ["参数 kind 必须取允许值", "fact", "hypothesis", "plan"],
        ),
    ],
)
async def test_argument_errors_explain_repairs_without_executing_or_echoing_input(
    story, name, arguments, expected
):
    store, session = story
    service = runtime(store)
    model = Model((name, arguments), "继续正文")
    try:
        result, current = await turn(service, session, model)
        assert result["status"] == "committed", result["error"]
        feedback = json.loads(
            next(m["content"] for m in model.requests[1]["messages"] if m["role"] == "tool")
        )
        assert feedback["error"]["message"].startswith(f"{name} 参数错误，工具未执行：")
        assert all(text in feedback["error"]["message"] for text in expected)
        assert "expected_parameters" not in feedback
        encoded = json.dumps(feedback, ensure_ascii=False)
        assert all(
            text not in encoded
            for text in (
                "errors.pydantic.dev",
                "validation error",
                "input_value",
                "input_type",
                "PRIVATE_INPUT",
                "48c2515daf9742308a8b50777d39dd75",
                # Schema metadata titles; a chapter's own "title" property is a parameter.
                '"title": "',
            )
        )
        for key in ("handoff", "notes", "window"):
            assert current["snapshot"]["story"][key] == session["snapshot"]["story"][key]
        event = next(e for e in store.events(result["id"]) if e["kind"] == "story_tool")
        assert event["payload"]["result"] == feedback
    finally:
        await service.close()


async def test_read_history_can_correct_missing_ref_after_actionable_feedback(story):
    store, session = story
    service = runtime(store)
    model = Model(
        ("read_history", {"reason": "核对H2内容"}), ("read_history", {"ref": "H2"}), "核对后继续正文"
    )
    try:
        result, current = await turn(service, session, model)
        assert result["status"] == "committed", result["error"]
        returned = [json.loads(m["content"]) for m in model.requests[-1]["messages"] if m["role"] == "tool"]
        assert "缺少必填参数 ref" in returned[0]["error"]["message"]
        assert "expected_parameters" not in returned[0]
        assert "error" not in returned[1] and returned[1]["data"]["ref"] == "H2"
        assert returned[1]["data"]["text"] == "我答应明天在钟楼见面。"
        assert current["messages"][-1]["raw"] == "核对后继续正文"
    finally:
        await service.close()


def test_structural_argument_feedback_includes_only_actionable_schema_without_input():
    model = SPECS["revise_content"][0]
    private = "PRIVATE_INPUT_48c2515daf9742308a8b50777d39dd75"
    with pytest.raises(ValidationError) as invalid:
        model.model_validate({"comment": "新增条目", "operations": [{"op": "add_lore", "entry": private}]})
    feedback = ToolArgumentError("revise_content", model, invalid.value).result
    assert "operations[0].entry 必须是对象" in feedback["error"]["message"]
    assert set(feedback["expected_parameters"]) == {"operations[0].entry"}
    assert feedback["expected_parameters"]["operations[0].entry"]["required"] == ["title", "content"]
    encoded = json.dumps(feedback, ensure_ascii=False)
    assert private not in encoded and '"$ref"' not in encoded and '"$defs"' not in encoded
    assert '"comment"' not in encoded and '"ChapterMerge"' not in encoded

    model = SPECS["patch_state"][0]
    with pytest.raises(ValidationError) as invalid:
        model.model_validate({"comment": "修正", "operations": [{"op": private, "path": "/hp"}]})
    feedback = ToolArgumentError("patch_state", model, invalid.value).result
    assert "'op' 必须取允许值" in feedback["error"]["message"]
    assert set(feedback["expected_parameters"]) == {"operations[0]"}
    branches = feedback["expected_parameters"]["operations[0]"]["oneOf"]
    assert [branch["required"] for branch in branches] == [
        ["op", "path", "value"],
        ["op", "path", "from"],
        ["op", "path"],
    ]
    assert private not in json.dumps(feedback)


def test_business_argument_feedback_reports_rules_without_schema_or_input_echo():
    model = SPECS["revise_content"][0]
    with pytest.raises(ValidationError) as invalid:
        model.model_validate(
            {
                "comment": "新增",
                "operations": [{"op": "add_lore", "entry": {"title": " ", "content": "PRIVATE_INPUT"}}],
            }
        )
    feedback = ToolArgumentError("revise_content", model, invalid.value).result
    assert "operations[0].entry 条目标题和正文不能为空白" in feedback["error"]["message"]
    assert "expected_parameters" not in feedback and "PRIVATE_INPUT" not in json.dumps(feedback)
    with pytest.raises(ValidationError) as invalid:
        model.model_validate(
            {
                "comment": "修改",
                "operations": [{"op": "update_lore", "ref": "L1", "changes": {"title": None}}],
            }
        )
    feedback = ToolArgumentError("revise_content", model, invalid.value).result
    assert "必须提供至少一个非 null 的设定字段" in feedback["error"]["message"]
    assert "expected_parameters" not in feedback


@pytest.mark.parametrize("name", ["search_lore", "search_read_lore", "search_lore_entries", "unknown_tool"])
async def test_unknown_tool_errors_identify_missing_name_and_allow_retry(story, name):
    store, session = story
    service = runtime(store)
    model = Model((name, {"query": "灯塔"}), ("recall_lore", {"query": "灯塔"}), "继续。")
    try:
        result, current = await turn(service, session, model)
        assert result["status"] == "committed", result["error"]
        assert name not in {t["function"]["name"] for t in model.requests[0]["extra_body"]["tools"]}
        output = [json.loads(m["content"]) for m in model.requests[-1]["messages"] if m["role"] == "tool"]
        assert output[0]["ok"] is False
        assert output[0]["error"]["code"] == "UNKNOWN_TOOL"
        assert name in output[0]["error"]["message"]
        assert output[1]["data"]["matches"]
        event = next(e for e in store.events(result["id"]) if e["kind"] == "story_tool")
        assert event["payload"]["result"] == output[0]
        for key in ("handoff", "notes", "window"):
            assert current["snapshot"]["story"][key] == session["snapshot"]["story"][key]
    finally:
        await service.close()


@pytest.mark.parametrize("old_name", ["search_lore", "search_read_lore", "search_lore_entries", "read_lore"])
async def test_retired_tool_names_are_not_accepted_in_settings(story, old_name):
    store, session = story
    app = create_app(store.root)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
            response = await client.put(
                f"/api/sessions/{session['id']}/story/tools",
                json={"expected_revision": 0, "disabled_tools": [old_name]},
            )
            assert response.status_code == 400
            assert store.session(session["id"])["disabled_tools"] == session["disabled_tools"]
    finally:
        await app.state.service.close()


@pytest.mark.parametrize("tool", ["patch_state"])
async def test_required_mvu_update_rejects_disabled_dependency(story, tool):
    store, session = story
    with store.connection() as db:
        db.execute("UPDATE sessions SET disabled_tools=? WHERE id=?", (json.dumps([tool]), session["id"]))
    service = runtime(store)
    try:
        with pytest.raises(ValueError, match=f"启用 {tool}"):
            service.story.submit(
                session["id"],
                TurnRequest(
                    text="继续", operation_id="mvu-disabled", expected_revision=0, update_variables=True
                ),
            )
        assert store.session(session["id"])["turns"] == []
    finally:
        await service.close()


async def test_tool_catalog_switches_persist_and_enforce_permissions(story):
    store, session = story
    initially_disabled = session["disabled_tools"]
    feature_controlled = {"write_plot", *MAIN_SPECS, *MEDIA_SPECS}
    app = create_app(store.root)
    service = runtime(store)
    url = f"/api/sessions/{session['id']}/story/tools"
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
            catalog = (await client.get(url)).json()["tools"]
            delivery = next(t for t in catalog if t["name"] == "submit_story")
            assert (
                delivery["enabled"]
                and delivery["available"]
                and delivery["controlled_by"] == "story_protocol"
            )
            rejected = await client.put(
                url, json={"expected_revision": 0, "disabled_tools": ["submit_story"]}
            )
            assert rejected.status_code == 400
            assert store.session(session["id"])["disabled_tools"] == initially_disabled
            preview_url = f"/api/sessions/{session['id']}/preview"
            original_tools = (await client.post(preview_url, json={"text": "继续"})).json()["tools"]
            assert {t["name"] for t in catalog} == set(SPECS)
            assert "read_lore" not in {t["name"] for t in catalog}
            for tool in catalog:
                assert tool["enabled"] == (tool["name"] not in {*feature_controlled, *initially_disabled})
                if tool["name"] == "write_plot":
                    description = tool["description"].replace(" (锚点)", "").replace(" (冷酷度)", "")
                    tool = {**tool, "description": description}
                assert not re.search(r"[\u4e00-\u9fff]", json.dumps(tool, ensure_ascii=False))
                assert all(p.get("description") for p in tool["parameters"]["properties"].values())
            lore = next(t for t in catalog if t["name"] == "recall_lore")
            assert lore["result_options"] == {"include_ref": False, "include_title": True}
            assert set(lore["parameters"]["properties"]) == {"query"}
            patch = next(t for t in catalog if t["name"] == "patch_state")
            assert not patch["available"]
            edit = next(t for t in catalog if t["name"] == "revise_content")
            assert edit["enabled"] and not edit["available"]
            assert not any(edit["edit_capabilities"].values())
            authorized = (
                await client.get(url, params={"update_variables": "true", "allow_content_edit": "true"})
            ).json()["tools"]
            assert next(t for t in authorized if t["name"] == "patch_state")["available"]
            assert next(t for t in authorized if t["name"] == "revise_content")["available"]
            response = await client.put(
                url, json={"expected_revision": 0, "disabled_tools": ["write_handoff"]}
            )
            assert response.status_code == 200
            session = response.json()
            assert Store(store.root).session(session["id"])["disabled_tools"] == ["write_handoff"]
            assert (
                await client.put(url, json={"expected_revision": 0, "disabled_tools": []})
            ).status_code == 409
            assert (
                await client.put(url, json={"expected_revision": 1, "disabled_tools": ["unknown"]})
            ).status_code == 400
            preview = (
                await client.post(f"/api/sessions/{session['id']}/preview", json={"text": "继续"})
            ).json()
            assert "write_handoff" not in {t["function"]["name"] for t in preview["tools"]}
            model = Model(("write_handoff", handoff("禁止写入", through=None)), "正文")
            result, session = await turn(service, session, model)
            assert result["status"] == "committed", result["error"]
            assert not current_text(session["snapshot"]["story"])
            assert "write_handoff" not in {
                t["function"]["name"] for t in model.requests[0]["extra_body"]["tools"]
            }
            feedback = json.loads(
                next(m["content"] for m in model.requests[1]["messages"] if m["role"] == "tool")
            )
            assert feedback["ok"] is False
            assert feedback["error"]["code"] == "PERMISSION_DENIED"
            assert feedback["error"]["retryable"] is False
            fork = store.fork_session(session["id"], session["head"], session["revision"])
            assert fork["disabled_tools"] == ["write_handoff"]
            result, session = await turn(service, session, Model("重生成"), mode="regenerate")
            assert result["status"] == "committed"
            assert session["disabled_tools"] == ["write_handoff"]
            response = await client.put(
                url,
                json={
                    "expected_revision": session["revision"],
                    "disabled_tools": [
                        name for name in SPECS if name not in {"submit_story", *feature_controlled}
                    ],
                },
            )
            assert response.status_code == 200, response.text
            session = response.json()
            no_tools = Model("仅提交正文")
            result, session = await turn(service, session, no_tools)
            assert result["status"] == "committed", result["error"]
            assert [t["function"]["name"] for t in no_tools.requests[0]["extra_body"]["tools"]] == [
                "submit_story"
            ]
            response = await client.put(
                url, json={"expected_revision": session["revision"], "disabled_tools": initially_disabled}
            )
            session = response.json()
            assert session["disabled_tools"] == initially_disabled
            restored_tools = (await client.post(preview_url, json={"text": "继续"})).json()["tools"]
            assert json.dumps(restored_tools, ensure_ascii=False) == json.dumps(
                original_tools, ensure_ascii=False
            )
            tid, _ = store.create_turn(
                session["id"], TurnRequest(operation_id="pending", expected_revision=session["revision"])
            )
            assert (
                await client.put(url, json={"expected_revision": session["revision"], "disabled_tools": []})
            ).status_code == 409
            store.set_turn(tid, status="cancelled")
            # New stories select a channel on creation. Only legacy rows may still be unclassified.
            with store.connection() as db:
                db.execute("UPDATE sessions SET channel_origin=NULL WHERE id=?", (session["id"],))
            assert (await client.get(url)).status_code == 400
    finally:
        await service.close()
        await app.state.service.close()


async def test_user_result_options_save_and_control_tool_output(tmp_path):
    store = Store(tmp_path)
    card = store.import_card(
        json.dumps(
            {
                "name": "港口",
                "first_mes": "开场。",
                "character_book": {
                    "entries": [
                        {
                            "id": "port",
                            "keys": ["旧港"],
                            "comment": "旧港",
                            "content": "旧港有一艘船。",
                            "enabled": True,
                        }
                    ]
                },
            }
        ).encode(),
    )
    settings = store.settings()
    settings.retrieval.channels = ["keyword"]
    settings.story_config.auto_retrieval = False
    store.save_settings(settings)
    session = store.new_session(SessionCreate(card_id=card["id"]))
    app = create_app(store.root)
    service = runtime(store)
    url = f"/api/sessions/{session['id']}/story/tools"
    options = {
        "recall_lore": {"include_ref": True, "include_title": True},
        "search_history": {"include_preview": False},
        "read_history": {"include_metadata": False},
    }
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
            assert (
                await client.put(
                    url,
                    json={
                        "expected_revision": 0,
                        "disabled_tools": [],
                        "tool_result_options": {"recall_lore": {"unknown": True}},
                    },
                )
            ).status_code == 400
            response = await client.put(
                url, json={"expected_revision": 0, "disabled_tools": [], "tool_result_options": options}
            )
            assert response.status_code == 200
            session = response.json()
            assert session["tool_result_options"] == options
            catalog = (await client.get(url)).json()["tools"]
            assert (
                next(t for t in catalog if t["name"] == "recall_lore")["result_options"]
                == options["recall_lore"]
            )
            fork = store.fork_session(session["id"], session["head"], session["revision"])
            assert fork["tool_result_options"] == options
            model = Model(
                ("recall_lore", {"query": "旧港"}),
                ("search_history", {"query": "开场"}),
                ("read_history", {"ref": "H1"}),
                ("read_history", {"ref": "H1-H2"}),
                "继续。",
            )
            result, _ = await turn(service, session, model)
            assert result["status"] == "committed", result["error"]
            output = [json.loads(m["content"]) for m in model.requests[-1]["messages"] if m["role"] == "tool"]
            assert output[0]["data"]["matches"] == [
                {"ref": "L1", "title": "旧港", "text": "<lorebook>\n旧港有一艘船。\n</lorebook>"}
            ]
            assert "preview" not in output[1]["data"]["matches"][0]
            assert "offset" not in output[1]["data"]["matches"][0]
            assert set(output[2]["data"]) == {"text"}
            # The switch applies to every message of a span, including the current input.
            assert [set(m) for m in output[3]["data"]["messages"]] == [{"text"}, {"text"}]
    finally:
        await service.close()
        await app.state.service.close()


async def test_disabled_rollover_fails_before_requesting_unavailable_tool(story, monkeypatch):
    store, session = story
    with store.connection() as db:
        db.execute(
            "UPDATE sessions SET disabled_tools=? WHERE id=?", (json.dumps(["new_context"]), session["id"])
        )
    session = store.session(session["id"])
    original = StoryAgent.compile

    def full_window(self):
        result = original(self)
        self.must_rollover = True
        return result

    monkeypatch.setattr(StoryAgent, "compile", full_window)
    service = runtime(store)
    model = Model("应当不请求模型")
    try:
        result, current = await turn(service, session, model)
        assert result["status"] == "error"
        assert "new_context 已关闭" in result["error"]
        assert model.requests == []
        assert current["snapshot"] == session["snapshot"]
    finally:
        await service.close()


@pytest.mark.parametrize("wrapped", [False, True])
async def test_character_tool_has_same_semantic_contract_for_wrapped_and_legacy_cards(wrapped):
    from types import SimpleNamespace

    from tavern.core.model_view import ModelView
    from tavern.story.state import new_state
    from tavern.story.tools import AgentTools, tool_definitions

    data = {"name": "Guide", "description": "Before", "custom_field": "Custom"}
    document = {"spec": "chara_card_v2", "spec_version": "2.0", "data": data} if wrapped else data
    definitions = tool_definitions(
        {"policy_effective": "auto"},
    )
    catalog = {item["function"]["name"]: item["function"] for item in definitions}
    assert "custom_field" not in json.dumps(catalog)
    assert set(catalog["read_character"]["parameters"]["properties"]["ref"]["enum"]) == {
        "name",
        "description",
        "personality",
        "scenario",
        "mes_example",
        "system_prompt",
        "post_history_instructions",
    }
    owner = SimpleNamespace(
        document=document,
        definitions=definitions,
        request=TurnRequest(operation_id="guidance", expected_revision=0, allow_content_edit=True),
        state=new_state(),
        snapshot={},
        session={},
        model_view=ModelView(document),
        tid="guidance",
    )
    tools = AgentTools(owner)
    before = await tools.execute("read_character", json.dumps({"ref": "description"}))
    assert before["data"]["text"] == "Before"
    await tools.execute(
        "revise_content",
        json.dumps(
            {
                "operations": [{"op": "set_character", "field": "description", "value": "After"}],
                "comment": "Update setting",
            }
        ),
    )
    after = await tools.execute("read_character", json.dumps({"ref": "description"}))
    assert after["data"]["text"] == "After"


def test_handoff_chapters_cover_sources_and_reject_old_formats():
    from tavern.core.models import Settings
    from tavern.story.context import compile_window
    from tavern.story.state import new_state, story_state

    history = [{"id": f"m{i}", "role": ("assistant", "user")[i % 2], "raw": f"原文{i}"} for i in range(6)]
    empty = {"chapters": [], "current": [], "revision": 0}

    def write(handoff, chapters, current="仍在港口"):
        return apply_write(
            handoff,
            HandoffWrite.model_validate(
                {"chapters": chapters, "current": [{"text": current, "refs": ["H6"]}]}
            ),
            history,
        )

    def point(text, *refs):
        return {"text": text, "refs": list(refs)}

    first = write(
        empty, [{"op": "add", "through": "H3", "title": "初到", "points": [point("借出戒指", "H2")]}]
    )
    with pytest.raises(HandoffError, match="不在所属章节"):
        write(first, [{"op": "add", "through": "H6", "title": "夜航", "points": [point("上船", "H2")]}])
    with pytest.raises(HandoffError) as gap:
        require_coverage(first, history, 6)
    assert gap.value.code == "HANDOFF_COVERAGE" and "H4-H6" in str(gap.value)
    both = write(first, [{"op": "add", "through": "H6", "title": "夜航", "points": [point("上船", "H5-H6")]}])
    require_coverage(both, history, 6)
    # Without through, the Runtime endpoint may leave nothing new to archive.
    with pytest.raises(HandoffError, match="章节已覆盖到 H6.*删去这个 add"):
        write(both, [{"op": "add", "title": "无新内容", "points": [point("无")]}])
    # Merge is the only way old chapters shrink; spans stay addressable.
    merged = write(
        both,
        [
            {
                "op": "merge",
                "chapters": ["C1", "C2"],
                "title": "港口",
                "points": [point("借戒指后上船", "H2", "H5-H6")],
            }
        ],
    )
    text = render_text(merged, history)
    assert text == "## C1 港口 [H1-H6]\n- 借戒指后上船 [H2, H5-H6]\n\n## 当前交接\n- 仍在港口 [H6]"
    # Read-only rendering preserves the model's complete prose without an
    # intermediate editable grammar or converting literal text into references.
    unusual = apply_write(
        empty,
        HandoffWrite(
            chapters=[
                {
                    "op": "add",
                    "title": '两行\n标题 "港口"',
                    "points": [
                        {"text": "第一行\n第二行"},
                        {"text": "招呼 [Hello]"},
                        {"text": "字面量 [H2]"},
                        {"text": "（推断）这是引文", "kind": "inference", "refs": ["H2"]},
                    ],
                }
            ],
            current=[{"text": '"原话"\n仍保留', "refs": ["H3"]}, {"text": "（推断）只是文字"}],
        ),
        history,
    )
    rendered = render_text(unusual, history)
    assert '两行\n标题 "港口"' in rendered
    assert "第一行\n第二行" in rendered
    assert "招呼 [Hello]" in rendered and "字面量 [H2]" in rendered
    assert '"原话"\n仍保留' in rendered
    assert not unusual["chapters"][0]["points"][2]["refs"]
    assert render_written_current(
        [{"text": '"原话"\n仍保留', "refs": ["H3"]}, {"text": "（推断）只是文字"}]
    ) == render_text({"chapters": [], "current": unusual["current"]}, history)
    # Unreleased v2 development states are rejected like v1, not migrated.
    legacy = new_state()
    legacy["version"] = 2
    with pytest.raises(ValueError, match="格式版本 2 不受支持"):
        story_state({"story": legacy})
    revised = write(
        merged,
        [{"op": "revise", "chapter": "C1", "title": "港口", "points": [point("核对后的经过", "H2")]}],
    )
    assert revised["chapters"][0]["id"] == merged["chapters"][0]["id"]
    # Ordinary writes contain no mechanical H fields; Runtime supplies the exiting
    # range, while current can carry this turn's instructions without a fake source.
    plain = HandoffWrite.model_validate(
        {
            "chapters": [
                {"op": "add", "title": "港口", "points": [{"text": "已借出戒指"}, {"text": "船票仍未找到"}]}
            ],
            "current": [{"text": "按本轮用户要求保持第一人称"}],
        }
    )
    automatic = apply_write(empty, plain, history, archive_through=3)
    require_coverage(automatic, history, 3)
    assert automatic["chapters"][0]["end"] == history[2]["id"]
    assert (
        render_written_current(plain.model_dump()["current"]) == "## 当前交接\n- 按本轮用户要求保持第一人称"
    )
    plain_text = render_text(automatic, history)
    assert (
        plain_text
        == "## C1 港口 [H1-H3]\n- 已借出戒指\n- 船票仍未找到\n\n## 当前交接\n- 按本轮用户要求保持第一人称"
    )
    # Full-source replay must drop both handoff frames even when the baseline
    # still matches (local -> cloud -> local with no intervening story request).
    state = new_state()
    state["window"]["number"] = 2
    state["handoff"] = automatic
    session, settings = {"id": "story", "persona": "User", "messages": history}, Settings()
    snapshot = {"state": {}, "story": state}
    compiled = compile_window({}, session, history, snapshot, "", settings, [])
    trace = [
        {"message_id": history[-1]["id"], "window_id": state["window"]["id"], "message": item}
        for item in compiled["context_additions"]
    ]
    replay = compile_window({}, session, history, snapshot, "", settings, [], trace=trace, replay_all=True)
    assert replay["context_ledger"]["rebuild_reason"] is None
    assert not any(m.get("source") in ("handoff", "handoff_chapters") for m in replay["trace"])


async def test_new_context_reuses_handoff_and_checks_only_exiting_history():
    import copy
    from types import SimpleNamespace

    from tavern.core.model_view import ModelView
    from tavern.core.models import Settings
    from tavern.story.protocol import ToolError
    from tavern.story.state import new_state
    from tavern.story.tools import AgentTools, tool_definitions

    # H1 is the greeting; H2-H7 are three complete user/assistant exchanges.
    history = [{"id": f"m{i}", "role": ("assistant", "user")[i % 2], "raw": f"原文{i}"} for i in range(7)]
    owner = SimpleNamespace(
        state=new_state(),
        history=history,
        user_message={"id": "input", "role": "user", "raw": "继续"},
        settings=Settings(),
        definitions=tool_definitions({}),
        model_view=ModelView({}),
    )
    tools = AgentTools(owner)
    # Keeping all story messages needs no summary and must not invent one.
    empty = copy.deepcopy(owner.state["handoff"])
    await tools.execute("new_context", '{"keep_recent":7}')
    assert owner.state["window"]["start"] == 0
    assert owner.state["handoff"] == empty
    await tools.execute(
        "write_handoff",
        json.dumps(
            {
                "chapters": [
                    {"op": "add", "through": "H5", "title": "先前经历", "points": [{"text": "旧事已记录"}]}
                ],
                "current": [{"text": "继续最新交流"}],
            }
        ),
    )
    before = copy.deepcopy(owner.state["handoff"])
    # keep_recent=1 expands to H6-H7, so coverage through H5 is sufficient.
    await tools.execute("new_context", '{"keep_recent":1}')
    assert owner.state["window"]["start"] == 5
    assert owner.state["handoff"] == before
    # Retiring the previously retained exchange does require a chapter.
    unchanged = copy.deepcopy(owner.state)
    with pytest.raises(ToolError) as gap:
        await tools.execute("new_context", '{"keep_recent":0}')
    assert gap.value.result["error"]["code"] == "HANDOFF_COVERAGE"
    assert "H6-H7" in str(gap.value)
    assert owner.state == unchanged
