import json
from pathlib import Path

import pytest

from tavern.conversation.context import compile_context, search
from tavern.core.cache_policy import normalize_usage
from tavern.core.models import SessionCreate, Settings, TurnRequest
from tavern.core.usage import aggregate_cache_usage
from tavern.storage.store import Conflict, Store


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path)


@pytest.fixture
def session(store):
    c = store.import_card(Path("samples/雾港来信.json").read_bytes(), "card.json")
    return store.new_session(SessionCreate(card_id=c["id"]))


def test_revision_and_idempotency(store, session):
    req = TurnRequest(text="你好", operation_id="one", expected_revision=0)
    tid, created = store.create_turn(session["id"], req)
    assert created
    assert store.create_turn(session["id"], req) == (tid, False)
    with pytest.raises(Conflict):
        store.create_turn(session["id"], req.model_copy(update={"text": "changed"}))
    with pytest.raises(Conflict):
        store.update_session(session["id"], 0, head=session["head"])
    store.recover()
    assert store.turn(tid)["status"] == "interrupted"


def test_conversation_cache_usage_sums_requests_without_filling_missing_counters(store, session):
    from tavern.storage.maintenance import cleanup

    samples = [
        normalize_usage(
            {"prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": 75}}, "chat_completions"
        ),
        normalize_usage({"input_tokens": 900, "input_tokens_details": {"cached_tokens": 225}}, "responses"),
        normalize_usage(
            {"input_tokens": 100, "cache_read_input_tokens": 800, "cache_creation_input_tokens": 100},
            "anthropic",
        ),
        normalize_usage({"promptTokenCount": 2000, "cachedContentTokenCount": 1000}, "gemini"),
    ]
    # Unequal request sizes must be weighted by tokens, not averaged as rates.
    assert aggregate_cache_usage(samples[:2]) == {"prompt_tokens": 1000, "cached_tokens": 300}
    assert aggregate_cache_usage([{"prompt_tokens": 0, "cached_tokens": 0, "raw_usage": {}}]) == {
        "prompt_tokens": None,
        "cached_tokens": None,
    }
    assert aggregate_cache_usage([{"cached_tokens": 40}])["prompt_tokens"] is None
    assert (
        aggregate_cache_usage([{"input_tokens": 100, "cache_read_input_tokens": 800}])["prompt_tokens"]
        is None
    )
    assert store.session_cache_usage(session["id"]) is None
    req = TurnRequest(text="你好", operation_id="usage", expected_revision=0)
    tid, _ = store.create_turn(session["id"], req)
    narrative = {
        "prompt_tokens": 1000,
        "cached_tokens": 300,
        "raw_usage": [usage["raw_usage"] for usage in samples[:2]],
    }
    store.event(tid, "usage", {"purpose": "narrative", "usage": narrative})
    store.event(tid, "usage", {"purpose": "maintenance", "usage": samples[2]})
    assert store.latest_usage(session["id"]) == narrative
    store.set_turn(tid, status="committed")
    next_tid, _ = store.create_turn(session["id"], req.model_copy(update={"operation_id": "next"}))
    store.event(next_tid, "usage", {"purpose": "narrative", "usage": samples[3]})
    store.set_turn(next_tid, status="committed")
    with store.connection() as db:
        db.execute(
            """INSERT INTO context_operations(id,session,operation,kind,status,expected_revision,payload,created)
            VALUES ('compact',?,'compact','compact','completed',0,?,0)""",
            (
                session["id"],
                json.dumps(
                    {
                        "usage": [{"prompt_tokens": 500, "cached_tokens": 200}],
                        "replay": {"private": "expired"},
                    }
                ),
            ),
        )
    total = {"prompt_tokens": 4500, "cached_tokens": 2300}
    assert store.session_cache_usage(session["id"]) == total
    other = store.new_session(SessionCreate(card_id=session["card"]))
    other_tid, _ = store.create_turn(other["id"], req)
    store.event(other_tid, "usage", {"usage": {}})
    store.set_turn(other_tid, status="committed")
    cleanup(store)
    assert store.session_cache_usage(session["id"]) == total
    with store.connection() as db:
        compacted = json.loads(
            db.execute("SELECT payload FROM context_operations WHERE id='compact'").fetchone()[0]
        )
        assert set(compacted) == {"usage"}
    store.event(next_tid, "usage", {"usage": {"prompt_tokens": 100, "cached_tokens": 0}})
    assert store.session_cache_usage(session["id"]) == {"prompt_tokens": 4600, "cached_tokens": 2300}
    store.event(next_tid, "usage", {"usage": {"prompt_tokens": 100}})
    assert store.session_cache_usage(session["id"]) == {"prompt_tokens": 4700, "cached_tokens": None}


