"""Text editing and read evidence across real editor run/window boundaries."""

import copy
import json
from types import SimpleNamespace

import pytest

from tavern.character.card_author import CardAuthorService
from tavern.character.editor_reads import known
from tavern.character.editor_tools import EditorTools
from tavern.core.model_view import ModelView
from tavern.storage.store import Store, dump
from tests.backend.test_card_author import ToolLLM, fixture, import_fixture
from tests.backend.test_card_authoring import run


def text_call(path, text):
    return "set_text_field", {"card_ref": "C1", "path": path, "text": text, "comment": "修改正文"}


def tools_for(document=None):
    item = {"ref": "C1", "kind": "draft", "document": document or fixture()}
    owner = SimpleNamespace(
        working={"cards": [item]},
        reads={},
        legacy=False,
        context=SimpleNamespace(view=ModelView(item["document"])),
        edits=SimpleNamespace(item=lambda working, ref: next(c for c in working["cards"] if c["ref"] == ref)),
        event=lambda *args: None,
    )
    return EditorTools(owner), item


async def read(tools, path=""):
    return await tools.execute("read_field", {"card_ref": "C1", "path": path})


@pytest.mark.parametrize("wrapped", [False, True])
async def test_text_field_preserves_long_literal_text_and_unrelated_data(wrapped):
    document = fixture()
    opaque = "0123456789abcdef0123456789abcdef"
    document["extensions"][opaque] = {"a/b~c": '原文 "引用" ' + opaque, "source_hash": "private-version"}
    if wrapped:
        document = {"spec": "chara_card_v2", "spec_version": "2.0", "data": document}
    tools, item = tools_for(document)
    before = copy.deepcopy(document)
    prefix = "/data" if wrapped else ""
    path = prefix + "/character_book/entries/0/content"
    await read(tools, path)
    text = ('他说："未知"。\n`stat_data` {{user}} \\n 原样保留\n' * 1000) + "完整尾部"
    await tools.execute(*text_call(path, text))
    expected = copy.deepcopy(before)
    (expected["data"] if wrapped else expected)["character_book"]["entries"][0]["content"] = text
    assert item["document"] == expected
    await tools.execute(*text_call(path, ""))
    assert (item["document"]["data"] if wrapped else item["document"])["character_book"]["entries"][0][
        "content"
    ] == ""
    # The new entry point uses the same reversible paths and text mapping.
    actual = prefix + "/extensions/" + opaque + "/a~1b~0c"
    projected = tools.view.pointer(actual)
    result = await read(tools, projected)
    assert opaque not in dump(result)
    await tools.execute(*text_call(projected, result["text"] + " 新内容"))
    current = item["document"]["data"] if wrapped else item["document"]
    assert current["extensions"][opaque] == {
        "a/b~c": '原文 "引用" ' + opaque + " 新内容",
        "source_hash": "private-version",
    }
    with pytest.raises(ValueError, match="未知资料短编号"):
        await tools.execute(*text_call(projected, "@D999999"))


async def test_text_field_rejects_unread_nontext_missing_hidden_and_stale_values():
    tools, item = tools_for()
    before = copy.deepcopy(item["document"])
    with pytest.raises(ValueError, match="完整读取"):
        await tools.execute(*text_call("/description", "不应写入"))
    await read(tools)
    for path in ("/extensions", "/missing", "/extensions/source_hash"):
        with pytest.raises((ValueError, LookupError)):
            await tools.execute(*text_call(path, "不应写入"))
        assert item["document"] == before
    item["document"]["description"] = "外部更新"
    with pytest.raises(ValueError, match="完整读取"):
        await tools.execute(*text_call("/description", "不能覆盖外部更新"))
    # A sibling version is still valid even though the root version changed.
    await tools.execute(*text_call("/character_book/entries/0/comment", "新标题"))
    assert item["document"]["description"] == "外部更新"


async def test_removing_read_array_element_does_not_authorize_unseen_successor():
    tools, item = tools_for()
    await read(tools, "/character_book/entries/0")
    await tools.execute(
        "patch_draft",
        {
            "card_ref": "C1",
            "comment": "删除已读条目",
            "operations": [{"op": "remove", "path": "/character_book/entries/0"}],
        },
    )
    assert item["document"]["character_book"]["entries"][0]["comment"] == "变量规则"
    with pytest.raises(ValueError, match="完整读取"):
        await tools.execute(*text_call("/character_book/entries/0/content", "不能写入未读条目"))


async def test_previews_and_historical_versions_do_not_grant_current_read_evidence():
    tools, item = tools_for()
    tools.owner.base = copy.deepcopy(tools.owner.working)
    item["base_document"] = copy.deepcopy(item["document"])
    await tools.execute("overview", {"card_ref": "C1"})
    await tools.execute("search_entries", {"card_ref": "C1", "query": "人物"})
    for version in ("saved", "round_start"):
        await tools.execute("read_field", {"card_ref": "C1", "path": "", "version": version})
    with pytest.raises(ValueError, match="完整读取"):
        await tools.execute(*text_call("/character_book/entries/0/content", "不得视为已读"))


