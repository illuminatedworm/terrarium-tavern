"""Reference and manual draft changes append effective state on the real wire."""

import copy
import json

import pytest

from tavern.character.card_author import CardAuthorService
from tavern.core.models import CardAuthorRequest, PersonaEdit
from tavern.storage.store import Store
from tests.backend.test_card_author import import_fixture
from tests.backend.test_card_author_alignment import Editor
from tests.backend.test_half_agent_alignment import blocks


async def execute(editor, plan, text="继续"):
    editor.plans.append(plan)
    workspace = editor.service.edits.conversation(editor.workspace["id"])
    result = editor.service.submit(
        workspace["id"],
        CardAuthorRequest(
            text=text, expected_revision=workspace["revision"], operation_id=str(len(workspace["runs"]))
        ),
        legacy=False,
    )
    await editor.service.tasks[result["id"]]
    result = editor.service.edits.run(result["id"])
    assert result["status"] == "completed", result["error"]
    return result


def frames(editor, source):
    return [
        m
        for m in editor.service.edits.context(editor.workspace["id"])["messages"]
        if m.get("message_type") == "editor_" + source
    ]


@pytest.mark.parametrize("api", ["responses", "anthropic"])
async def test_initial_and_mid_conversation_references_share_append_only_frames(tmp_path, monkeypatch, api):
    editor = Editor(tmp_path, monkeypatch, api)
    edits, wid = editor.service.edits, editor.workspace["id"]

    def workspace():
        return edits.conversation(wid)

    try:
        persona = editor.store.save_persona(
            None, PersonaEdit(name="初始用户角色", description="完整角色资料\n" + "原文" * 2000 + "结尾")
        )
        initial = edits.attach_persona(wid, persona["id"], workspace()["revision"], persona["version"])
        p1 = next(iter(initial["state"]["persona_attachments"]))
        await execute(
            editor,
            [editor.tool(1, "read_field", {"card_ref": "C1", "path": "/description"}), blocks(api, "已读取")],
        )
        assert len(frames(editor, "overview")) == len(frames(editor, "persona_attachments")) == 1
        assert (
            persona["description"].replace("\n", "\\n") in frames(editor, "persona_attachments")[0]["content"]
        )
        before = editor.wire(editor.requests[-1])
        epoch = edits.context(wid)["epoch"]
        evidence = copy.deepcopy(edits.context(wid)["read_versions"])
        card = import_fixture(editor.store)
        attached = edits.attach(wid, card["id"], workspace()["revision"])
        c2 = attached["cards"][-1]["ref"]
        other = editor.store.save_persona(
            None, PersonaEdit(name="中途用户角色", description="第二份完整设定")
        )
        edits.attach_persona(wid, other["id"], workspace()["revision"], other["version"])
        result = await execute(editor, [blocks(api, "继续")])
        after = editor.wire(editor.requests[-1])
        assert after[: len(before)] == before
        assert edits.context(wid)["epoch"] == epoch
        assert edits.context(wid)["read_versions"] == evidence
        assert not any(e["kind"] == "context_baseline" for e in edits.events(result["id"]))
        assert len(frames(editor, "overview")) == len(frames(editor, "persona_attachments")) == 2
        assert c2 in frames(editor, "overview")[-1]["content"]
        assert "第二份完整设定" in frames(editor, "persona_attachments")[-1]["content"]
        await execute(editor, [blocks(api, "未改变资料")])
        assert len(frames(editor, "overview")) == len(frames(editor, "persona_attachments")) == 2
        before = editor.wire(editor.requests[-1])
        edits.remove_card(wid, c2, workspace()["revision"])
        edits.detach_persona(wid, p1, workspace()["revision"])
        await execute(editor, [blocks(api, "已移除引用")])
        assert editor.wire(editor.requests[-1])[: len(before)] == before
        latest = json.loads(frames(editor, "overview")[-1]["content"].split("：", 1)[1])
        assert latest["changes"]["removed"] == [c2]
        assert all(c["ref"] != c2 for c in latest["cards"])
        assert "初始用户角色" not in frames(editor, "persona_attachments")[-1]["content"]
        assert edits.context(wid)["epoch"] == epoch
    finally:
        await editor.service.close()


