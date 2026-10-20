import base64
import copy
import io
import json
import zipfile

import pytest
from httpx import ASGITransport, AsyncClient
from PIL import Image, PngImagePlugin

from tavern.application.api import create_app
from tavern.core.models import SessionCreate


def png_image(color, document=None):
    metadata = PngImagePlugin.PngInfo()
    if document:
        metadata.add_text("ccv3", base64.b64encode(json.dumps(document).encode()).decode())
    buffer = io.BytesIO()
    Image.new("RGB", (16, 24), color).save(buffer, "PNG", pnginfo=metadata)
    return buffer.getvalue()


@pytest.mark.parametrize("container", ["png", "charx"])
async def test_portrait_survives_card_edits_deletion_and_session_fork(tmp_path, container):
    app = create_app(tmp_path)
    store = app.state.store
    document = {
        "spec": "chara_card_v3",
        "spec_version": "3.0",
        "data": {
            "name": "有头像的角色",
            "first_mes": "故事开始。",
            "assets": [{"type": "icon", "uri": "embeded://assets/portrait.png"}],
        },
    }
    expected = png_image("teal", document if container == "png" else None)
    replacement = png_image("coral")
    original = expected
    if container == "charx":
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as zipped:
            zipped.writestr("card.json", json.dumps(document))
            zipped.writestr("assets/portrait.png", expected)
            zipped.writestr("assets/replacement.png", replacement)
        original = archive.getvalue()
    card = store.import_card(original, f"card.{container}")
    session = store.new_session(SessionCreate(card_id=card["id"]))
    card_url = f"/api/cards/{card['id']}/portrait"
    session_url = f"/api/sessions/{session['id']}/portrait"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        for url in (card_url, session_url):
            response = await client.get(url)
            assert response.status_code == 200
            assert response.headers["content-type"] == "image/png"
            assert response.content == expected
        changed = copy.deepcopy(document)
        changed["data"]["assets"][0]["uri"] = "embeded://assets/replacement.png"
        changed["data"]["description"] = "后来修改的设定"
        store.edit_card(card["id"], changed, card["revision"])
        assert (await client.get(card_url)).content == (replacement if container == "charx" else expected)
        assert (await client.get(session_url)).content == expected
        store.delete_card(card["id"])
        assert (await client.get(card_url)).status_code == 404
        assert (await client.get(session_url)).content == expected
        fork = store.fork_session(session["id"], session["head"], 0)
        store.delete_session(session["id"])
        assert (await client.get(session_url)).status_code == 404
        assert (await client.get(f"/api/sessions/{fork['id']}/portrait")).content == expected


async def test_no_portrait_and_missing_image_return_not_found(tmp_path):
    app = create_app(tmp_path)
    store = app.state.store
    document = {"name": "纯文本角色", "first_mes": "你好。"}
    card = store.import_card(json.dumps(document).encode(), "card.json")
    session = store.new_session(SessionCreate(card_id=card["id"]))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        assert (await client.get(f"/api/cards/{card['id']}/portrait")).status_code == 404
        assert (await client.get(f"/api/sessions/{session['id']}/portrait")).status_code == 404
        card = store.import_card(png_image("teal", document), "card.png")
        session = store.new_session(SessionCreate(card_id=card["id"]))
        (tmp_path / "blobs" / card["original"]).unlink()
        assert (await client.get(f"/api/cards/{card['id']}/portrait")).status_code == 404
        assert (await client.get(f"/api/sessions/{session['id']}/portrait")).status_code == 404
