import copy
import hashlib
import io
import json
import sqlite3
import zipfile
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest
from kohakuterrarium.testing.llm import ScriptedLLM

from tavern.application.api import create_app
from tavern.core.models import PersonaEdit, SessionCreate, TurnRequest
from tavern.memories.memory import fingerprint, window_valid
from tavern.storage.store import Conflict, Store, dump, uid
from tavern.storage.transfer import ExportSelection, export_package, import_package, read_package
from tests.backend.test_full_agent import Model


def seed(store, mode="half_agent", **fields):
    card = store.import_card(
        json.dumps({"name": "旅人", "first_mes": "故事开始。", **fields}).encode(), "card.json"
    )
    persona = store.save_persona(None, PersonaEdit(name="玩家", description="寻找钟楼"))
    session = store.new_session(SessionCreate(card_id=card["id"], persona_id=persona["id"], agent_mode=mode))
    return card, persona, session


def package(store, **options):
    return read_package(export_package(store, ExportSelection(**options)), store)


@pytest.mark.parametrize("mode", ["half_agent", "full_agent"])
async def test_imported_story_commits_next_round_with_pinned_resources_and_context(tmp_path, mode):
    source_app = create_app(tmp_path / "source")
    source, engine = source_app.state.store, source_app.state.service
    target_app = create_app(tmp_path / "target")
    target, target_engine = target_app.state.store, target_app.state.service
    card, persona, session = seed(
        source,
        mode,
        first_mes="原开场。<initvar>hp: 100\nold: true</initvar>",
        extensions={
            "regex_scripts": [
                {
                    "findRegex": r"/<initvar>[\s\S]*?<\/initvar>/g",
                    "replaceString": "",
                    "placement": [2],
                    "promptOnly": True,
                    "markdownOnly": True,
                }
            ]
        },
    )
    model = (
        Model(
            ("read_history", {"ref": "H2"}),
            ("write_note", {"key": "约定", "content": "一起去钟楼", "kind": "fact", "sources": ["H2"]}),
            "她点头。",
        )
        if mode == "full_agent"
        else ScriptedLLM(["她点头。"])
    )
    engine.provider_factory = lambda: model
    try:
        session = await engine.initialize_session(session["id"])
        assert session["snapshot"]["state"]["stat_data"]["old"] is True
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=source_app), base_url="http://testserver"
        ) as api:
            # Repeated opening edits must replace initialized variables, and the actual
            # next request/export must consume the same text that the user saved.
            response = await api.put(
                f"/api/sessions/{session['id']}/opening",
                json={"text": "修改后的开场。<initvar>hp: 80</initvar>", "expected_revision": 0},
            )
            assert response.status_code == 200, response.text
            session = response.json()
            response = await api.put(
                f"/api/sessions/{session['id']}/opening",
                json={
                    "text": "最终开场。<initvar>hp: 60</initvar>",
                    "expected_revision": session["revision"],
                },
            )
            assert response.status_code == 200, response.text
            session = response.json()
        assert session["snapshot"]["state"]["stat_data"] == {"hp": 60}
        assert session["messages"][0]["model"] == session["messages"][0]["display"] == "最终开场。"
        assert session["opening_editable"] and not session["turns"]
        assert source.card(card["id"])["document"] == card["document"]
        turn = engine.submit(
            session["id"],
            TurnRequest(text="一起去钟楼", operation_id="source", expected_revision=session["revision"]),
        )
        await engine.tasks[turn["id"]]
        assert source.turn(turn["id"])["status"] == "committed"
        wire = dump(model.requests[0]["messages"] if mode == "full_agent" else model.last_messages)
        assert "最终开场。" in wire and "原开场。" not in wire and "修改后的开场。" not in wire
        auxiliary = [{"kind": "thinking", "text": "生成记录 " + session["id"]}]
        with source.connection() as db:
            db.execute(
                "UPDATE messages SET auxiliary=? WHERE id=(SELECT head FROM sessions WHERE id=?)",
                (dump(auxiliary), session["id"]),
            )
        current = source.session(session["id"])
        # A deleted library card remains playable from its session-owned resources.
        source.delete_card(card["id"])
        payload, blobs = package(source, settings=False, sessions=[session["id"]])
        imported = import_package(target, payload, blobs)
        restored = target.session(imported["sessions"][0])
        assert restored["agent_mode"] == mode
        assert restored["persona_data"]["description"] == persona["description"]
        assert [m["raw"] for m in restored["messages"]] == [m["raw"] for m in current["messages"]]
        assert restored["messages"][-1]["auxiliary"] == auxiliary
        assert payload["version"] == 6
        assert restored["opening_locked"] and not restored["opening_editable"]
        assert not set(m["id"] for m in restored["messages"]) & set(m["id"] for m in current["messages"])
        if mode == "full_agent":
            from tavern.full_agent.state import note_view

            note = restored["snapshot"]["full_agent"]["notes"]["约定"]
            assert note_view(note, restored["messages"])["sources"] == ["H2"]
            assert note["sources"][0]["message_id"] == restored["messages"][1]["id"]
        assert target.cards() == []
        assert (target.root / "blobs" / restored["card_data"]["original"]).exists()
        table = "full_agent_trace" if mode == "full_agent" else "half_agent_trace"
        with target.connection() as db:
            assert db.execute(f"SELECT count(*) FROM {table} WHERE session=?", (restored["id"],)).fetchone()[
                0
            ]
        next_model = Model("钟楼到了。") if mode == "full_agent" else ScriptedLLM(["钟楼到了。"])
        target_engine.provider_factory = lambda: next_model
        turn = target_engine.submit(
            restored["id"], TurnRequest(text="继续走", operation_id="after-import", expected_revision=0)
        )
        await target_engine.tasks[turn["id"]]
        assert target.turn(turn["id"])["status"] == "committed", target.turn(turn["id"])["error"]
        assert target.session(restored["id"])["messages"][-1]["raw"] == "钟楼到了。"
    finally:
        await engine.close()
        await target_engine.close()


