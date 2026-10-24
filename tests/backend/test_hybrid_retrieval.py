import copy
import json
import sqlite3
from contextlib import closing
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from tavern.application.api import create_app
from tavern.conversation.context import compile_context
from tavern.core.models import ContextRecipe, SessionCreate, Settings
from tavern.memories.memory_archive import ARCHIVE, archive_entry, retrieval_entries
from tavern.memories.memory_records import Record
from tavern.search.retrieval import RetrievalEngine, SparseIndex, fuse, tokenize
from tavern.search.retrieval_config import RetrievalSettings
from tavern.search.retrieval_text import RetrievalParser, query_text, split_retrieval


@pytest.mark.parametrize("width", [1, 2, 5, 17, 10000])
@pytest.mark.parametrize(
    "hidden", ["<tavern_retrieval>灯塔 身份</tavern_retrieval>", "<tavern_retrieval>未闭合"]
)
def test_streaming_protocol_preserves_mvu_without_exposing_clue(width, hidden):
    text = "正文\n" + hidden + "<UpdateVariable><JSONPatch>[]</JSONPatch></UpdateVariable>"
    parser = RetrievalParser()
    visible = "".join(parser.feed(text[i : i + width]) for i in range(0, len(text), width)) + parser.feed(
        "", final=True
    )
    assert visible == "正文\n<UpdateVariable><JSONPatch>[]</JSONPatch></UpdateVariable>"
    assert split_retrieval(text)[0] == visible
    assert parser.query == ("灯塔 身份" if "</tavern_retrieval>" in hidden else "")


def test_missing_duplicate_long_and_truncated_protocol():
    assert split_retrieval("正文")[0] == "正文"
    clean, query, diagnostics = split_retrieval(
        "正文<tavern_retrieval>abcdef</tavern_retrieval><tavern_retrieval>x</tavern_retrieval>", 3
    )
    assert (clean, query) == ("正文", "abcdef")
    assert len(diagnostics) == 2
    assert split_retrieval("正文<tavern_retrieval>私有数据")[0] == "正文"


def test_query_does_not_scan_whole_history_and_uses_current_input():
    history = [{"id": "old", "model": "禁止旧剧情"}, {"id": "a", "retrieval_query": "港口 守卫"}]
    assert query_text(history, "新人物小舟", RetrievalSettings()) == "港口 守卫\n新人物小舟"
    assert query_text(history[:1], "新人物小舟", RetrievalSettings()) == "新人物小舟"


def test_long_retrieval_input_keeps_its_tail():
    text = "开头" * 2000 + "尾部人物"
    assert query_text([], text, RetrievalSettings(input_chars=64)) == text


def test_tokenizers_cover_three_languages():
    assert "lighthouse" in tokenize("The Lighthouse")
    assert "灯塔" in tokenize("来到灯塔寻找守卫")
    assert "灯台" in tokenize("灯台で船を待つ")


def test_bm25_finds_content_without_keywords():
    index = SparseIndex(
        [
            {"id": "a", "keys": ["门锁"], "content": "银色钥匙打开灯塔的门"},
            {"id": "b", "keys": ["云朵"], "content": "红色山谷里有花"},
        ],
        RetrievalSettings(),
    )
    assert index.search("银色钥匙 灯塔", RetrievalSettings())[0][0] == "a"


def test_title_retrieval_toggle_uses_comment_then_name():
    entries = [
        {"id": "a", "keys": ["门锁"], "comment": "", "name": "银色钥匙", "content": "红色山谷里有花"},
        {"id": "b", "keys": ["云朵"], "comment": "灯塔守卫", "name": "银色钥匙", "content": "蓝色海面"},
    ]
    on, off = RetrievalSettings(), RetrievalSettings(title_in_retrieval=False)
    assert SparseIndex(entries, on).search("银色钥匙", on)[0] == ["a"]
    assert SparseIndex(entries, off).search("银色钥匙", off)[0] == []


def test_fusion_ignores_score_scale_and_deduplicates():
    entries = [{"id": str(i)} for i in range(3)]
    assert fuse({"keyword": ["0", "1"], "bm25": ["1", "2"]}, entries, RetrievalSettings())[0] == "1"
    assert fuse({"bm25": ["2", "0"]}, entries, RetrievalSettings()) == ["2", "0"]


