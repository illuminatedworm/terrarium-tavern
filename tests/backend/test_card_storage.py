import copy
from pathlib import Path

from httpx import ASGITransport, AsyncClient

from tavern.application.api import create_app
from tavern.core.models import SessionCreate


async def test_card_api_revision_save_keeps_existing_story_settings(tmp_path):
    app = create_app(tmp_path)
    store = app.state.store
    card = store.import_card(Path("samples/雾港来信.json").read_bytes(), "card.json")
    session = store.new_session(SessionCreate(card_id=card["id"]))
    doc = copy.deepcopy(card["document"])
    doc["data"]["description"] = "覆盖后的当前设定"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.put(f"/api/cards/{card['id']}", json={"document": doc, "expected_revision": card["revision"]})
        assert response.status_code == 200
        assert response.json()["document"] == doc
        assert response.json()["revision"] == card["revision"] + 1
        preview = await client.post(f"/api/sessions/{session['id']}/preview", json={"text": "继续"})
        assert preview.status_code == 200
        assert "覆盖后的当前设定" not in next(m["content"] for m in preview.json()["trace"] if m["source"] == "character")
        spec = (await client.get('/openapi.json')).json()
        for route in ("/api/cards/{cid}", "/api/cards/{cid}/export"):
            assert not any(p["name"] == "version" for p in spec["paths"][route]["get"]["parameters"])
