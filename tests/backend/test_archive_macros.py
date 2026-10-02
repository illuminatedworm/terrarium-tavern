import json

import pytest

from tavern.application.api import create_app
from tavern.conversation.scene import archive_history
from tavern.core.models import SessionCreate, TurnRequest
from tavern.memories.memory import fingerprint
from tavern.storage.store import uid
from tests.support.tracker_model import MemoryModel, batch_payload


def test_rendered_archive_cache_identity_changes_with_persona():
    history = [{"id": "m", "role": "assistant", "raw": "{{user}}在这里"}]
    assert fingerprint(archive_history(history, {"name": "角色"}, "甲")) != fingerprint(
        archive_history(history, {"name": "角色"}, "乙"))
    assert history[0]["raw"] == "{{user}}在这里"


@pytest.mark.parametrize("kind", ["memory"])
async def test_manual_refresh_uses_session_identity(kind, tmp_path):
    app = create_app(tmp_path)
    store, service = app.state.store, app.state.service
    settings = store.settings()
    settings.foreground.model = settings.background.model = "fixture"
    settings.providers[0].api_key = "local"
    store.save_settings(settings)
    card = store.import_card(json.dumps({"name": "洛伊", "first_mes": "{{user}}遇见{{char}}。"}).encode(), "card.json")
    session = store.new_session(SessionCreate(card_id=card["id"], persona="小舟"))
    response = {"operations": [], "questions": []}
    provider = MemoryModel([json.dumps(response)])
    service.memory_provider_factory = lambda: provider
    try:
        turn = service.submit(session["id"], TurnRequest(operation_id=uid(), expected_revision=0, scene_refresh=kind))
        await service.tasks[turn["id"]]
        assert store.turn(turn["id"])["status"] == "committed"
        payload = batch_payload(provider.call_log[0])
        assert payload["messages"][0]["passages"] == [{"ref": "S1", "text": "小舟遇见洛伊。"}]
        assert store.session(session["id"])["messages"][0]["raw"] == "{{user}}遇见{{char}}。"
    finally:
        await service.close()
