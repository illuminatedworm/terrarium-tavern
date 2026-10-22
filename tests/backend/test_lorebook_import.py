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


def test_shelf_sync_keeps_story_changes_unless_selected_and_full_selection_overwrites():
    from tavern.character.card_sync import apply_sync, sync_items

    base = {
        "spec": "chara_card_v3",
        "data": {
            "name": "雾港",
            "description": "旧描述",
            "first_mes": "开场",
            "character_book": {"entries": {"a": {"content": "旧 A"}, "b": {"content": "旧 B"}}},
            "extensions": {
                "regex_scripts": [{"id": "r1", "findRegex": "1"}, {"id": "r2", "findRegex": "2"}],
                "tavern_helper": [["scripts", []], ["variables", {"好感": 0}]],
            },
        },
    }
    story = copy.deepcopy(base)
    story["data"]["character_book"]["entries"]["b"]["content"] = "Tracker 改写 B"
    story["data"]["character_book"]["entries"]["t"] = {"content": "Tracker 新增"}
    shelf = copy.deepcopy(base)
    data = shelf["data"]
    data.update(description="新描述", first_mes="新开场", creator_notes="不同步")
    data["character_book"]["entries"]["a"]["content"] = "新 A"
    data["character_book"]["entries"]["n"] = {"content": "书架新增"}
    data["extensions"]["regex_scripts"] = [{"id": "r2", "findRegex": "2"}, {"id": "r1", "findRegex": "1!"}]
    data["extensions"]["tavern_helper"][0] = ["scripts", [{"name": "面板"}]]
    originals = copy.deepcopy((base, story, shelf))

    items = {item["key"]: item for item in sync_items(base, story, shelf)}
    assert {key: (item["change"], item["origin"]) for key, item in items.items()} == {
        "field:description": ("update", "card"),
        "lore:a": ("update", "card"),
        "lore:b": ("update", "story"),
        "lore:t": ("remove", "story"),
        "lore:n": ("add", "card"),
        "regex:r1": ("update", "card"),
        "regex-order": ("update", "card"),
        "scripts": ("update", "card"),
    }
    defaults = [key for key, item in items.items() if item["origin"] == "card"]
    merged = apply_sync(base, story, shelf, defaults)["data"]
    assert merged["description"] == "新描述" and merged["first_mes"] == "开场"
    assert {key: entry["content"] for key, entry in merged["character_book"]["entries"].items()} == {
        "a": "新 A",
        "b": "Tracker 改写 B",
        "t": "Tracker 新增",
        "n": "书架新增",
    }
    assert merged["extensions"]["regex_scripts"] == data["extensions"]["regex_scripts"]
    assert merged["extensions"]["tavern_helper"] == [
        ["scripts", [{"name": "面板"}]],
        ["variables", {"好感": 0}],
    ]

    overwritten = apply_sync(base, story, shelf, list(items))
    assert sync_items(base, overwritten, shelf) == []
    assert overwritten["data"]["character_book"] == data["character_book"]
    assert overwritten["data"]["first_mes"] == "开场" and "creator_notes" not in overwritten["data"]
    with pytest.raises(ValueError, match="同步项已变化"):
        apply_sync(base, overwritten, shelf, ["lore:a"])
    # Entries mounted from the worldbook library are never offered and survive a full overwrite.
    mounted = copy.deepcopy(story)
    mounted["data"]["character_book"]["entries"]["m"] = {"content": "挂载条目"}
    assert sync_items(base, mounted, shelf, {"m"}) == list(items.values())
    kept = apply_sync(base, mounted, shelf, list(items), {"m"})["data"]["character_book"]["entries"]
    assert kept == {**data["character_book"]["entries"], "m": {"content": "挂载条目"}}
    assert (base, story, shelf) == originals