def test_hybrid_obeys_secondary_and_timeline_and_disabled_constraints():
    entries = [
        {"id": str(i), "keys": ["never"], "content": "银色钥匙打开灯塔的门", **values}
        for i, values in enumerate(
            [{}, {"secondary_keys": ["许可"], "selective": True}, {"enabled": False}, {"timelines": ["过去"]}]
        )
    ]
    doc = {"character_book": {"entries": entries}}
    session = {"id": "s", "persona": "用户", "recipe": ContextRecipe().model_dump()}
    result = compile_context(
        doc,
        session,
        [],
        {"state": {"世界": {"当前时间线": "现在"}}},
        "银色钥匙 灯塔",
        Settings(lore_recursive=False),
        "t",
    )
    assert [e["id"] for e in result["lore"]] == ["0"]


async def test_provider_secrets_and_role_routing(tmp_path):
    app = create_app(tmp_path)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
            config = (await client.get("/api/settings")).json()
            providers = config["shared"]["providers"] + [
                {"id": "a", "api_key": "secret-a"},
                {"id": "b", "api_key": "secret-b"},
            ]
            response = await client.patch(
                "/api/settings/shared", json={"expected_revision": 0, "changes": {"providers": providers}}
            )
            assert response.status_code == 200, response.text
            response = await client.patch(
                "/api/settings/half_agent",
                json={
                    "expected_revision": 0,
                    "changes": {
                        "foreground.provider_id": "a",
                        "foreground.model": "story",
                        "background.provider_id": "b",
                        "background.model": "memory",
                    },
                },
            )
            assert response.status_code == 200, response.text
            assert "secret-" not in response.text
            saved = app.state.store.settings()
            assert saved.for_role("foreground").api_key == "secret-a"
            assert saved.for_role("background").api_key == "secret-b"
            config = response.json()
            response = await client.patch(
                "/api/settings/shared",
                json={
                    "expected_revision": config["revisions"]["shared"],
                    "changes": {"providers": config["shared"]["providers"]},
                },
            )
            assert response.status_code == 200
            assert app.state.store.settings().for_role("background").api_key == "secret-b"
            response = await client.patch(
                "/api/settings/shared",
                json={
                    "expected_revision": response.json()["revisions"]["shared"],
                    "changes": {"providers": config["shared"]["providers"][:1]},
                },
            )
            assert response.status_code == 400

    finally:
        await app.state.service.close()


async def test_neural_missing_degrades_and_does_not_download(tmp_path):
    app = create_app(tmp_path)
    service = app.state.service
    settings = Settings(retrieval=RetrievalSettings(channels=["embedding"]))
    result = await service.retrieval.prepare(
        {"character_book": {"entries": [{"id": "a", "keys": ["灯塔"], "content": "灯塔设定"}]}},
        [],
        "灯塔",
        {"state": {}},
        settings,
        {"id": "s"},
        "t",
    )
    assert result["ids"] == ["a"]
    assert result["diagnostics"]
    assert not service.local_models.jobs
    assert not (tmp_path / "models").exists()
    await service.close()


async def test_committed_clues_never_enter_model_history(tmp_path):
    import json

    from kohakuterrarium.testing.llm import ScriptedLLM

    from tavern.core.models import SessionCreate, TurnRequest

    app = create_app(tmp_path)
    store, service = app.state.store, app.state.service
    card = store.import_card(json.dumps({"name": "角色", "first_mes": "你好"}).encode(), "c.json")
    session = store.new_session(SessionCreate(card_id=card["id"]))
    provider = ScriptedLLM(["正文<tavern_retrieval>唯一隐藏标记xyz</tavern_retrieval>"])
    service.provider_factory = lambda: provider
    try:
        turn = service.submit(
            session["id"], TurnRequest(text="继续", operation_id="one", expected_revision=0)
        )
        await service.tasks[turn["id"]]
        assert store.turn(turn["id"])["status"] == "committed"
        message = store.session(session["id"])["messages"][-1]
        assert message["retrieval_query"] == "唯一隐藏标记xyz"
        assert message["raw"] == message["model"] == message["display"] == "正文"
        streamed = "".join(
            event["payload"].get("text", "") for event in store.events(turn["id"]) if event["kind"] == "text"
        )
        assert "唯一隐藏标记xyz" not in streamed and "<tavern" not in streamed
        next_provider = ScriptedLLM(["下一轮正文"])
        service.provider_factory = lambda: next_provider
        turn = service.submit(
            session["id"], TurnRequest(text="新话题", operation_id="two", expected_revision=1)
        )
        await service.tasks[turn["id"]]
        assert "唯一隐藏标记xyz" not in json.dumps(next_provider.call_log, ensure_ascii=False)
    finally:
        await service.close()


