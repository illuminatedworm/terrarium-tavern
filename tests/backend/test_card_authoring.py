"""Behavioral authoring contracts, using isolated data and local model fixtures."""
import asyncio
import copy
import json

import pytest
from httpx import ASGITransport, AsyncClient
from kohakuterrarium.llm.base import NativeToolCall

from tavern.application.api import create_app
from tavern.character.card_editor import CardEditorService
from tavern.character.editor_sandbox import test_card as probe_card
from tavern.character.editor_tools import EditorTools, tool_definitions
from tavern.core.models import CardEditorRequest, SessionCreate
from tavern.storage.backup import backup, restore
from tavern.storage.store import Conflict, Store, dump, uid
from tests.backend.test_card_editor import ToolLLM, import_fixture


async def run(editor, wid, provider, text="修改已引用卡片", resume=None):
    editor.provider_factory = lambda: provider
    workspace = editor.edits.conversation(wid)
    result = editor.submit(wid, CardEditorRequest(text=text, expected_revision=workspace["revision"], operation_id=uid(), resume_run_id=resume), legacy=False)
    await editor.tasks[result["id"]]
    return editor.edits.run(result["id"])


def patch(ref, text):
    return ("patch_draft", {"card_ref": ref, "operations": [{"op": "replace", "path": "/description", "value": text}], "comment": "用户要求"})


class Batches(ToolLLM):
    async def chat(self, messages, **kwargs):
        self.call_log.append(messages)
        self.schemas.append(kwargs.get("tools", []))
        index = self.call_count
        self.call_count += 1
        if index < len(self.steps):
            self.native = [NativeToolCall(id=f"batch{index}-{i}", name=n, arguments=dump(a)) for i, (n, a) in enumerate(self.steps[index])]
            yield "正在处理"
        else:
            self.native = []
            yield "草稿已更新，等待审阅。"


def test_schemas_are_explicit_and_use_one_catalog():
    definitions = tool_definitions()
    assert len(definitions) == 24
    for tool in definitions:
        schema = tool["function"]["parameters"]
        assert schema["additionalProperties"] is False
        assert all(value.get("description") for value in schema["properties"].values())
    patch_schema = next(t["function"]["parameters"] for t in definitions if t["function"]["name"] == "patch_draft")
    assert "card_ref" in patch_schema["required"]
    assert patch_schema["properties"]["operations"]["items"]["$ref"]
    assert set(patch_schema["$defs"]["Operation"]["properties"]["op"]["enum"]) == {"add", "remove", "replace", "move", "copy", "test"}


@pytest.mark.parametrize("name", ["patch_draft", "revert_draft"])
async def test_comment_argument_recovers_from_old_name_in_editor_loop(tmp_path, name):
    store = Store(tmp_path)
    card = import_fixture(store)
    editor = CardEditorService(store)
    workspace = editor.edits.create(card["id"])
    operations = [{"op": "replace", "path": "/description", "value": "新的角色描述"}]
    steps = [("read_field", {"card_ref": "C1", "path": "/description"})]
    arguments = {"card_ref": "C1"}
    if name == "revert_draft":
        steps.append(("patch_draft", {"card_ref": "C1", "operations": operations, "comment": "临时修改"}))
    else:
        arguments["operations"] = operations
    comment = "简短操作备注"
    provider = ToolLLM([*steps, (name, {**arguments, "reason": "旧参数备注"}),
                       (name, {**arguments, "comment": comment})])
    try:
        result = await run(editor, workspace["id"], provider)
        assert result["status"] == "completed", result["error"]
        schema = next(tool.parameters for tool in provider.schemas[0] if tool.name == name)
        assert "comment" in schema["properties"] and "comment" in schema["required"]
        assert "reason" not in schema["properties"]
        events = editor.edits.activity(result["id"])["events"]
        finished = [event["payload"] for event in events
                    if event["kind"] == "tool_finished" and event["payload"]["tool"] == name]
        assert [event["status"] for event in finished[-2:]] == ["error", "completed"]
        assert "comment" in finished[-2]["detail"] and "reason" in finished[-2]["detail"]
        starts = [event["payload"] for event in events
                  if event["kind"] == "tool_started" and event["payload"]["tool"] == name]
        assert json.loads(starts[-2]["arguments"])["reason"] == "旧参数备注"
        assert json.loads(starts[-1]["arguments"])["comment"] == comment
        current = editor.edits.conversation(workspace["id"])["cards"][0]["document"]
        assert current["description"] == ("原人物设定" if name == "revert_draft" else "新的角色描述")
        assert store.card(card["id"])["document"]["description"] == "原人物设定"
        if name == "patch_draft":
            change = next(event for event in editor.edits.events(result["id"])
                          if event["kind"] == "draft_change")
            assert change["payload"]["reason"] == comment
    finally:
        await editor.close()