def test_context_sends_mid_system_without_probe(store, session):
    card = store.card(session["card"])
    out = compile_context(
        card["document"], session, session["messages"], session["snapshot"], "灯塔", Settings(), "seed"
    )
    assert any(x["id"] == "1" for x in out["lore"])
    assert any(x["source"] == "lore:1" and x["role"] == "system" for x in out["trace"])
    assert not any(x["source"] == "provider-fallback" for x in out["trace"])


def test_user_system_prompt_is_used_by_context(store, session):
    settings = Settings(system_prompt="用户自定义系统提示")
    store.save_settings(settings)
    loaded = store.settings()
    out = compile_context(
        store.card(session["card"])["document"],
        session,
        session["messages"],
        session["snapshot"],
        "继续",
        loaded,
        "custom",
    )
    rules = next(item for item in out["trace"] if item["source"] == "rules")
    assert rules["content"] == "用户自定义系统提示" and rules["role"] == "system"


def test_aggregated_lore_preview_retains_entry_titles_and_ids(store, session):
    document = store.card(session["card"])["document"]
    data = document.get("data", document)
    data["character_book"]["entries"] = [
        {
            "id": "1",
            "keys": [],
            "enabled": True,
            "constant": True,
            "comment": "标题一",
            "content": "内容一",
            "extensions": {"position": 0},
        },
        {
            "id": "2",
            "keys": [],
            "enabled": True,
            "constant": True,
            "comment": "标题二",
            "content": "内容二",
            "extensions": {"position": 0},
        },
    ]
    out = compile_context(
        document, session, session["messages"], session["snapshot"], "继续", Settings(), "titles"
    )
    lore = [item for item in out["trace"] if item.get("lore_region") == "constant"]
    assert [item["lore_ids"] for item in lore] == [["1"], ["2"]]
    assert [item["title"] for item in lore] == ["标题一", "标题二"]
    assert all(set(message) == {"role", "content"} for message in out["messages"])


def test_card_edits_replace_current_content_for_existing_sessions(store, session):
    card = store.card(session["card"])
    doc = card["document"]
    doc["data"]["description"] = "NEW"
    updated = store.edit_card(card["id"], doc)
    assert updated["document"] == doc and "version" not in updated
    existing = store.session(session["id"])
    compiled = compile_context(
        store.card(existing["card"])["document"],
        existing,
        existing["messages"],
        existing["snapshot"],
        "continue",
        Settings(),
        "updated-card",
    )
    assert "NEW" in next(m["content"] for m in compiled["trace"] if m["source"] == "character")


def test_message_edit_is_in_place_and_invalidates_window(store, session):
    message = session["messages"][0]
    snapshot = {**message["snapshot"], "context_window": {"covered_count": 1}}
    with store.connection() as db:
        db.execute("UPDATE messages SET snapshot=? WHERE id=?", (json.dumps(snapshot), message["id"]))
    updated = store.edit_message(session["id"], message["id"], "修改后的原文", 0)
    assert updated["revision"] == 1
    assert len(updated["messages"]) == 1 and updated["messages"][0]["id"] == message["id"]
    assert updated["messages"][0]["raw"] == "修改后的原文"
    assert "context_window" not in updated["snapshot"]


def test_chinese_search():
    found = search([{"content": "蓝色灯塔在秋分亮起"}, {"content": "街角书店"}], "灯塔")
    assert len(found) == 1