def test_all_branches_sources_cache_and_window_fingerprints_are_remapped(tmp_path):
    store = Store(tmp_path)
    card, persona, session = seed(store)
    first = session["messages"][0]
    other = uid()
    snapshot = copy.deepcopy(first["snapshot"])
    snapshot["state"] = {"hp": 75}
    snapshot["memory_document"]["records"] = [
        {"id": "memory", "sources": [{"message_id": first["id"], "hash": "text"}]}
    ]
    snapshot["memory_archive"] = {"covered_count": 1, "fingerprint": fingerprint([first])}
    snapshot["lore_injections"] = [{"anchor_id": first["id"], "source_ids": [first["id"]]}]
    with store.connection() as db:
        db.execute("UPDATE messages SET snapshot=? WHERE id=?", (dump(snapshot), first["id"]))
        db.execute(
            "INSERT INTO messages(id,session,parent,role,raw,model,display,snapshot,created) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                other,
                session["id"],
                first["id"],
                "assistant",
                "另一条分支",
                "另一条分支",
                "另一条分支",
                dump(snapshot),
                100,
            ),
        )
        db.execute(
            "UPDATE sessions SET cache_breakpoint_message=?,cache_breakpoint_auto=0 WHERE id=?",
            (first["id"], session["id"]),
        )
    payload, blobs = package(
        store, settings=False, personas=[persona["id"]], cards=[card["id"]], sessions=[session["id"]]
    )
    imported = import_package(store, payload, blobs)
    restored = store.session(imported["sessions"][0])
    message = restored["messages"][0]
    assert restored["head"] == message["id"] != first["id"]
    assert restored["persona_data"]["id"] == imported["personas"][0]
    assert restored["card"] == imported["cards"][0]
    assert restored["cache_breakpoint_message"] == message["id"]
    assert message["snapshot"]["state"] == {"hp": 75}
    assert message["auxiliary"] == []
    assert message["snapshot"]["memory_document"]["records"][0]["sources"][0]["message_id"] == message["id"]
    assert message["snapshot"]["lore_injections"][0]["anchor_id"] == message["id"]
    assert window_valid(message["snapshot"]["memory_archive"], restored["messages"])
    with store.connection() as db:
        assert (
            db.execute("SELECT count(*) FROM messages WHERE session=?", (restored["id"],)).fetchone()[0] == 2
        )
    assert len(store.sessions()) == 2
    assert store.session(session["id"])["head"] == first["id"]