async def test_empty_consultation_then_confirmed_creation_survives_rollover(tmp_path):
    store = Store(tmp_path)
    editor = CardEditorService(store)
    workspace = editor.edits.create_conversation()
    wid = workspace["id"]
    try:
        provider = ToolLLM([])
        first = await run(editor, wid, provider, "先给我书店店主的方案，不要制作")
        assert first["status"] == "completed"
        assert editor.edits.conversation(wid)["cards"] == []
        proposal = editor.edits.messages(wid)[-1]
        made = await run(editor, wid, ToolLLM([
            ("create_draft", {"name": "林间", "proposal_ref": proposal["ref"], "confirmation_ref": "H3"}),
            ("patch_draft", {"card_ref": "C1", "operations": [
                {"op": "replace", "path": "/data/description", "value": "经营雨夜书店的店主"},
                {"op": "replace", "path": "/data/first_mes", "value": "铜铃轻响，他递来毛巾。"},
                {"op": "replace", "path": "/data/mes_example", "value": "{{char}}：雨总会停的。"}], "comment": "按确认方案制作"}),
            ("new_context", {"content": "林间已创建，等待审阅；保留已确认方案。"}),
        ]), "确认，按方案制作")
        assert made["status"] == "completed", made["error"]
        workspace = editor.edits.conversation(wid)
        card = workspace["cards"][0]
        assert card["document"]["spec"] == "chara_card_v2"
        assert proposal["content"] in card["brief"] and "确认，按方案制作" in card["brief"]
        assert workspace["state"]["window"] == 1
        assert not store.cards()
        record = editor.edits.context(wid)
        assert proposal["content"] in dump(record["messages"])
        saved = editor.edits.save_card(wid, "C1", workspace["revision"], "create", "save-once")
        assert saved["name"] == "林间"
        assert editor.edits.save_card(wid, "C1", workspace["revision"], "create", "save-once")["id"] == saved["id"]
        assert store.new_session(SessionCreate(card_id=saved["id"]))["card"] == saved["id"]
    finally:
        await editor.close()


async def test_reference_promotion_multi_card_and_invalid_target(tmp_path):
    store, editor = Store(tmp_path), None
    editor = CardEditorService(store)
    one, two = import_fixture(store), import_fixture(store)
    wid = editor.edits.create_conversation()["id"]
    editor.edits.attach(wid, one["id"], 0)
    editor.edits.attach(wid, two["id"], 1)
    provider = ToolLLM([("read_field", {"card_ref": "C1", "path": "/description"}), patch("C1", "只修改 A"), patch("C99", "错误目标")])
    try:
        result = await run(editor, wid, provider)
        assert result["status"] == "completed"
        current = editor.edits.conversation(wid)
        assert current["cards"][0]["kind"] == "draft"
        assert current["cards"][0]["document"]["description"] == "只修改 A"
        assert current["cards"][1]["document"] == two["document"]
        again = editor.edits.attach(wid, one["id"], current["revision"])
        assert [c["ref"] for c in again["cards"]] == ["C1", "C2"]
        assert again["cards"][0]["document"]["description"] == "只修改 A"
        assert store.card(one["id"])["document"] == one["document"]
        assert "卡片引用不存在" in dump(provider.call_log[-1])
        with pytest.raises(Conflict, match="明确"):
            editor.edits.get(wid)
    finally:
        await editor.close()