def stalled_worker(pipe):
    import time

    time.sleep(30)


async def test_timeout_terminates_worker_and_requires_explicit_rewarm(tmp_path):
    import multiprocessing as mp

    from tavern.search.local_models import LocalModelManager
    from tavern.search.retrieval import digest
    from tavern.storage.store import Store

    manager = LocalModelManager(Store(tmp_path))
    settings = Settings()
    parent, child = mp.get_context("spawn").Pipe()
    process = mp.get_context("spawn").Process(target=stalled_worker, args=(child,), daemon=True)
    process.start()
    child.close()
    manager.host = (process, {"embedding": parent})
    for purpose in ("embedding", "reranker"):
        manager.loaded[purpose] = digest(getattr(settings.local_models, purpose).model_dump())
        manager.ready.add(purpose)
    try:
        with pytest.raises(TimeoutError):
            await manager.infer("embedding", "embed", ["query"], settings, timeout=0.05)
        assert not process.is_alive()
        # Both purposes share the terminated host process, so both need an explicit rewarm.
        assert manager.host is None and not manager.ready and not manager.loaded
        with pytest.raises(RuntimeError, match="预热"):
            await manager.infer("embedding", "embed", ["query"], settings, timeout=0.05)
    finally:
        await manager.close()


async def test_reported_model_error_keeps_sibling_model_warm(tmp_path):
    from types import SimpleNamespace

    from tavern.search.local_models import LocalModelManager
    from tavern.search.retrieval import digest
    from tavern.storage.store import Store

    manager = LocalModelManager(Store(tmp_path))
    settings = Settings()
    pipe = SimpleNamespace(send=lambda payload: None, poll=lambda: True, recv=lambda: {"error": "CUDA OOM"})
    process = SimpleNamespace(is_alive=lambda: True)
    host = manager.host = (process, {"embedding": pipe, "reranker": pipe})
    for purpose in ("embedding", "reranker"):
        manager.loaded[purpose] = digest(getattr(settings.local_models, purpose).model_dump())
        manager.ready.add(purpose)
    with pytest.raises(RuntimeError, match="CUDA OOM"):
        await manager.infer("reranker", "rerank", [["q", "d"]], settings, timeout=1)
    assert manager.host is host
    assert manager.ready == {"embedding"} and set(manager.loaded) == {"embedding"}


async def test_changed_and_deleted_entries_are_not_served_from_stale_indexes(tmp_path):
    app = create_app(tmp_path)
    engine = app.state.service.retrieval
    settings = Settings()
    doc = {"character_book": {"entries": [{"id": "a", "keys": ["银钥匙"], "content": "银钥匙打开门"}]}}
    try:
        first = await engine.prepare(doc, [], "银钥匙", {}, settings, {"id": "s"}, "first")
        assert first["ids"] == ["a"]
        doc["character_book"]["entries"] = [{"id": "b", "keys": ["火山"], "content": "岩浆喷发"}]
        changed = await engine.prepare(doc, [], "银钥匙", {}, settings, {"id": "s"}, "second")
        assert "a" not in changed["ids"]
        doc["character_book"]["entries"] = []
        deleted = await engine.prepare(doc, [], "火山", {}, settings, {"id": "s"}, "third")
        assert deleted["ids"] == []
    finally:
        await app.state.service.close()


