from types import SimpleNamespace

import pytest

from tavern.application.api import create_app
from tavern.core.models import Settings
from tavern.search.retrieval import SparseIndex, fuse, lexical_recall, tokenize
from tavern.search.retrieval_config import RetrievalSettings
from tavern.search.retrieval_text import query_text


def test_query_does_not_scan_whole_history_and_uses_current_input():
    history = [{"id": "old", "model": "禁止旧剧情"}, {"id": "a", "retrieval_query": "港口 守卫"}]
    assert query_text(history, "新人物小舟", RetrievalSettings()) == "港口 守卫\n新人物小舟"
    assert query_text(history[:1], "新人物小舟", RetrievalSettings()) == "新人物小舟"
    # Ooc text joins the query, but an empty story part still falls back to recent history.
    fallback = query_text(history[:1], "", RetrievalSettings())
    assert fallback
    assert query_text(history[:1], "让小舟出场", RetrievalSettings(), "") == fallback + "\n让小舟出场"


def test_long_retrieval_input_keeps_its_tail():
    text = "开头" * 2000 + "尾部人物"
    assert query_text([], text, RetrievalSettings(input_chars=64)) == text


def test_tokenizers_load_only_the_language_resources_needed(monkeypatch):
    from tavern.search import retrieval
    from tavern.search.japanese_dictionary import DictionaryUnavailable

    real_chinese = retrieval.chinese_tokenizer
    calls = []

    def chinese():
        calls.append("chinese")
        return real_chinese()

    monkeypatch.setattr(retrieval, "chinese_tokenizer", chinese)
    retrieval._tokenize.cache_clear()
    assert "lighthouse" in tokenize("The Lighthouse")
    assert not calls
    assert "灯塔" in tokenize("来到灯塔寻找守卫")
    assert calls
    with pytest.raises(DictionaryUnavailable):
        tokenize("灯台で船を待つ")
    # A Japanese worldbook needs the same resource even when the input is English;
    # missing data must escape the broad BM25 degradation handler.
    with pytest.raises(DictionaryUnavailable):
        retrieval.lexical_recall(
            {
                "character_book": {
                    "entries": [{"id": "ja", "keys": ["lighthouse"], "content": "灯台で船を待つ"}]
                }
            },
            [],
            "lighthouse",
            {},
            Settings(),
            {"id": "s"},
            "t",
        )


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
    result = lexical_recall(
        doc,
        [],
        "银色钥匙 灯塔",
        {"state": {"世界": {"当前时间线": "现在"}}},
        Settings(lore_recursive=False),
        {"id": "s", "persona": "用户"},
        "t",
    )
    assert result["rankings"]["bm25"] == ["0"]


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