async def test_interrupt_and_steer_abort_unexecuted_calls_without_repeating_writes(tmp_path, monkeypatch):
    store = Store(tmp_path)
    editor = CardEditorService(store)
    wid = editor.edits.create_conversation()["id"]
    for i in range(3):
        editor.edits.attach(wid, import_fixture(store)["id"], i)
    entered = asyncio.Event()
    calls = []
    original = EditorTools.patch_draft

    async def hold(self, args):
        if args.card_ref == "C3" and not calls.count("resuming"):
            entered.set()
            await asyncio.Event().wait()
        calls.append(args.card_ref)
        return await original(self, args)

    monkeypatch.setattr(EditorTools, "patch_draft", hold)
    provider = Batches([[("read_field", {"card_ref": f"C{i}"}) for i in range(1, 4)],
                        [patch("C1", "A 已改"), patch("C2", "B 不想要"), patch("C3", "旧的 C")]])
    editor.provider_factory = lambda: provider
    try:
        active = editor.submit(wid, CardEditorRequest(text="修改三张卡", expected_revision=3, operation_id="start"), legacy=False)
        await asyncio.wait_for(entered.wait(), 10)
        await editor.cancel(active["id"])
        stopped = editor.edits.conversation(wid)
        assert stopped["cards"][0]["document"]["description"] == "原人物设定"
        assert stopped["pending"]["cards"][0]["document"]["description"] == "A 已改"
        with pytest.raises(Conflict, match="未完成"):
            editor.edits.save_card(wid, "C1", 3, "overwrite", "blocked")
        calls.append("resuming")
        followup = ToolLLM([('revert_draft', {'card_ref': 'C2', 'comment': '用户要求恢复 B'}), patch('C3', '新的 C')])
        completed = await run(editor, wid, followup, "保留 A，B 恢复本轮之前的版本，C 按新要求改", resume=active["id"])
        assert completed["status"] == "completed", completed["error"]
        current = editor.edits.conversation(wid)
        assert [c["document"]["description"] for c in current["cards"]] == ["A 已改", "原人物设定", "新的 C"]
        assert calls.count("C1") == 1 and calls.count("C3") == 1
        assert current["pending"] is None
        first = followup.call_log[0]
        cancelled_index = next(i for i, m in enumerate(first) if m.get("role") == "tool" and '"cancelled": true' in m.get("content", ""))
        assert first[cancelled_index]["_tool_error"] is True
        steering_index = next(i for i, m in enumerate(first) if m.get("role") == "user" and "保留 A" in m.get("content", ""))
        assert cancelled_index < steering_index
    finally:
        await editor.close()


async def test_long_source_reaches_followup_and_strict_patch_recovers(tmp_path):
    store = Store(tmp_path)
    settings = store.settings_document()
    settings.shared.card_editor.context_tokens = 550000
    store.save_settings(settings)
    document = {"name": "长卡", "description": "x" * 300000 + "TAIL_849301", "first_mes": "开场"}
    card = store.import_card(dump(document).encode(), "card.json")
    editor = CardEditorService(store)
    workspace = editor.edits.create(card["id"])
    provider = ToolLLM([("read_field", {"card_ref": "C1", "path": "/description"}),
        ("patch_draft", {"card_ref": "C1", "comment": "错误参数", "operations": [{"op": "replace", "path": "/description"}]}),
        patch("C1", "正确结果")])
    try:
        result = await run(editor, workspace["id"], provider)
        assert result["status"] == "completed", result["error"]
        assert "TAIL_849301" in dump(provider.call_log[1])
        assert "tool output truncated" not in dump(provider.call_log)
        assert "必须提供 value" in dump(provider.call_log[2])
        assert editor.edits.conversation(workspace["id"])["cards"][0]["document"]["description"] == "正确结果"
    finally:
        await editor.close()


async def test_pending_progress_backup_resume_and_discard(tmp_path):
    store = Store(tmp_path / "source")
    editor = CardEditorService(store)
    workspace = editor.edits.create(import_fixture(store)["id"])

    class Failure(ToolLLM):
        async def chat(self, messages, **kwargs):
            if self.call_count == 2:
                raise ValueError("offline transport failure")
            async for chunk in super().chat(messages, **kwargs):
                yield chunk

    try:
        failed = await run(editor, workspace["id"], Failure([("read_field", {"card_ref": "C1"}), patch("C1", "保留的进度")]))
        assert failed["status"] == "error"
        backup(store, tmp_path / "backup.zip")
        recovered = CardEditorService(restore(tmp_path / "backup.zip", tmp_path / "restore"))
        try:
            result = await run(recovered, workspace["id"], ToolLLM([]), "", resume=failed["id"])
            assert result["status"] == "completed"
            assert recovered.edits.get(workspace["id"])["document"]["description"] == "保留的进度"
        finally:
            await recovered.close()
        editor.edits.discard(workspace["id"], 0)
        assert editor.edits.get(workspace["id"])["document"] == workspace["document"]
    finally:
        await editor.close()


