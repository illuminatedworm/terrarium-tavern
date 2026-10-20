import asyncio
import threading

from httpx import ASGITransport, AsyncClient

from tavern.application.api import create_app
from tavern.core.models import SessionCreate


async def test_slow_preview_does_not_block_other_requests(tmp_path, monkeypatch):
    app = create_app(tmp_path)
    store = app.state.store
    card = store.import_card(b'{"name":"Test","first_mes":"Hello"}', "test.json")
    session = store.new_session(SessionCreate(card_id=card["id"]))
    started, release = threading.Event(), threading.Event()

    def slow_compile(*args, **kwargs):
        started.set()
        assert release.wait(5), "Preview blocked the event loop"
        return {"tokens": 123, "messages": [], "trace": []}

    monkeypatch.setattr("tavern.application.api.compile_context", slow_compile)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as api:
            preview = asyncio.create_task(api.post(f"/api/sessions/{session['id']}/preview", json={}))
            try:
                assert await asyncio.to_thread(started.wait, 2)
                response = await asyncio.wait_for(api.get("/api/sessions"), 1)
                assert response.status_code == 200
                assert not preview.done()
            finally:
                release.set()
                result = await preview
            assert result.status_code == 200
            assert result.json()["tokens"] == 123
    finally:
        release.set()
        await app.state.service.close()
