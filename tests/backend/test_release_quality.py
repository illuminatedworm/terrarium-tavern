"""Release regressions use invented lore, never the private playthrough."""
import copy
import json
import sqlite3
import time

import pytest

from tavern.conversation.context import compile_context
from tavern.core.models import ContextRecipe, SessionCreate, Settings, TurnRequest
from tavern.storage.store import Store


def test_final_request_cannot_resurrect_rejected_lore():
    document = {"name": "Test", "character_book": {"entries": [
        {"id": "harbor", "keys": ["港口"], "content": "港口附近有钟塔。"},
        {"id": "tower", "keys": ["钟塔"], "content": "钟塔的秘密与当前行动无关。"},
    ]}}
    session = {"id": "s", "persona": "旅人", "recipe": ContextRecipe().model_dump(),
               "lore_injection_mode": "conversation"}
    history = [{"id": "g", "role": "assistant", "raw": "欢迎", "model": "欢迎"}]
    retrieval = {"ids": ["harbor"], "query": "进入港口", "diagnostics": [],
                 "rerank_scores": {"harbor": .9, "tower": .1}}
    result = compile_context(document, session, history, {}, "进入港口", Settings(), "turn",
                             retrieval=retrieval)
    assert [r["id"] for r in result["next_lore_injections"]] == ["harbor"]
    snapshot = {"lore": result["next_lore"], "lore_injections": result["next_lore_injections"]}
    original = copy.deepcopy(snapshot)
    result = compile_context(document, session, history, snapshot, "静静等待", Settings(), "next",
                             retrieval={"ids": [], "query": "静静等待", "diagnostics": []})
    assert [r["id"] for r in result["next_lore_injections"]] == ["harbor"]
    assert snapshot == original


def test_database_version_is_stable_and_newer_database_is_untouched(tmp_path):
    store = Store(tmp_path)
    with store.connection() as db:
        original = db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
    Store(tmp_path)
    with store.connection() as db:
        assert db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == original
    newer = tmp_path / "future"
    newer.mkdir()
    with sqlite3.connect(newer / "tavern.sqlite3") as db:
        db.execute("CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT)")
        db.execute("INSERT INTO meta VALUES ('schema_version','9999')")
    with pytest.raises(ValueError, match="版本"):
        Store(newer)
    with sqlite3.connect(newer / "tavern.sqlite3") as db:
        assert db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == [("meta",)]


@pytest.mark.parametrize("reranker", ["ready", "failed"])
async def test_recursive_admission_and_degradation_use_original_query(tmp_path, reranker):
    from tavern.search.retrieval import RetrievalEngine

    class Models:
        async def infer(self, purpose, action, pairs, settings, **kwargs):
            if reranker == "failed":
                raise TimeoutError("fixture offline")
            assert all(query == "港口" for query, _ in pairs)
            return [.9 if "港口" in content else .1 for _, content in pairs]

    entries = [
        {"id": "port", "keys": ["港口"], "content": "港口连接钟塔和沙漠。"},
        {"id": "tower", "keys": ["钟塔"], "content": "钟塔负责港口导航。", "preventRecursion": True},
        {"id": "desert", "keys": ["沙漠"], "content": "沙漠深处有古墓。"},
        {"id": "noise", "keys": ["都"], "content": "都城的无关档案。"},
    ]
    cfg = Settings(retrieval={"channels": ["keyword"]})
    cfg.local_models.reranker_enabled = True
    engine = RetrievalEngine(Store(tmp_path), Models())
    result = await engine.prepare({"character_book": {"entries": entries}}, [], "港口", {}, cfg,
                                  {"id": "test"}, "turn")
    assert result["ids"] == (["port", "tower"] if reranker == "ready" else ["port"])
    assert "desert" not in result["ids"]
    assert any("递归" in d["reason"] for d in result["diagnostics"])


async def test_two_hundred_rounds_bound_actual_window_and_keep_story_sources(tmp_path):
    from tavern.memories.memory import Tracker, fingerprint

    cfg = Settings(history_message_cap=20, chunk_size=7, memory_interval_turns=100,
                   daily_context_budget=10000, retrieval={"channels": ["keyword"]})
    manager = Tracker(Store(tmp_path))
    session = {"id": "long", "persona": "旅人", "recipe": ContextRecipe().model_dump(),
               "lore_injection_mode": "conversation"}
    history = [{"id": "g", "role": "assistant", "raw": "开场", "model": "开场"}]
    snapshot = {"state": {}, "memory_document": {"version": 1, "records": []}}
    cuts = []

    async def archive(messages, **kwargs):
        payload = json.loads(messages[-1]["content"])
        assert all(m["role"] in ("user", "assistant") for m in payload["messages"])
        assert all("资料正文" not in p["text"] for m in payload["messages"] for p in m["passages"])
        return json.dumps({"operations": [], "questions": []}), {}

    for turn in range(200):
        doc = {"name": "旅人", "character_book": {"entries": [
            {"id": f"entry-{turn}", "keys": [f"地点{turn}"], "content": "资料正文" * 20}]}}
        retrieval = {"ids": [f"entry-{turn}"], "query": f"地点{turn}", "diagnostics": []}
        request = TurnRequest(text=f"走到地点{turn}", operation_id=str(turn), expected_revision=turn)
        await manager.prepare(str(turn), doc, session, history, snapshot, request, cfg, archive, retrieval=retrieval)
        before = copy.deepcopy(snapshot)
        await manager.prepare(str(turn), doc, session, history, snapshot,
                              request.model_copy(update={"mode": "regenerate"}), cfg, archive, retrieval=retrieval)
        assert snapshot == before
        compiled = compile_context(doc, session, history, snapshot, request.text, cfg, str(turn),
                                   retrieval=retrieval, input_message_id=f"u{turn}")
        assert compiled["window_message_count"] < 20
        assert compiled["estimated_tokens"] < cfg.daily_context_budget
        if window := snapshot.get("context_window"):
            cuts.append(window["covered_count"])
            assert snapshot["memory_archive"]["covered_count"] >= window["covered_count"]
            assert window["fingerprint"] == fingerprint(history[:window["covered_count"]])
        snapshot.update(lore=compiled["next_lore"], lore_injections=compiled["next_lore_injections"])
        history.extend([{"id": f"u{turn}", "role": "user", "raw": request.text, "model": request.text},
                        {"id": f"a{turn}", "role": "assistant", "raw": "继续旅行。", "model": "继续旅行。"}])
    assert len(history) == 401 and len(set(cuts)) > 20