async def test_api_empty_preview_reference_snapshot_and_revision(tmp_path):
    app = create_app(tmp_path)
    editor = app.state.editor
    editor.provider_factory = lambda: ToolLLM([])
    card = import_fixture(app.state.store)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
            created = (await client.post("/api/card-editor/conversations", json={})).json()
            base = "/api/card-editor/conversations/" + created["id"]
            preview = (await client.post(base + "/preview", json={"text": "先讨论"})).json()
            assert preview["preview_only"] and len(preview["tools"]) == 24
            assert editor.edits.messages(created["id"]) == []
            attached = (await client.post(base + "/references", json={"card_id": card["id"], "expected_revision": 0})).json()
            assert attached["cards"][0]["kind"] == "reference"
            assert (await client.put(base, json={"title": "过时写入", "expected_revision": 0})).status_code == 409
            doc = copy.deepcopy(card["document"])
            doc["description"] = "书架新版本"
            app.state.store.edit_card(card["id"], doc, 0)
            current = (await client.get(base)).json()
            assert current["cards"][0]["source_changed"]
            assert current["cards"][0]["document"] == card["document"]
    finally:
        await editor.close()
        await app.state.service.close()


async def test_optional_compatibility_target_can_be_preserved_cleared_and_undone(tmp_path):
    app = create_app(tmp_path)
    editor = app.state.editor
    card = import_fixture(app.state.store)
    workspace = editor.edits.create_conversation()
    workspace = editor.edits.attach(workspace["id"], card["id"], workspace["revision"])
    base = f"/api/card-editor/conversations/{workspace['id']}/drafts/C1"
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
            for fields, expected in (({"target_mode": "full_agent"}, "full_agent"), ({}, "full_agent"),
                                     ({"target_mode": None}, None)):
                response = await client.put(base, json={"document": card["document"],
                    "expected_revision": workspace["revision"], **fields})
                assert response.status_code == 200, response.text
                workspace = response.json()
                assert workspace["cards"][0]["target_mode"] == expected
                assert workspace["cards"][0]["document"] == card["document"]
            undone = await client.post(base + "/undo", json={"expected_revision": workspace["revision"]})
            assert undone.status_code == 200
            assert undone.json()["cards"][0]["target_mode"] == "full_agent"
    finally:
        await editor.close()
        await app.state.service.close()


async def test_local_sandbox_reports_observed_initialization_and_errors(tmp_path):
    store = Store(tmp_path)
    item = {"ref": "C1", "card": None, "source": {}, "document": {
        "name": "本地验证", "first_mes": "<initvar>hp: 8</initvar>", "extensions": {}}}
    passed = await probe_card(store, item, samples=["正文样例"])
    assert passed["status"] == "passed", passed
    assert passed["checks"][0]["state"]["stat_data"]["hp"] == 8
    assert passed["model_calls"] == 0 and not store.sessions()
    item["document"]["extensions"]["tavern_helper"] = {"scripts": [{"name": "bad", "content": "throw new Error('PROBE_EXPECTED_ERROR')"}]}
    failed = await probe_card(store, item)
    assert failed["status"] == "failed", failed
    assert "PROBE_EXPECTED_ERROR" in dump(failed)


async def test_inline_initialization_update_preserves_narrative(tmp_path):
    store = Store(tmp_path)
    editor = CardEditorService(store)
    source = import_fixture(store)
    workspace = editor.edits.create(source["id"])
    try:
        result = await run(editor, workspace["id"], ToolLLM([
            ("read_field", {"card_ref": "C1", "path": "/first_mes"}),
            ("set_opening", {"card_ref": "C1", "index": 0, "narrative": source["document"]["first_mes"] + "\n<initvar>hp: 8</initvar>"})]),
            "只设置开场初始化，正文原样保留")
        assert result["status"] == "completed", result["error"]
        card = editor.edits.conversation(workspace["id"])["cards"][0]
        assert card["document"]["first_mes"] == source["document"]["first_mes"] + "\n<initvar>hp: 8</initvar>"
        assert card["document"].get("extensions", {}) == source["document"].get("extensions", {})
        observed = await probe_card(store, card)
        assert observed["status"] == "passed"
        assert observed["checks"][0]["state"]["stat_data"]["hp"] == 8
    finally:
        await editor.close()