async def test_session_card_sync_commits_selected_items_and_rejects_stale_shelf(tmp_path):
    from tavern.core.models import SessionCreate

    app = create_app(tmp_path)
    store = app.state.store
    document = {
        "name": "测试卡",
        "description": "旧描述",
        "character_book": {"entries": [{"id": "x", "content": "旧"}]},
    }
    card = store.import_card(json.dumps(document).encode(), "card.json")
    session = store.new_session(SessionCreate(card_id=card["id"]))
    shelf = {
        **document,
        "description": "新描述",
        "character_book": {"entries": [{"id": "x", "content": "新"}]},
    }
    card = store.edit_card(card["id"], shelf, card["revision"])
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
            diff = (await client.get(f"/api/sessions/{session['id']}/card-sync")).json()
            assert [item["key"] for item in diff["items"]] == ["field:description", "lore:x"]
            request = {
                "keys": ["lore:x"],
                "card_revision": diff["card"]["revision"] - 1,
                "expected_revision": session["revision"],
                "operation_id": "sync",
            }
            response = await client.post(f"/api/sessions/{session['id']}/card-sync", json=request)
            assert response.status_code == 409, response.text
            request["card_revision"] = diff["card"]["revision"]
            response = await client.post(f"/api/sessions/{session['id']}/card-sync", json=request)
            assert response.status_code == 200, response.text
            synced = response.json()["snapshot"]["content"]["document"]
            assert synced["character_book"] == shelf["character_book"]
            assert synced["description"] == "旧描述"
            store.delete_card(card["id"])
            response = await client.get(f"/api/sessions/{session['id']}/card-sync")
            assert response.status_code == 404 and "已删除" in response.json()["detail"]
    finally:
        await app.state.editor.close()
        await app.state.service.close()


def test_worldbook_library_validates_writes_and_card_defaults_follow_their_targets(tmp_path):
    from tavern.core.models import CardDefaultsEdit, PersonaEdit, SessionCreate, WorldbookEdit
    from tavern.storage import worldbooks
    from tavern.storage.store import Conflict, Store

    store = Store(tmp_path)
    book = {"entries": {"0": {"uid": 0, "key": ["钟楼"], "content": "钟楼在北岸。"}}}
    saved = worldbooks.save(store, None, WorldbookEdit(name=" 雾港 ", book=book))
    assert (saved["name"], saved["book"], saved["revision"]) == ("雾港", book, 0)
    for invalid in ({"entries": "x"}, {"entries": [{"extensions": {"terrarium": {"audience": "nobody"}}}]}):
        with pytest.raises(ValueError):
            worldbooks.save(store, saved["id"], WorldbookEdit(name="雾港", book=invalid))
    assert worldbooks.worldbook(store, saved["id"]) == saved
    worldbooks.save(store, saved["id"], WorldbookEdit(name="雾港", book=book))
    with pytest.raises(Conflict):
        worldbooks.save(store, saved["id"], WorldbookEdit(name="雾港", book=book))

    card = store.import_card(json.dumps({"name": "旅人"}).encode(), "card.json")
    persona = store.save_persona(None, PersonaEdit(name="玩家"))
    other = worldbooks.save(store, None, WorldbookEdit(name="航线", book={"entries": []}))
    edit = CardDefaultsEdit(persona_id=persona["id"], worldbook_ids=[other["id"], saved["id"]])
    worldbooks.save_defaults(store, card["id"], edit)
    with pytest.raises(Conflict):
        worldbooks.save_defaults(store, card["id"], edit)
    copied = store.copy_card(card["id"])
    store.delete_persona(persona["id"])
    worldbooks.delete(store, other["id"])
    expected = {"persona_id": None, "worldbook_ids": [saved["id"]]}
    assert worldbooks.card_defaults(store, card["id"]) == {**expected, "version": 1}
    assert worldbooks.card_defaults(store, copied["id"]) == {**expected, "version": 1}
    store.delete_card(card["id"])
    with store.connection() as db:
        assert db.execute("SELECT count(*) FROM card_default_worldbooks").fetchone()[0] == 1

    # A schema 9 database with conversations upgrades after its automatic backup.
    store.new_session(SessionCreate(card_id=copied["id"]))
    with store.connection() as db:
        db.execute("UPDATE meta SET value='9' WHERE key='schema_version'")
    Store(tmp_path)
    assert any(path.name.startswith("before-upgrade-schema9") for path in tmp_path.iterdir())