def test_storage_cleanup_preserves_story_branches_and_recoverable_failure(tmp_path):
    from tavern.storage.maintenance import cleanup, storage_report

    store = Store(tmp_path)
    card = store.import_card(json.dumps({"name": "测试", "first_mes": "开场"}).encode(), "card.json")
    session = store.new_session(SessionCreate(card_id=card["id"]))
    old, _ = store.create_turn(session["id"], TurnRequest(text="过去", operation_id="old", expected_revision=0))
    store.set_turn(old, status="committed", draft="duplicate", preview="{}")
    with store.connection() as db:
        db.execute("UPDATE turns SET created=? WHERE id=?", (time.time() - 20 * 86400, old))
    store.event(old, "provider_request", {"purpose": "narrative", "messages": ["private"]})
    store.event(old, "text", {"text": "old"})
    runs = tmp_path / "runs" / f"{old}.kohakutr.drives"
    runs.mkdir(parents=True)
    (runs / "nested-log").write_text("private")
    failed, _ = store.create_turn(session["id"], TurnRequest(text="重试", operation_id="failed", expected_revision=0))
    store.set_turn(failed, status="error", draft="recoverable")
    store.event(failed, "provider_request", {"messages": ["recoverable"]})
    with store.connection() as db:
        db.execute("UPDATE turns SET created=? WHERE id=?", (time.time() - 20 * 86400, failed))
    original = store.session(session["id"])["messages"]
    assert storage_report(store)["turns_to_compact"] == 1
    cleanup(store, vacuum=True)
    assert store.session(session["id"])["messages"] == original
    assert store.turn(failed)["draft"] == "recoverable"
    assert not runs.exists()
    assert store.turn(old)["preview"] is None
    assert store.events(old)[0]["payload"]["detail_expired"]
    assert store.work_turns(session["id"], limit=1)["total"] == 2
    assert len(store.work_turns(session["id"], limit=1)["items"]) == 1
    assert storage_report(store)["reclaimable_bytes"] == 0


def test_storage_quota_evicts_details_and_obsolete_indexes_but_not_models(tmp_path):
    from tavern.storage.maintenance import cleanup, storage_report

    store = Store(tmp_path)
    cfg = store.settings()
    cfg.log_max_mib = cfg.retrieval_cache_max_mib = 1
    store.save_settings(cfg)
    card = store.import_card(json.dumps({"name": "测试", "first_mes": "开场"}).encode(), "card.json")
    session = store.new_session(SessionCreate(card_id=card["id"]))
    turn, _ = store.create_turn(session["id"], TurnRequest(operation_id="large", expected_revision=0))
    store.set_turn(turn, status="committed")
    store.event(turn, "provider_request", {"messages": ["x" * (2 * 1024**2)]})
    with store.connection() as db:
        db.execute("CREATE TABLE retrieval_vectors(key TEXT PRIMARY KEY,value TEXT)")
        db.execute("CREATE TABLE retrieval_collections(session TEXT PRIMARY KEY,keys TEXT)")
        db.execute("INSERT INTO retrieval_vectors VALUES ('old',?)", ("x" * 4096,))
        db.execute("INSERT INTO retrieval_vectors VALUES ('current',?)", ("x" * 4096,))
        db.execute("INSERT INTO retrieval_collections VALUES (?,?)", (session["id"], '["current"]'))
    models = tmp_path / "models"
    models.mkdir()
    (models / "weights").write_text("not disposable")
    report = storage_report(store)
    assert report["turns_to_compact"] == 1 and report["cache_items_to_remove"] == 1
    cleanup(store)
    assert storage_report(store)["remaining_detail_bytes"] < 1024**2
    with store.connection() as db:
        assert [r[0] for r in db.execute("SELECT key FROM retrieval_vectors")] == ["current"]
    assert (models / "weights").read_text() == "not disposable"