def test_embedded_assets_and_cached_dependencies_survive_offline_import(tmp_path):
    source = Store(tmp_path / "source")
    card, _, _ = seed(source)
    image = source.blob(b"embedded-image")
    script = source.blob(b"export default 'cached-script';")
    stylesheet = source.blob(b"body { color: lavender; }")
    dependencies = {
        "scripts": {"source-hash": script},
        "resources": {
            "https://assets.example/style.css": {
                "hash": stylesheet,
                "url": "https://assets.example/style.css",
                "content_type": "text/css",
            }
        },
    }
    with source.connection() as db:
        db.execute(
            "UPDATE cards SET resources=?,permission=1 WHERE id=?",
            (dump({"assets/portrait.png": image}), card["id"]),
        )
    (source.root / "dependencies").mkdir()
    source.cache_card_dependencies(card["id"], dependencies)
    session = source.new_session(SessionCreate(card_id=card["id"]))
    payload, blobs = package(source, settings=False, cards=[card["id"]], sessions=[session["id"]])
    target = Store(tmp_path / "target")
    imported = import_package(target, payload, blobs)
    for digest in (image, script, stylesheet):
        assert (target.root / "blobs" / digest).read_bytes() == (source.root / "blobs" / digest).read_bytes()
    restored_card = target.card(imported["cards"][0])
    restored_session = target.session_card(imported["sessions"][0])
    assert target.card_manifest(restored_card) == target.card_manifest(restored_session) == dependencies
    assert restored_card["permission"] == restored_session["permission"] == 1


def test_resource_hash_failure_is_reported_before_writing(tmp_path):
    source = Store(tmp_path / "source")
    card, _, _ = seed(source)
    content = export_package(source, ExportSelection(settings=False, cards=[card["id"]]))
    output = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(content)) as archive, zipfile.ZipFile(output, "w") as corrupt:
        for name in archive.namelist():
            corrupt.writestr(name, b"corrupted" if name.startswith("blobs/") else archive.read(name))
    with pytest.raises(ValueError, match="校验失败"):
        read_package(output.getvalue(), source)


def test_settings_secrets_and_browser_preferences_are_explicit_and_revision_checked(tmp_path):
    store = Store(tmp_path)
    settings = store.settings_document()
    settings.shared.providers[0].api_key = "source-secret"
    settings.half_agent.system_prompt = "个人提示词"
    store.save_settings(settings)
    payload, blobs = package(
        store, browser_preferences={"layout": {"fontSize": 20, "fontFamily": "serif", "glass": False}}
    )
    assert "source-secret" not in dump(payload)
    assert payload["settings"]["browser"]["layout"]["fontSize"] == 20
    assert payload["settings"]["browser"]["layout"]["glass"] is False
    with pytest.raises(ValueError, match="布局偏好"):
        package(store, browser_preferences={"layout": {"glass": "off"}})
    target = Store(tmp_path / "target")
    local = target.settings_document()
    local.shared.providers[0].api_key = "local-secret"
    target.save_settings(local)
    imported = import_package(target, payload, blobs)
    assert not imported["settings_restored"]
    assert target.settings_document().half_agent.system_prompt != "个人提示词"
    revisions = target.settings_document().revisions
    card = target.import_card(b'{"name":"running consultation"}', "card.json")
    session = target.new_session(SessionCreate(card_id=card["id"]))
    before = target.settings_document()
    with target.connection() as db:
        db.execute(
            "INSERT INTO advisor_runs(id,session,operation,request,status,created) VALUES (?,?,?,'{}','running',0)",
            ("active-advisor", session["id"], "active-advisor"),
        )
    with pytest.raises(Conflict, match="运行中的任务"):
        import_package(target, payload, blobs, True, revisions)
    assert target.settings_document() == before
    with target.connection() as db:
        db.execute("UPDATE advisor_runs SET status='cancelled' WHERE id='active-advisor'")
    imported = import_package(target, payload, blobs, True, revisions)
    assert imported["settings_restored"]
    assert target.settings_document().half_agent.system_prompt == "个人提示词"
    assert target.settings_document().shared.providers[0].api_key == "local-secret"
    assert target.settings_document().revisions["shared"] == revisions["shared"] + 1
    with pytest.raises(Conflict):
        import_package(target, payload, blobs, True, revisions)
    with_keys, blobs = package(store, include_api_keys=True)
    import_package(target, with_keys, blobs, True, target.settings_document().revisions)
    assert target.settings_document().shared.providers[0].api_key == "source-secret"
    payload["settings"]["document"]["shared"]["providers"][0]["base_url"] = "https://different.example"
    import_package(target, payload, blobs, True, target.settings_document().revisions)
    assert target.settings_document().shared.providers[0].api_key == ""