@pytest.mark.parametrize("target", [None, "half_agent", "full_agent"])
async def test_mvu_adaptation_uses_shared_validation_without_requiring_target(tmp_path, target):
    store = Store(tmp_path)
    editor = CardEditorService(store)
    source = import_fixture(store)
    workspace = editor.edits.create_conversation()
    wid = workspace["id"]
    workspace = editor.edits.attach(wid, source["id"], workspace["revision"])
    document = copy.deepcopy(source["document"])
    document["character_book"]["entries"][0]["content"] = "stat_data.hp should track injuries"
    workspace = editor.edits.update_card(wid, "C1", document, workspace["revision"], target_mode=target)
    try:
        assert workspace["cards"][0]["validation"]["warnings"]
        model = ToolLLM([
            ("read_skill", {"resource": "runtime-guide.md"}),
            ("read_field", {"card_ref": "C1", "path": "/character_book/entries/0"}),
            ("patch_draft", {"card_ref": "C1", "operations": [{"op": "add", "path": "/character_book/entries/0/extensions",
                "value": {"terrarium": {"audience": "tracker_mvu"}}}], "comment": "Use shared variable routing"}),
            ("validate_draft", {"card_ref": "C1"}),
        ])
        result = await run(editor, wid, model, "将变量更新规则标记为变量用途，不改变其他内容")
        assert result["status"] == "completed", result["error"]
        card = editor.edits.conversation(wid)["cards"][0]
        assert card["validation"]["warnings"] == []
        assert card["target_mode"] == target
        assert card["document"]["character_book"]["entries"][0]["extensions"]["terrarium"]["audience"] == "tracker_mvu"
    finally:
        await editor.close()


def test_schema_two_migration_preserves_identity_history_and_backup(tmp_path):
    store = Store(tmp_path)
    card = import_fixture(store)
    with store.connection() as db:
        db.execute("UPDATE meta SET value='2' WHERE key='schema_version'")
        db.execute("INSERT INTO card_edits VALUES (?,?,?,?,?,?,?,?)", ("legacy-chat", card["id"], dump(card), 0, dump(card["document"]), dump(card["document"]), 7, 100))
        db.execute("INSERT INTO card_edit_runs VALUES ('legacy-run','legacy-chat','once',?,'completed','原回复',NULL,100)", (dump({"text": "原要求"}),))
    reopened = Store(tmp_path)
    workspace = CardEditorService(reopened).edits.conversation("legacy-chat")
    assert workspace["revision"] == 7 and workspace["cards"][0]["ref"] == "C1"
    assert [m["content"] for m in workspace["messages"]] == ["原要求", "原回复"]
    assert list(tmp_path.glob("before-card-editor-v3-*.sqlite3"))
    assert CardEditorService(Store(tmp_path)).edits.conversation("legacy-chat")["messages"] == workspace["messages"]


def test_undo_reload_restores_original_save_authority(tmp_path):
    store = Store(tmp_path)
    editor = CardEditorService(store)
    card = import_fixture(store)
    workspace = editor.edits.create(card["id"])
    changed = copy.deepcopy(card["document"])
    changed["description"] = "另一处的新版本"
    store.edit_card(card["id"], changed, 0)
    editor.edits.reload_card(workspace["id"], "C1", 0)
    restored = editor.edits.undo_card(workspace["id"], "C1", 1)
    assert restored["cards"][0]["document"] == card["document"]
    assert restored["cards"][0]["base_revision"] == 0
    with pytest.raises(Conflict):
        editor.edits.save_card(workspace["id"], "C1", 2, "overwrite", "no-stale-overwrite")


async def test_nullable_worldbook_is_searchable_and_unrelated_edit_preserves_it(tmp_path):
    store = Store(tmp_path)
    editor = CardEditorService(store)
    card = store.import_card(dump({"name": "无世界书", "description": "原文", "character_book": None}).encode(), "card.json")
    workspace = editor.edits.create(card["id"])
    try:
        result = await run(editor, workspace["id"], ToolLLM([
            ("search_entries", {"card_ref": "C1"}),
            ("read_field", {"card_ref": "C1", "path": "/description"}), patch("C1", "新文")]))
        assert result["status"] == "completed", result["error"]
        document = editor.edits.get(workspace["id"])["document"]
        assert document["character_book"] is None and document["description"] == "新文"
    finally:
        await editor.close()


async def test_new_unsaved_card_does_not_break_asset_cleanup(tmp_path):
    store = Store(tmp_path)
    editor = CardEditorService(store)
    existing = import_fixture(store)
    workspace = editor.edits.create_conversation()
    try:
        result = await run(editor, workspace["id"], ToolLLM([("create_draft", {
            "name": "新卡", "confirmation_ref": "H1", "direct_instruction": True})]), "直接创建一张新卡草稿")
        assert result["status"] == "completed"
        store.delete_card(existing["id"])
        current = editor.edits.conversation(workspace["id"])
        editor.edits.delete(workspace["id"], current["revision"])
        assert not (store.root / "blobs" / existing["original"]).exists()
    finally:
        await editor.close()