async def test_windowed_recall_uses_absolute_turn_for_cooldown(tmp_path):
    app = create_app(tmp_path)
    try:
        document = {
            "character_book": {
                "entries": [{"id": "e", "keys": ["灯塔"], "content": "灯塔守卫", "cooldown": 1}]
            }
        }
        history = [{"id": "recent", "role": "assistant", "model": "继续", "retrieval_query": ""}]
        result = await app.state.service.retrieval.prepare(
            document,
            history,
            "灯塔",
            {"lore": {"e": {"present": False, "removed_at": 20}}},
            Settings(),
            {"id": "s"},
            "t",
            turn_number=25,
        )
        assert result["ids"] == ["e"]
    finally:
        await app.state.service.close()


def test_bm25_rejects_character_only_and_weak_relative_winners():
    config = RetrievalSettings()
    index = SparseIndex([{"id": "a", "content": "cathedral mountain"}], config)
    assert index.search("catastrophe", config)[0] == []
    assert index.search("mountain ocean river forest desert glacier", config)[0] == []
    assert index.search("mountain", config)[0] == ["a"]


async def test_index_preparation_never_runs_query_or_reranker(tmp_path, monkeypatch):
    app = create_app(tmp_path)
    store = app.state.store
    engine = app.state.service.retrieval
    manager = app.state.service.local_models
    settings = store.settings()
    settings.retrieval.channels = ["bm25", "embedding"]
    settings.local_models.reranker_enabled = True
    store.save_settings(settings)
    scheduled = []

    async def forbidden(*args, **kwargs):
        pytest.fail("Index preparation must not run query inference")

    monkeypatch.setattr(manager, "infer", forbidden)
    monkeypatch.setattr(manager, "status", lambda _: {"models": {"embedding": {"ready": True}}})
    monkeypatch.setattr(
        manager, "schedule_index", lambda engine, missing, settings: scheduled.extend(missing)
    )
    doc = {
        "name": "fixture",
        "first_mes": "开场",
        "character_book": {"entries": [{"id": "a", "keys": ["灯塔"], "content": "灯塔守卫"}]},
    }
    try:
        card = store.import_card(json.dumps(doc, ensure_ascii=False).encode(), "fixture.json")
        session = store.new_session(SessionCreate(card_id=card["id"]))
        snapshot = copy.deepcopy(session["snapshot"])
        snapshot[ARCHIVE] = [
            archive_entry(Record(id="old", category="episode", content="在灯塔借出戒指").model_dump(), 1)
        ]
        store._save_memory_snapshot(session["id"], session, snapshot, session["revision"])
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
            response = await client.post(f"/api/sessions/{session['id']}/retrieval/rebuild")
        assert response.status_code == 200
        job = response.json()["id"]
        await manager.tasks[job]
        assert manager.jobs[job]["status"] == "complete", manager.jobs[job]
        assert len(scheduled) == 2
        assert any(entry["id"] == "memory:old" for _, entry in scheduled)
        with store.connection() as db:
            keys = json.loads(
                db.execute(
                    "SELECT keys FROM retrieval_collections WHERE session=?", (session["id"],)
                ).fetchone()[0]
            )
        assert {key for key, _ in scheduled} <= set(keys)
        assert not engine.cache
    finally:
        await app.state.service.close()


async def test_index_job_publishes_incremental_counts(tmp_path, monkeypatch):
    import asyncio

    app = create_app(tmp_path)
    manager = app.state.service.local_models
    engine = app.state.service.retrieval
    started_second = asyncio.Event()
    finish_second = asyncio.Event()
    calls = 0

    async def infer(*args, **kwargs):
        nonlocal calls
        assert kwargs["timeout"] == 180
        calls += 1
        if calls == 2:
            started_second.set()
            await finish_second.wait()
        return [{"fragments": ["test"], "vectors": [[1.0]]}]

    monkeypatch.setattr(manager, "status", lambda settings: {"models": {"embedding": {"ready": True}}})
    monkeypatch.setattr(manager, "infer", infer)
    try:
        settings = Settings()
        settings.retrieval.index_timeout = 180
        job = manager.schedule_index(engine, [("one", {"id": "a"}), ("two", {"id": "b"})], settings)
        await asyncio.wait_for(started_second.wait(), 2)
        assert job["completed_entries"] == 1
        assert job["total_entries"] == 2
        with manager.store.connection() as db:
            assert db.execute("SELECT count(*) FROM retrieval_vectors").fetchone()[0] == 1
        finish_second.set()
        await manager.tasks[job["id"]]
        assert job["completed_entries"] == 2
        assert job["status"] == "complete"
    finally:
        await app.state.service.close()