@pytest.mark.parametrize("api", ["responses", "anthropic"])
async def test_same_length_manual_edit_preserves_prefix_and_unaffected_read_evidence(
    tmp_path, monkeypatch, api
):
    editor = Editor(tmp_path, monkeypatch, api)
    wid = editor.workspace["id"]
    try:
        await execute(
            editor, [editor.tool(1, "read_field", {"card_ref": "C1", "path": ""}), blocks(api, "已读取整卡")]
        )
        before = editor.wire(editor.requests[-1])
        baseline = editor.service.edits.context(wid)
        workspace = editor.service.edits.conversation(wid)
        document = copy.deepcopy(workspace["cards"][0]["document"])
        original = document["description"]
        replacement = "新" * len(original)
        document["description"] = replacement
        editor.service.edits.update_card(wid, "C1", document, workspace["revision"])
        await editor.service.close()
        editor.service = CardAuthorService(Store(tmp_path))
        start = len(editor.requests)
        result = await execute(
            editor,
            [
                editor.tool(
                    2,
                    "set_text_field",
                    {
                        "card_ref": "C1",
                        "path": "/description",
                        "text": "不能直接覆盖",
                        "comment": "检查旧读取",
                    },
                ),
                editor.tool(
                    3,
                    "set_text_field",
                    {
                        "card_ref": "C1",
                        "path": "/first_mes",
                        "text": "独立字段可修改",
                        "comment": "保留有效证据",
                    },
                ),
                editor.tool(4, "read_field", {"card_ref": "C1", "path": "/description"}),
                editor.tool(
                    5,
                    "set_text_field",
                    {
                        "card_ref": "C1",
                        "path": "/description",
                        "text": "重新读取后的合法修改",
                        "comment": "更新描述",
                    },
                ),
                blocks(api, "完成"),
            ],
        )
        assert editor.wire(editor.requests[start])[: len(before)] == before
        assert editor.service.edits.context(wid)["epoch"] == baseline["epoch"]
        changes = frames(editor, "overview")[1]["content"]
        assert '"/description"' in changes and '"changes"' in changes
        results = [
            e["payload"] for e in editor.service.edits.events(result["id"]) if e["kind"] == "tool_finished"
        ]
        assert [r["status"] for r in results] == ["error", "completed", "completed", "completed"]
        assert replacement in results[2]["result"]
        wire = json.dumps([json.loads(r.content) for r in editor.requests], ensure_ascii=False)
        assert not any(
            digest in wire for entries in baseline["read_versions"].values() for digest in entries.values()
        )
        for record in baseline["frames"]["overview"]["documents"].values():
            assert record["document"] not in wire
            assert not any(digest in wire for digest in record["fields"].values())
        assert "read_versions" not in wire
        await execute(editor, [blocks(api, "再次继续")])
        assert editor.service.edits.context(wid)["epoch"] == baseline["epoch"]
    finally:
        await editor.service.close()


@pytest.mark.parametrize("changed", [False, True])
async def test_old_overview_ledger_upgrades_without_rebuilding_prefix(tmp_path, monkeypatch, changed):
    editor = Editor(tmp_path, monkeypatch, "responses")
    wid = editor.workspace["id"]
    try:
        await execute(
            editor,
            [editor.tool(1, "read_field", {"card_ref": "C1", "path": ""}), blocks("responses", "完成")],
        )
        saved = editor.service.edits.context(wid)
        before = editor.wire(editor.requests[-1])
        saved["frames"]["overview"] = saved["frames"]["overview"]["overview"]
        with editor.store.connection() as db:
            db.execute("UPDATE card_edit_contexts SET payload=? WHERE workspace=?", (json.dumps(saved), wid))
        if changed:
            workspace = editor.service.edits.conversation(wid)
            document = copy.deepcopy(workspace["cards"][0]["document"])
            document["description"] = "新" * len(document["description"])
            editor.service.edits.update_card(wid, "C1", document, workspace["revision"])
        await execute(editor, [blocks("responses", "继续")])
        current = editor.service.edits.context(wid)
        assert editor.wire(editor.requests[-1])[: len(before)] == before
        assert current["epoch"] == saved["epoch"]
        assert len(frames(editor, "overview")) == (2 if changed else 1)
        assert ("/description" in current["read_versions"]["C1"]) is not changed
        assert "/first_mes" in current["read_versions"]["C1"]
    finally:
        await editor.service.close()
