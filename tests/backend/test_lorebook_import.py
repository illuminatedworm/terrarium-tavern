import copy
import json

import pytest
from httpx import ASGITransport, AsyncClient

from tavern.application.api import create_app
from tavern.character.cards import append_lorebook, lore_entries


@pytest.mark.parametrize("destination", ["array", "object"])
@pytest.mark.parametrize("source", ["array", "object"])
@pytest.mark.parametrize("wrapped", [False, True])
async def test_lorebook_upload_adds_entries_without_overwriting_existing_data(
    tmp_path, destination, source, wrapped
):
    old = {"id": 0, "content": "原有设定", "extensions": {"custom": {"keep": True}}}
    incoming = [
        {"id": "0", "content": "新设定", "extensions": {"custom": {"imported": True}}},
        {"id": "fresh", "content": "另一条新设定"},
    ]
    current = {
        "name": "当前世界书",
        "scan_depth": 7,
        "extensions": {"keep": True},
        "entries": [old] if destination == "array" else {"same": old},
    }
    imported = {
        "name": "导入世界书",
        "scan_depth": 1,
        "entries": incoming if source == "array" else {"same": incoming[0], "fresh": incoming[1]},
    }
    data = {"name": "测试卡", "description": "原人物设定", "character_book": current}
    document = {"spec": "chara_card_v3", "data": data} if wrapped else data
    app = create_app(tmp_path)
    store = app.state.store
    card = store.import_card(json.dumps(document).encode(), "card.json")
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
            for count in (3, 5):
                response = await client.post(
                    f"/api/cards/{card['id']}/lorebook",
                    files={
                        "file": ("lore.json", json.dumps(imported).encode(), "application/json"),
                    },
                )
                assert response.status_code == 200, response.text
                result = response.json()
                saved = result["document"]["data"] if wrapped else result["document"]
                book = saved["character_book"]
                rows = lore_entries(result["document"])
                assert len(rows) == count
                assert len({entry["id"] for entry in rows}) == count
                assert [entry["content"] for entry in rows] == ["原有设定", "新设定", "另一条新设定"] + (
                    ["新设定", "另一条新设定"] if count == 5 else []
                )
                assert book["name"] == current["name"] and book["scan_depth"] == 7
                assert book["extensions"] == current["extensions"]
                assert isinstance(book["entries"], list) == (destination == "array")
                assert (book["entries"][0] if destination == "array" else book["entries"]["same"]) == old
                assert saved["description"] == data["description"]
                assert result["revision"] == card["revision"] + (1 if count == 3 else 2)
                assert store.card(card["id"])["document"] == result["document"]
    finally:
        await app.state.editor.close()
        await app.state.service.close()


def test_import_preserves_implicit_identities_and_original_source():
    current = {"entries": [{"content": "原有索引条目"}]}
    imported = {"entries": [{"content": "索引条目"}, {"uid": "named", "content": "UID 条目"}]}
    originals = copy.deepcopy((current, imported))
    book = append_lorebook(current, imported)
    book = append_lorebook(book, imported)
    rows = lore_entries({"character_book": book})
    assert len({entry["id"] for entry in rows}) == 5
    assert rows[0]["id"] == "0" and rows[2]["id"] == "named"
    assert (current, imported) == originals


def test_import_retains_settings_without_existing_book_and_preserves_keyed_identities():
    imported = {"name": "新世界书", "extensions": {"custom": True}, "entries": {"named": {"content": "正文"}}}
    book = append_lorebook(None, imported)
    assert book == {**imported, "entries": {"named": {"id": "named", "content": "正文"}}}
    array = append_lorebook({"entries": []}, imported)
    assert array["entries"] == [{"id": "named", "uid": "named", "content": "正文"}]


def test_import_keeps_colliding_keys_and_extension_identities_independent():
    old = {"id": "old", "content": "原文"}
    added = {"id": "new", "content": "新文", "extensions": {"id": "shared"}}
    book = append_lorebook({"entries": {"same": old}}, {"entries": {"same": added}})
    assert book["entries"]["same"] == old
    assert list(book["entries"].values()) == [old, added]
    book = append_lorebook(book, {"entries": [added, added]})
    assert len({entry["id"] for entry in lore_entries({"character_book": book})}) == 4


@pytest.mark.parametrize("imported", [None, {"entries": "invalid"}, {"entries": [{"extensions": []}]}])
async def test_invalid_lorebook_upload_preserves_saved_card(tmp_path, imported):
    app = create_app(tmp_path)
    card = app.state.store.import_card(
        json.dumps(
            {
                "name": "测试卡",
                "character_book": {"entries": [{"id": "old", "content": "保留原文"}]},
            }
        ).encode(),
        "card.json",
    )
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
            response = await client.post(
                f"/api/cards/{card['id']}/lorebook",
                files={
                    "file": ("lore.json", json.dumps(imported).encode(), "application/json"),
                },
            )
            assert response.status_code == 400, response.text
            assert app.state.store.card(card["id"])["document"] == card["document"]
            assert app.state.store.card(card["id"])["revision"] == card["revision"]
    finally:
        await app.state.editor.close()
        await app.state.service.close()


def test_importing_empty_book_preserves_existing_settings_and_entries():
    current = {"name": "保留", "entries": {"old": {"content": "保留原文"}}}
    assert append_lorebook(current, {"name": "不覆盖", "entries": []}) == current