async def test_archive_recall_admits_relevant_episodes_into_their_own_frame():
    key = Record(
        id="key",
        category="episode",
        content="守塔人把地下室钥匙交给了用户",
        people=["守塔人"],
        kind="claim",
        known_to=["守塔人"],
        story_time="昨夜",
        manual=True,
        status="completed",
    )
    umbrella = Record(id="umbrella", category="episode", content="用户在集市买了一把红伞")
    entries = [archive_entry(r.model_dump(), 3) for r in (key, umbrella)]
    history = [
        {"id": "g", "role": "assistant", "raw": "开场", "model": "开场", "snapshot": {}},
        {"id": "u1", "role": "user", "raw": "走", "model": "走", "snapshot": {ARCHIVE: entries[:1]}},
        {"id": "a1", "role": "assistant", "raw": "好", "model": "好", "snapshot": {ARCHIVE: entries[1:]}},
    ]
    session = {"id": "s", "persona": "用户", "recipe": ContextRecipe().model_dump()}
    query = "去钟塔找守塔人要地下室钥匙"
    out = compile_context({"name": "向导"}, session, history, {"state": {}}, query, Settings(), "fixture")
    recall = [m for m in out["trace"] if m.get("context_source") == "memory_recall"]
    assert len(recall) == 1 and "守塔人把地下室钥匙交给了用户" in recall[0]["content"]
    assert "红伞" not in recall[0]["content"] and out["retrieval"]["memory_recall"]["ids"] == ["key"]
    assert all(
        label in recall[0]["content"]
        for label in ("手工修订", "角色说法", "已完成", "明确知情：守塔人", "时间：昨夜")
    )
    scripted = compile_context(
        {"name": "向导"},
        session,
        history,
        {"state": {}},
        query,
        Settings(),
        "fixture",
        retrieval=out["retrieval"],
        generate={"ordered_prompts": ["memory", "user_input"]},
    )
    assert any(m.get("context_source") == "memory_recall" for m in scripted["trace"])
    prepared = {k: v for k, v in out["retrieval"].items() if k != "memory_recall"}
    before = copy.deepcopy(prepared)
    repeated = compile_context(
        {"name": "向导"},
        session,
        history,
        {"state": {}},
        query,
        Settings(),
        "fixture",
        retrieval=prepared,
    )
    assert prepared == before and "memory_recall" not in prepared
    assert repeated["retrieval"]["memory_recall"] == out["retrieval"]["memory_recall"]
    assert (
        "memory_recall"
        not in compile_context(
            {"name": "向导"},
            session,
            history,
            {"state": {}},
            query,
            Settings(memory_recall_count=0),
            "fixture",
        )["retrieval"]
    )

    class Reranker:
        def __init__(self, scores):
            self.scores = scores

        async def infer(self, *args, **kwargs):
            if isinstance(self.scores, Exception):
                raise self.scores
            return self.scores

    rows = [{k: r[k] for k in ("id", "content", "keys", "comment")} for r in retrieval_entries(entries)]
    engine = RetrievalEngine.__new__(RetrievalEngine)
    settings = Settings(local_models={"reranker_enabled": True})
    engine.models = Reranker([0.2])
    rejected = await engine.recall_archive(rows, query, settings)
    assert rejected["ids"] == [] and rejected["diagnostics"][0]["id"] == "key"
    # A failed reranker keeps only lexical evidence, never a weaker shortlist.
    engine.models = Reranker(RuntimeError("down"))
    assert (await engine.recall_archive(rows, query, settings))["ids"] == ["key"]


