import copy
import json
from pathlib import Path

from tavern.core.models import SessionCreate, TurnRequest
from tavern.storage.store import Store
from tools.audit_runtime_cache import audit, wire_prefix


def test_wire_prefix_includes_tools_and_excludes_uncached_system_suffix():
    body = {"model": "claude-fixture", "tools": [{"name": "search", "description": "stable"}],
            "system": [{"type": "text", "text": "fixed", "cache_control": {"type": "ephemeral"}},
                       {"type": "text", "text": "uncached"}],
            "messages": [{"role": "user", "content": [{"type": "text", "text": "first"}]}]}
    original, path = wire_prefix(body, "anthropic", "cache_control")
    assert path == "system[0]"
    changed = copy.deepcopy(body)
    changed["messages"][0]["content"][0]["text"] = "second"
    changed["system"][1]["text"] = "different uncached text"
    assert wire_prefix(changed, "anthropic", "cache_control")[0] == original
    changed["tools"][0]["description"] = "different tools invalidate the prefix"
    assert wire_prefix(changed, "anthropic", "cache_control")[0] != original
    assert body["tools"][0]["description"] == "stable"


def test_audit_recovers_observed_usage_alias_without_exporting_prose_or_secrets(tmp_path):
    store = Store(tmp_path)
    card = store.import_card(Path("samples/雾港来信.json").read_bytes(), "card.json")
    session = store.new_session(SessionCreate(card_id=card["id"]))
    tid, _ = store.create_turn(session["id"], TurnRequest(operation_id="audit", expected_revision=0))
    secret = "PRIVATE_STORY_OR_KEY"
    store.event(tid, "provider_request", {"purpose": "narrative", "api_mode": "chat_completions",
        "model": "claude-fixture", "wire": {"model": "claude-fixture", "messages": [
            {"role": "system", "content": [{"type": "text", "text": secret, "cache_control": {"type": "ephemeral"}}]}]},
        "cache": {"status": "sent", "parameters": {"cache_control": {"type": "ephemeral"}}}})
    store.event(tid, "usage", {"purpose": "narrative", "usage": {
        "prompt_tokens": 9908, "completion_tokens": 159, "cached_tokens": None, "cache_write_tokens": None,
        "raw_usage": [{"prompt_tokens": 9908, "completion_tokens": 159,
                       "prompt_tokens_details": {"cache_creation_tokens": 9812}}]}})
    store.set_turn(tid, status="cancelled")
    report = audit(tmp_path, session["id"])
    usage = report["turns"][0]["usage"][0]
    assert usage["cache_write_tokens"] == 9812 and usage["cached_tokens"] is None
    assert usage["renormalized"] is True
    assert report["turns"][0]["requests"][0]["wire_breakpoint"] == "messages[0].content[0]"
    assert secret not in json.dumps(report)
    assert store.session(session["id"])["revision"] == session["revision"]
