"""Explicit UI persona references are durable, complete, and read-only snapshots."""

import copy

from httpx import ASGITransport, AsyncClient

from tavern.application.api import create_app
from tavern.character.card_author import CardAuthorService
from tavern.core.models import PersonaEdit
from tavern.storage.store import Store, dump
from tests.backend.test_card_author import ToolLLM
from tests.backend.test_card_authoring import run


async def test_persona_reference_enters_actual_messages_without_storage_metadata(tmp_path):
    store = Store(tmp_path)
    description = "完整用户角色介绍。" * 1000 + "保留全文末尾"
    persona = store.save_persona(None, PersonaEdit(name="读者", description=description))
    store.save_persona(None, PersonaEdit(name="未选择的私有角色", description="不要泄漏的介绍"))
    editor = CardAuthorService(store)
    try:
        wid = editor.edits.create_conversation()["id"]
        current = editor.edits.attach_persona(wid, persona["id"], 0, persona["version"])
        ref, snapshot = next(iter(current["state"]["persona_attachments"].items()))
        assert snapshot["description"] == description
        assert editor.edits.conversations()[0]["reference_count"] == 1
        assert (
            editor.edits.attach_persona(wid, persona["id"], current["revision"], persona["version"])[
                "revision"
            ]
            == current["revision"]
        )
        model = ToolLLM([])
        result = await run(editor, wid, model, "按引用的用户角色讨论互动，不修改用户角色")
        assert result["status"] == "completed", result["error"]
        outbound = dump(model.call_log)
        assert description in outbound and persona["id"] not in outbound
        assert "未选择的私有角色" not in outbound and "不要泄漏的介绍" not in outbound
        frames = [
            message
            for message in model.call_log[0]
            if "用户主动引用的用户角色完整快照" in str(message.get("content"))
        ]
        assert len(frames) == 1 and '"version"' not in frames[0]["content"]
        assert store.persona(persona["id"]) == persona
        store.delete_persona(persona["id"])
        assert editor.edits.conversation(wid)["state"]["persona_attachments"][ref] == snapshot
        current = editor.edits.conversation(wid)
        editor.edits.detach_persona(wid, ref, current["revision"])
        model = ToolLLM([])
        result = await run(editor, wid, model, "继续讨论")
        assert result["status"] == "completed", result["error"]
        # Detaching replaces the effective reference frame, without rewriting
        # the already-sent prefix or pretending historical input never existed.
        frames = [m for m in model.call_log[0] if "用户主动引用的用户角色完整快照" in m.get("content", "")]
        assert description in frames[0]["content"]
        assert frames[-1]["content"].endswith("[]")
    finally:
        await editor.close()


async def test_persona_reference_api_versions_and_pending_lock(tmp_path):
    app = create_app(tmp_path)
    editor = app.state.editor
    persona = app.state.store.save_persona(None, PersonaEdit(name="玩家", description="原始介绍"))
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
            workspace = (await client.post("/api/card-author/conversations", json={})).json()
            base = "/api/card-author/conversations/" + workspace["id"]
            payload = {
                "persona_id": persona["id"],
                "expected_revision": 0,
                "expected_persona_version": persona["version"],
            }
            assert (
                await client.post(
                    base + "/persona-references", json={**payload, "expected_persona_version": 999}
                )
            ).status_code == 409
            response = await client.post(base + "/persona-references", json=payload)
            assert response.status_code == 200
            current = response.json()
            ref = next(iter(current["state"]["persona_attachments"]))
            assert (await client.delete(base + "/persona-references/" + ref)).status_code == 422
            assert (
                await client.request(
                    "DELETE", base + "/persona-references/" + ref, json={"expected_revision": 0}
                )
            ).status_code == 409
            editor.edits.save_progress(
                workspace["id"],
                "test-pending",
                {
                    "working": copy.deepcopy({"cards": current["cards"], "state": current["state"]}),
                    "calls": 0,
                },
            )
            assert (
                await client.request(
                    "DELETE",
                    base + "/persona-references/" + ref,
                    json={"expected_revision": current["revision"]},
                )
            ).status_code == 409
            assert (
                await client.post(
                    base + "/persona-references", json={**payload, "expected_revision": current["revision"]}
                )
            ).status_code == 409
            await client.request(
                "DELETE", base + "/progress", json={"expected_revision": current["revision"]}
            )
            current = (await client.get(base)).json()
            response = await client.request(
                "DELETE", base + "/persona-references/" + ref, json={"expected_revision": current["revision"]}
            )
            assert response.status_code == 200 and response.json()["state"]["persona_attachments"] == {}
            assert app.state.store.persona(persona["id"]) == persona
    finally:
        await editor.close()
        await app.state.service.close()