def test_invalid_graph_and_failed_write_leave_no_partial_import(tmp_path, monkeypatch):
    store = Store(tmp_path / "source")
    card, persona, session = seed(store)
    payload, blobs = package(
        store, settings=False, personas=[persona["id"]], cards=[card["id"]], sessions=[session["id"]]
    )
    target = Store(tmp_path / "target")
    broken = copy.deepcopy(payload)
    broken["sessions"][0]["messages"][0]["parent"] = session["head"]
    with pytest.raises(ValueError, match="分支无效"):
        import_package(target, broken, blobs)
    from tavern.storage import transfer

    insert = transfer.insert

    def fail(db, table, row):
        if table == "sessions":
            raise OSError("disk full")
        insert(db, table, row)

    monkeypatch.setattr(transfer, "insert", fail)
    with pytest.raises(OSError):
        import_package(target, payload, blobs)
    assert target.cards() == target.personas() == target.sessions() == []
    assert not list((target.root / "blobs").iterdir())
    assert not list((target.root / "dependencies").glob("*.json"))


def test_failed_file_write_cleans_up_before_another_import_can_reuse_blobs(tmp_path, monkeypatch):
    store = Store(tmp_path)
    card = store.import_card(b'{"name":"fixture"}', "card.json")
    payload, blobs = package(store, settings=False, cards=[card["id"]])
    store.delete_card(card["id"])
    asset = b"new imported asset"
    digest = hashlib.sha256(asset).hexdigest()
    payload["cards"][0]["resources"]["asset.txt"] = digest
    blobs[digest] = asset
    write_bytes = Path.write_bytes
    failed = False

    def incomplete_write(path, data):
        nonlocal failed
        if data == asset and not failed:
            failed = True
            write_bytes(path, data[:4])
            raise OSError("disk full")
        return write_bytes(path, data)

    connection = store.connection
    imported = {}

    @contextmanager
    def next_import_after_rollback():
        try:
            with connection() as db:
                yield db
        except OSError:
            # Deterministically give the next importer the released write lock.
            # The failed attempt must have removed its files before this point.
            imported.update(import_package(store, payload, blobs))
            raise

    monkeypatch.setattr(Path, "write_bytes", incomplete_write)
    monkeypatch.setattr(store, "connection", next_import_after_rollback)
    with pytest.raises(OSError, match="disk full"):
        import_package(store, payload, blobs)
    assert [item["id"] for item in store.cards()] == imported["cards"]
    assert {path.name for path in (store.root / "blobs").iterdir()} == set(blobs)
    for digest, content in blobs.items():
        assert (store.root / "blobs" / digest).read_bytes() == content

    # SQLITE_FULL/IOERR can release the transaction before raising. Another
    # import may already own the published blob when control returns to Python.
    from tavern.storage import transfer

    second_payload = copy.deepcopy(payload)
    original = b'{"name":"second fixture"}'
    original_hash = hashlib.sha256(original).hexdigest()
    second_payload["cards"][0]["original"] = original_hash
    second_blobs = {**blobs, original_hash: original}
    insert = transfer.insert
    interrupted = False

    def fail_after_automatic_rollback(db, table, row):
        nonlocal interrupted
        if table == "cards" and not interrupted:
            interrupted = True
            db.rollback()
            imported.update(import_package(store, second_payload, second_blobs))
            raise sqlite3.OperationalError("disk I/O error")
        insert(db, table, row)

    monkeypatch.setattr(transfer, "insert", fail_after_automatic_rollback)
    with pytest.raises(sqlite3.OperationalError, match="disk I/O error"):
        import_package(store, second_payload, second_blobs)
    assert store.card(imported["cards"][0])["original"] == original_hash
    assert (store.root / "blobs" / original_hash).read_bytes() == original