async def test_completed_reads_and_writes_survive_next_run_and_service_restart(tmp_path):
    store = Store(tmp_path)
    editor = CardAuthorService(store)
    wid = editor.edits.create(import_fixture(store)["id"])["id"]
    try:
        first = await run(
            editor,
            wid,
            ToolLLM(
                [
                    ("read_field", {"card_ref": "C1", "path": "/character_book"}),
                    text_call("/character_book/entries/0/content", "第一轮正文"),
                ]
            ),
        )
        assert first["status"] == "completed"
        saved = editor.edits.context(wid)
        await editor.close()
        editor = CardAuthorService(Store(tmp_path))
        provider = ToolLLM(
            [
                (
                    "patch_draft",
                    {
                        "card_ref": "C1",
                        "comment": "下一轮直接编辑已知版本",
                        "operations": [
                            {
                                "op": "replace",
                                "path": "/character_book/entries/0/content",
                                "value": "第二轮正文",
                            },
                            {
                                "op": "replace",
                                "path": "/character_book/entries/0/comment",
                                "value": "第二轮标题",
                            },
                        ],
                    },
                )
            ]
        )
        second = await run(editor, wid, provider)
        assert second["status"] == "completed"
        assert not any(e["payload"].get("status") == "error" for e in editor.edits.events(second["id"]))
        entry = editor.edits.get(wid)["document"]["character_book"]["entries"][0]
        assert (entry["content"], entry["comment"]) == ("第二轮正文", "第二轮标题")
        outbound = dump(provider.call_log)
        assert "普通世界设定" in outbound
        carried = [
            json.loads(call["function"]["arguments"])
            for message in provider.call_log[0]
            for call in message.get("tool_calls", [])
            if call["function"]["name"] == "set_text_field"
        ]
        assert carried[0]["text"] == "第一轮正文"
        assert "read_versions" not in outbound
        assert all(digest not in outbound for digest in saved["read_versions"]["C1"].values())
    finally:
        await editor.close()


@pytest.mark.parametrize("invalidation", ["rollover", "model", "draft", "old_context", "revert"])
async def test_removed_or_rebuilt_context_requires_fresh_read(tmp_path, invalidation):
    store = Store(tmp_path)
    editor = CardAuthorService(store)
    wid = editor.edits.create(import_fixture(store)["id"])["id"]
    try:
        await run(editor, wid, ToolLLM([("read_field", {"card_ref": "C1", "path": "/description"})]))
        prefix = []
        if invalidation == "rollover":
            prefix = [("new_context", {"content": "保留修改目标，换窗后继续。"})]
        elif invalidation == "revert":
            prefix = [
                text_call("/description", "临时更改"),
                ("revert_draft", {"card_ref": "C1", "comment": "撤销"}),
            ]
        elif invalidation == "model":
            settings = store.settings_document()
            settings.shared.card_author.model = "different-model"
            store.save_settings(settings)
        else:
            # Simulate an externally edited draft or a pre-upgrade context record.
            with store.connection() as db:
                if invalidation == "draft":
                    ws = editor.edits.load(db, wid)
                    ws["cards"][0]["document"]["description"] = "人工修改"
                    editor.edits.write(db, ws, increment=True)
                else:
                    saved = editor.edits.context(wid)
                    saved.pop("read_versions")
                    db.execute(
                        "UPDATE card_edit_contexts SET payload=? WHERE workspace=?", (dump(saved), wid)
                    )
        provider = ToolLLM(
            [
                *prefix,
                text_call("/description", "首次不应通过"),
                ("read_field", {"card_ref": "C1", "path": "/description"}),
                text_call("/description", "重新读取后通过"),
            ]
        )
        result = await run(editor, wid, provider)
        assert result["status"] == "completed", result["error"]
        results = [
            e["payload"]
            for e in editor.edits.events(result["id"])
            if e["kind"] == "tool_finished" and e["payload"]["tool"] == "set_text_field"
        ]
        assert [r["status"] for r in results[-2:]] == ["error", "completed"]
        assert "完整读取" in results[-2]["detail"]
    finally:
        await editor.close()


async def test_read_versions_resume_after_transport_failure(tmp_path):
    store = Store(tmp_path)
    editor = CardAuthorService(store)
    wid = editor.edits.create(import_fixture(store)["id"])["id"]

    class Failure(ToolLLM):
        async def chat(self, messages, **kwargs):
            if self.call_count == 2:
                raise ValueError("offline fixture failure")
            async for chunk in super().chat(messages, **kwargs):
                yield chunk

    try:
        failed = await run(
            editor,
            wid,
            Failure(
                [
                    ("read_field", {"card_ref": "C1", "path": "/description"}),
                    text_call("/description", "中断前版本"),
                ]
            ),
        )
        assert failed["status"] == "error"
        await editor.close()
        editor = CardAuthorService(Store(tmp_path))
        resumed = await run(
            editor, wid, ToolLLM([text_call("/description", "续接版本")]), "", resume=failed["id"]
        )
        assert resumed["status"] == "completed", resumed["error"]
        assert editor.edits.get(wid)["document"]["description"] == "续接版本"
    finally:
        await editor.close()


async def test_failed_atomic_patch_preserves_document_and_read_versions():
    tools, item = tools_for()
    await read(tools)
    before, reads = copy.deepcopy(item["document"]), copy.deepcopy(tools.owner.reads)
    with pytest.raises(Exception):
        await tools.execute(
            "patch_draft",
            {
                "card_ref": "C1",
                "comment": "失败批次",
                "operations": [
                    {"op": "replace", "path": "/description", "value": "不能部分写入"},
                    {"op": "test", "path": "/name", "value": "错误名称"},
                ],
            },
        )
    assert item["document"] == before and tools.owner.reads == reads
    assert known(reads, "C1", item["document"], "/description")