async def test_lore_and_archive_publish_complete_vector_indexes_independently(tmp_path):
    from tavern.advisor.tools import AdvisorTools
    from tavern.core.model_view import ModelView
    from tavern.storage.maintenance import _plan
    from tavern.tracker.task import TrackerTask

    class Models:
        def __init__(self):
            self.missing = []

        def revision(self, settings):
            return "fixture"

        def schedule_index(self, engine, missing, settings):
            self.missing = missing

        async def infer(self, *args, **kwargs):
            return [[1.0]]

    with closing(sqlite3.connect(":memory:")) as db:
        db.row_factory = sqlite3.Row
        db.execute("CREATE TABLE sessions(id TEXT PRIMARY KEY, revision INTEGER)")
        db.execute("INSERT INTO sessions VALUES ('s', 0)")
        db.execute(
            "CREATE TABLE turns(id TEXT, session TEXT, status TEXT, created REAL, request TEXT, preview TEXT, draft TEXT)"
        )
        models = Models()
        engine = RetrievalEngine(SimpleNamespace(connection=lambda: db), models)
        settings = Settings(retrieval={"channels": ["embedding"]}, local_models={"reranker_enabled": False})
        document = {
            "character_book": {
                "entries": [{"id": "lore", "keys": ["other"], "content": "Northern tower setting"}]
            }
        }
        session = {"id": "s", "persona": "User", "recipe": ContextRecipe().model_dump()}
        args = (document, [], "semanticquery", {"state": {}}, settings, session, "fixture")

        def finish_index():
            for key, entry in models.missing:
                db.execute(
                    "INSERT INTO retrieval_vectors VALUES (?,?)",
                    (key, json.dumps({"fragments": [entry["content"]], "vectors": [[1.0]]})),
                )
            engine.cache.clear()
            models.missing = []

        await engine.prepare(*args, index_only=True)
        finish_index()
        assert (await engine.prepare(*args))["rankings"]["embedding"] == ["lore"]
        archive = [
            archive_entry(Record(id="old", category="episode", content="Archived event").model_dump(), 1)
        ]
        queries = []
        db.set_trace_callback(queries.append)
        recalled = await engine.prepare(*args, archive=archive)
        assert recalled["rankings"]["embedding"] == ["lore"]
        assert recalled["memory_recall"]["ids"] == []
        assert [entry["id"] for _, entry in models.missing] == ["memory:old"]
        archive_key = models.missing[0][0]
        assert len([q for q in queries if q.startswith("SELECT value FROM retrieval_vectors")]) == 1
        engine.cache.clear()
        queries.clear()
        await engine.prepare(*args, archive=archive)
        assert len([q for q in queries if q.startswith("SELECT value FROM retrieval_vectors")]) == 1
        compiled = compile_context(
            document, session, [], {"state": {}}, "semanticquery", settings, "fixture", retrieval=recalled
        )
        assert "Northern tower setting" in json.dumps(compiled["messages"])

        finish_index()
        edited = copy.deepcopy(document)
        edited["character_book"]["entries"][0]["content"] = "Changed tower setting"
        recalled = await engine.prepare(edited, *args[1:], archive=archive)
        assert "embedding" not in recalled["rankings"]
        assert recalled["memory_recall"]["evidence"]["old"]["channels"] == ["embedding"]

        def live_keys():
            return json.loads(
                db.execute("SELECT keys FROM retrieval_collections WHERE session='s'").fetchone()[0]
            )

        registered = live_keys()
        assert archive_key in registered
        story = {**session, "document": edited, "snapshot": {"state": {}}, "agent_mode": "half_agent"}
        advisor = AdvisorTools(
            SimpleNamespace(
                story=story, cfg=settings, service=SimpleNamespace(retrieval=engine), rid="consult"
            )
        )
        await advisor.dispatch("recall_lore", SimpleNamespace(query="semanticquery"))
        tracker = SimpleNamespace(
            retrieval=engine,
            view=ModelView({}),
            document=edited,
            committed_history=[],
            search_snapshot=lambda: {"state": {}},
            settings=settings,
            session=session,
            tid="tracker",
        )
        await TrackerTask.execute_tool(tracker, "search_lore", {"query": "semanticquery"})
        assert live_keys() == registered
        cache_remove = _plan(SimpleNamespace(root=tmp_path, settings=lambda: settings), db, 0)[2]
        assert archive_key not in {row["key"] for row in cache_remove}

        settings.memory_recall_count = 0
        models.missing = []
        recalled = await engine.prepare(*args, archive=archive)
        assert recalled["rankings"]["embedding"] == ["lore"]
        assert "memory_recall" not in recalled and not models.missing