@pytest.mark.parametrize("path", ["../escape", "blobs/not-a-hash", "dataset.json/extra"])
def test_archive_paths_and_corruption_are_rejected(path):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("dataset.json", "{}")
        archive.writestr(path, "bad")
    with pytest.raises(ValueError, match="路径"):
        read_package(output.getvalue())
    with pytest.raises(ValueError):
        read_package(b"not zip")


async def test_api_preview_download_and_import(tmp_path):
    app = create_app(tmp_path)
    card, persona, session = seed(app.state.store)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            assert (await client.get("/api/data-transfer")).json()["sessions"][0]["id"] == session["id"]
            response = await client.post(
                "/api/data-transfer/export", json={"settings": False, "sessions": [session["id"]]}
            )
            assert response.status_code == 200
            assert "attachment" in response.headers["content-disposition"]
            file = {"file": ("data.zip", response.content, "application/zip")}
            preview = (await client.post("/api/data-transfer/preview", files=file)).json()
            assert not preview["settings"]
            assert preview["sessions"][0]["messages"] == 1
            assert preview["sha256"] == hashlib.sha256(response.content).hexdigest()
            bad = await client.post(
                "/api/data-transfer/import", files=file, data={"options": json.dumps({"sha256": "wrong"})}
            )
            assert bad.status_code == 400
            imported = await client.post(
                "/api/data-transfer/import",
                files=file,
                data={"options": json.dumps({"sha256": preview["sha256"]})},
            )
            assert imported.status_code == 200, imported.text
            assert len(app.state.store.sessions()) == 2
            assert len(app.state.store.personas()) == 1
            turn, _ = app.state.store.create_turn(
                session["id"], TurnRequest(text="pending", operation_id="busy", expected_revision=0)
            )
            blocked = await client.post(
                "/api/data-transfer/export", json={"settings": False, "sessions": [session["id"]]}
            )
            assert blocked.status_code == 409
    finally:
        await app.state.service.close()


def test_worldbooks_card_defaults_and_mount_records_are_remapped_together(tmp_path):
    from tavern.core.models import CardDefaultsEdit, WorldbookEdit
    from tavern.storage import worldbooks

    source = Store(tmp_path / "source")
    card, persona, _ = seed(source)
    packed = worldbooks.save(
        source, None, WorldbookEdit(name="雾港", book={"entries": [{"content": "钟楼"}]})
    )
    # An edited book (revision 1) must keep its revision so imported mounts stay current.
    packed = worldbooks.save(
        source, packed["id"], WorldbookEdit(name="雾港", book={"entries": {"0": {"content": "钟楼"}}})
    )
    left = worldbooks.save(source, None, WorldbookEdit(name="未导出", book={"entries": []}))
    edit = CardDefaultsEdit(persona_id=persona["id"], worldbook_ids=[left["id"], packed["id"]])
    worldbooks.save_defaults(source, card["id"], edit)
    session = source.new_session(SessionCreate(card_id=card["id"], worldbook_ids=[packed["id"]]))
    payload, blobs = package(
        source, settings=False, worldbooks=[packed["id"]], cards=[card["id"]], sessions=[session["id"]]
    )
    # Defaults travel only with the personas and books packed beside them.
    assert payload["cards"][0]["defaults"] == {"persona_id": None, "worldbook_ids": [packed["id"]]}
    target = Store(tmp_path / "target")
    imported = import_package(target, payload, blobs)
    [book_id] = imported["worldbooks"]
    assert book_id != packed["id"] and worldbooks.worldbook(target, book_id)["book"] == packed["book"]
    assert worldbooks.cards_defaults(target, imported["cards"]) == {
        imported["cards"][0]: {"persona_id": None, "worldbook_ids": [book_id], "version": 1}
    }
    [mount] = target.session(imported["sessions"][0])["snapshot"]["worldbooks"]
    assert mount == {**session["snapshot"]["worldbooks"][0], "id": book_id}
    [summary] = worldbooks.summaries(target)
    assert summary["revision"] == mount["revision"] == 1 and summary["entries"] == 1

    # Packages from before the library still import.
    legacy = copy.deepcopy(payload)
    legacy["version"] = 3
    legacy.pop("worldbooks")
    legacy["cards"][0].pop("defaults")
    imported = import_package(target, legacy, blobs)
    assert imported["worldbooks"] == [] and len(imported["sessions"]) == 1