async def test_new_story_mounts_worldbooks_as_fresh_entries_and_initializes_their_initvar(tmp_path):
    from tavern.application.service import RoleplayRuntime
    from tavern.character.card_sync import sync_items
    from tavern.character.worldbooks import mounted_ids
    from tavern.core.models import SessionCreate, WorldbookEdit
    from tavern.storage import worldbooks
    from tavern.storage.store import Store

    store = Store(tmp_path)
    document = {
        "name": "雾港",
        "first_mes": "开门。",
        "character_book": {"entries": [{"id": "0", "content": "卡片设定"}]},
    }
    card = store.import_card(json.dumps(document).encode(), "card.json")
    book = {
        "entries": {
            "0": {"id": "0", "content": "书库设定", "extensions": {"id": "0"}},
            "1": {"comment": "[InitVar]", "enabled": False, "content": "Money: 7"},
        }
    }
    library = worldbooks.save(store, None, WorldbookEdit(name="雾港设定", book=book))
    service = RoleplayRuntime(store)
    try:
        session = store.new_session(SessionCreate(card_id=card["id"], worldbook_ids=[library["id"]]))
        session = await service.initialize_session(session["id"])
    finally:
        await service.close()
    current = session["snapshot"]["content"]["document"]
    rows = lore_entries(current)
    assert [entry["content"] for entry in rows] == ["卡片设定", "书库设定", "Money: 7"]
    assert rows[0]["id"] == "0" and len({entry["id"] for entry in rows}) == 3
    assert session["snapshot"]["worldbooks"] == [
        {"id": library["id"], "name": "雾港设定", "revision": 0, "entries": [e["id"] for e in rows[1:]]}
    ]
    # The shelf copy stays the sync baseline, and mounted copies are not shelf differences.
    assert session["card_data"]["document"] == document
    assert sync_items(document, current, document, mounted_ids(session["snapshot"])) == []
    assert session["snapshot"]["state"]["stat_data"]["Money"] == 7
    assert worldbooks.worldbook(store, library["id"])["book"] == book


async def test_adjusting_mounts_touches_only_that_books_copies_and_versions_restore_them(tmp_path):
    from tavern.core.models import SessionCreate, WorldbookEdit
    from tavern.storage import worldbooks

    app = create_app(tmp_path)
    store = app.state.store
    document = {"name": "雾港", "character_book": {"entries": [{"id": "card", "content": "卡片设定"}]}}
    card = store.import_card(json.dumps(document).encode(), "card.json")
    first = worldbooks.save(store, None, WorldbookEdit(name="甲", book={"entries": [{"content": "甲一"}]}))
    second = worldbooks.save(store, None, WorldbookEdit(name="乙", book={"entries": [{"content": "乙一"}]}))
    session = store.new_session(SessionCreate(card_id=card["id"], worldbook_ids=[first["id"]]))
    sid = session["id"]

    def contents(value):
        return [entry["content"] for entry in lore_entries(value["snapshot"]["content"]["document"])]

    async def mount(client, ids, reload=(), operation="mount"):
        payload = {
            "worldbook_ids": ids,
            "reload": list(reload),
            "expected_revision": store.session(sid)["revision"],
            "operation_id": operation,
        }
        return await client.put(f"/api/sessions/{sid}/worldbooks", json=payload)

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
            edited = copy.deepcopy(session["snapshot"]["content"]["document"])
            edited["character_book"]["entries"].append({"id": "story", "content": "对话新增"})
            response = await client.put(
                f"/api/sessions/{sid}/lorebook",
                json={"document": edited, "expected_revision": 0, "operation_id": "edit"},
            )
            assert response.status_code == 200, response.text
            assert response.json()["snapshot"]["worldbooks"] == session["snapshot"]["worldbooks"]
            # Library edits reach the story only when the user reloads the book.
            worldbooks.save(
                store, first["id"], WorldbookEdit(name="甲", book={"entries": [{"content": "甲二"}]})
            )
            response = await mount(client, [first["id"], second["id"]], [first["id"]], "reload")
            assert response.status_code == 200, response.text
            assert contents(response.json()) == ["卡片设定", "对话新增", "甲二", "乙一"]
            assert [item["revision"] for item in response.json()["snapshot"]["worldbooks"]] == [1, 0]
            reloaded = response.json()["head"]
            response = await mount(client, [second["id"]], operation="unmount")
            unmounted = response.json()
            assert contents(unmounted) == ["卡片设定", "对话新增", "乙一"]
            assert unmounted["snapshot"]["content"]["reason"] == "卸载「甲」"
            response = await mount(client, [second["id"]], operation="unchanged")
            assert response.status_code == 400 and "没有变化" in response.json()["detail"]
            # Each adjustment is a version of the latest reply; switching restores its mounts.
            latest = unmounted["messages"][-1]
            restored = store.switch_message_version(sid, latest["id"], reloaded, unmounted["revision"])
            assert contents(restored) == ["卡片设定", "对话新增", "甲二", "乙一"]
            assert [item["id"] for item in restored["snapshot"]["worldbooks"]] == [first["id"], second["id"]]
    finally:
        await app.state.editor.close()
        await app.state.service.close()
