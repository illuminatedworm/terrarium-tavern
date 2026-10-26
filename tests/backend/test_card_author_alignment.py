"""Editor cache contracts through the real agent and offline HTTP transport."""

import copy
import json
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest

from tavern.character.card_author import CardAuthorService
from tavern.character.editor_context import EditorContext
from tavern.core.models import CardAuthorRequest, Settings
from tavern.scripting.model_transport import ModelTransport
from tavern.storage.backup import backup, restore
from tavern.storage.maintenance import cleanup
from tavern.storage.store import Store
from tests.backend.test_card_author import ToolLLM, import_fixture
from tests.backend.test_full_agent_alignment import reply
from tests.backend.test_half_agent_alignment import blocks, sse


class Editor:
    def __init__(self, tmp_path, monkeypatch, api, mode="runtime"):
        self.store = Store(tmp_path)
        settings = self.store.settings_document()
        settings.full_agent.model.api_mode = api
        settings.half_agent.foreground.api_mode = api
        settings.half_agent.background.api_mode = api
        settings.shared.card_author.api_mode = api
        settings.shared.providers[0].api_key = "editor-test-secret"
        settings.shared.providers[0].base_url = "https://fixture.test"
        settings.shared.network.api_proxy = False
        settings.shared.card_author.model = "editor-model"
        settings.shared.card_author.cache.mode = mode
        self.store.save_settings(settings)
        self.service = CardAuthorService(self.store)
        self.workspace = self.service.edits.create(import_fixture(self.store)["id"])
        self.requests, self.plans = [], []
        self.api = api

        async def factory(cfg, capture):
            plan = iter(self.plans.pop(0))

            def route(request):
                self.requests.append(request)
                payload = reply(api, next(plan))
                # Native Anthropic yields for tools with tool_use, not end_turn.
                if api == "anthropic" and any(p["type"] == "tool_use" for p in payload["content"]):
                    payload["stop_reason"] = "tool_use"
                return sse(api, payload)

            return ModelTransport(
                cfg, cfg.api_key, capture, http_client=httpx.AsyncClient(transport=httpx.MockTransport(route))
            )

        monkeypatch.setattr("tavern.character.card_author.make_provider", factory)

    async def run(self, plan, text="编辑草稿"):
        self.plans.append(plan)
        ws = self.service.edits.conversation(self.workspace["id"])
        run = self.service.submit(
            ws["id"],
            CardAuthorRequest(text=text, expected_revision=ws["revision"], operation_id=str(len(ws["runs"]))),
        )
        await self.service.tasks[run["id"]]
        result = self.service.edits.run(run["id"])
        assert result["status"] == "completed", result["error"]
        return result

    def tool(self, index, name, args):
        parts = blocks(self.api, "正在编辑")
        parts.insert(
            1,
            {"type": "tool_use", "id": f"tool{index}", "name": name, "input": args}
            if self.api == "anthropic"
            else {
                "type": "function_call",
                "id": f"fc{index}",
                "call_id": f"tool{index}",
                "name": name,
                "arguments": json.dumps(args),
            },
        )
        return parts

    def wire(self, request):
        body = json.loads(request.content)
        return body["messages" if self.api == "anthropic" else "input"]


@pytest.mark.parametrize("api", ["anthropic", "responses"])
async def test_text_tool_native_schema_and_cross_turn_read_evidence(tmp_path, monkeypatch, api):
    editor = Editor(tmp_path, monkeypatch, api)

    async def execute(plan):
        editor.plans.append(plan)
        ws = editor.service.edits.get(editor.workspace["id"])
        result = editor.service.submit(
            ws["id"],
            CardAuthorRequest(
                text="修改世界书正文", expected_revision=ws["revision"], operation_id=str(len(ws["runs"]))
            ),
            legacy=False,
        )
        await editor.service.tasks[result["id"]]
        result = editor.service.edits.run(result["id"])
        assert result["status"] == "completed", result["error"]
        failures = [
            e
            for e in editor.service.edits.events(result["id"])
            if e["kind"] == "tool_finished" and e["payload"]["status"] == "error"
        ]
        assert not failures

    try:
        await execute(
            [
                editor.tool(1, "read_field", {"card_ref": "C1", "path": "/character_book"}),
                blocks(api, "已读取"),
            ]
        )
        evidence = editor.service.edits.context(editor.workspace["id"])["read_versions"]
        text = '他说："未知"。\n`Money` {{user}} \\n\n完整尾部'
        await execute(
            [
                editor.tool(
                    2,
                    "set_text_field",
                    {
                        "card_ref": "C1",
                        "path": "/character_book/entries/0/content",
                        "text": text,
                        "comment": "修改正文",
                    },
                ),
                blocks(api, "完成"),
            ]
        )
        body = json.loads(editor.requests[-1].content)
        schema = next(t for t in body["tools"] if t["name"] == "set_text_field")
        parameters = schema["input_schema" if api == "anthropic" else "parameters"]
        assert set(parameters["properties"]) == {"card_ref", "path", "text", "comment"}
        assert all(p["type"] == "string" for p in parameters["properties"].values())
        assert (
            editor.service.edits.get(editor.workspace["id"])["document"]["character_book"]["entries"][0][
                "content"
            ]
            == text
        )
        wire = json.dumps(body, ensure_ascii=False)
        assert "普通世界设定" in wire
        assert "read_versions" not in wire
        assert all(value not in wire for value in evidence["C1"].values())
    finally:
        await editor.service.close()


@pytest.mark.parametrize("api", ["anthropic", "responses"])
async def test_editor_argument_failure_guides_repair_and_survives_native_replay(tmp_path, monkeypatch, api):
    editor = Editor(tmp_path, monkeypatch, api)
    # Reproduce the double-encoded array with unescaped prose quotes. Neither
    # automatic JSON repair nor retrying the same input should write the draft.
    bad = editor.tool(
        1,
        "patch_draft",
        {
            "card_ref": "C1",
            "comment": "修改开场",
            "operations": '[{"op":"replace","path":"/first_mes","value":"他说"你好"。SECRET_SOURCE_123"}]',
        },
    )
    good = editor.tool(2, "set_opening", {"card_ref": "C1", "index": 0, "narrative": '他说："**你好**"。'})

    def results(wire):
        if api == "anthropic":
            return [
                part
                for message in wire
                for part in message.get("content", [])
                if isinstance(part, dict) and part.get("type") == "tool_result"
            ]
        return [item for item in wire if item.get("type") == "function_call_output"]

    try:
        run = await editor.run([bad, good, blocks(api, "完成")])
        requests = [json.loads(r.content) for r in editor.requests]
        failed = results(editor.wire(editor.requests[1]))[0]
        content = failed["content" if api == "anthropic" else "output"]
        error = json.loads(content)["error"]
        assert all(
            text in error for text in ("operations", "JSON 数组", "不要原样重试", "set_opening", "narrative")
        )
        assert "SECRET_SOURCE_123" not in error
        assert "input_value" not in error
        if api == "anthropic":
            assert failed["is_error"] is True
            assert "is_error" not in results(editor.wire(editor.requests[2]))[1]
        else:
            assert "is_error" not in failed
        assert "_tool_error" not in json.dumps(requests)
        card = editor.service.edits.conversation(editor.workspace["id"])["cards"][0]
        assert card["document"]["first_mes"] == '他说："**你好**"。'
        assert card["base_document"]["first_mes"] == "开场一"
        with editor.store.connection() as db:
            trace = json.loads(
                db.execute("SELECT payload FROM card_author_traces WHERE run=?", (run["id"],)).fetchone()[0]
            )
        returned = [m for m in trace if m["role"] == "tool"]
        assert returned[0]["_tool_error"] is True
        assert "_tool_error" not in returned[1]
        # Stored context must preserve the same result on a subsequent run.
        await editor.service.close()
        editor.service = CardAuthorService(Store(tmp_path))
        await editor.run([blocks(api, "后续回复")])
        assert results(editor.wire(editor.requests[-1]))[0] == failed
    finally:
        await editor.service.close()


@pytest.mark.parametrize("api", ["anthropic", "responses"])
@pytest.mark.parametrize("mode", ["runtime", "cloud"])
async def test_editor_native_prefix_identity_and_committed_replay(tmp_path, monkeypatch, api, mode):
    editor = Editor(tmp_path, monkeypatch, api, mode)
    first = editor.tool(
        1,
        "patch_draft",
        {
            "operations": [{"op": "replace", "path": "/name", "value": "更新后的角色名称"}],
            "comment": "修改名称",
        },
    )
    second = editor.tool(2, "validate_draft", {})
    final = blocks(api, "完成编辑")
    try:
        run = await editor.run([first, second, final])
        wires = [editor.wire(r) for r in editor.requests]
        for previous, current in zip(wires, wires[1:]):
            assert current[: len(previous)] == previous
        if api == "anthropic":
            assert wires[1][len(wires[0])]["content"] == first
            assert wires[2][len(wires[1])]["content"] == second
            assert all(
                json.loads(r.content)["system"] == json.loads(editor.requests[0].content)["system"]
                for r in editor.requests
            )
        else:
            assert wires[1][len(wires[0]) : len(wires[0]) + len(first)] == first
            assert wires[2][len(wires[1]) : len(wires[1]) + len(second)] == second
        # Changed overview follows the tool result; unchanged overview isn't repeated.
        assert "更新后的角色名称" in json.dumps(wires[1][-1], ensure_ascii=False)
        if api == "anthropic":
            assert "tool_result" in json.dumps(wires[1][-2])
        else:
            assert wires[1][-2]["type"] == "function_call_output"
        assert sum("当前草稿概览" in json.dumps(m, ensure_ascii=False) for m in wires[2]) == 2
        header = "x-claude-code-session-id" if api == "anthropic" else "session-id"
        sid = str(UUID(editor.workspace["id"]))
        assert all(r.headers[header] == sid for r in editor.requests)
        if api == "responses":
            assert "x-codex-turn-state" not in editor.requests[0].headers
            assert editor.requests[1].headers["x-codex-turn-state"] == "routing"
            assert json.loads(editor.requests[0].content)["prompt_cache_key"] == sid
        events = editor.service.edits.events(run["id"])
        assert len([e for e in events if e["kind"] == "provider_response"]) == 3
        assert all(
            e["payload"]["request_context"]["session_id"] == sid
            for e in events
            if e["kind"] == "provider_request"
        )
        # A new service instance resumes the committed prefix, without reviving turn routing.
        await editor.service.close()
        editor.service = CardAuthorService(Store(tmp_path))
        await editor.run([blocks(api, "第二轮完成")])
        resumed = editor.wire(editor.requests[-1])
        assert resumed[: len(wires[-1])] == wires[-1]
        if api == "anthropic":
            assert resumed[len(wires[-1])]["content"] == final
        else:
            assert resumed[len(wires[-1]) : len(wires[-1]) + len(final)] == final
        assert editor.requests[-1].headers[header] == sid
        assert "x-codex-turn-state" not in editor.requests[-1].headers
        carried = editor.service.edits.new_context(editor.workspace["id"], 1)
        editor.workspace = carried
        await editor.run([blocks(api, "新对话完成")])
        assert editor.requests[-1].headers[header] != sid
        assert "signed-opaque" not in json.dumps(editor.wire(editor.requests[-1]))
        assert "encrypted_content" not in json.dumps(editor.wire(editor.requests[-1]))
    finally:
        await editor.service.close()


@pytest.mark.parametrize(
    "change", ["model", "credential", "reasoning", "draft", "history", "handles", "call_notifications"]
)
async def test_editor_rebuilds_stale_context(tmp_path, monkeypatch, change):
    editor = Editor(tmp_path, monkeypatch, "responses")
    try:
        await editor.run([blocks("responses", "旧回复")])
        before = editor.service.edits.context(editor.workspace["id"])
        text = "继续"
        expected = "provider_or_model_changed"
        if change in {"model", "credential"}:
            settings = editor.store.settings_document()
            if change == "model":
                settings.shared.card_author.model = "new-model"
            else:
                settings.shared.providers[0].api_key = "different-editor-secret"
            editor.store.save_settings(settings)
            if change == "model":
                # Composer model selections belong to the conversation, not the global default.
                with editor.store.connection() as db:
                    db.execute(
                        "UPDATE card_author_conversations SET model_selection=? WHERE id=?",
                        (
                            json.dumps({"provider_id": "default", "model": "new-model"}),
                            editor.workspace["id"],
                        ),
                    )
        elif change == "reasoning":
            settings = editor.store.settings_document()
            settings.shared.card_author.reasoning_effort = "high"
            editor.store.save_settings(settings)
            expected = None
        elif change == "draft":
            doc = copy.deepcopy(editor.workspace["document"])
            doc["description"] = "手工改写设定"
            editor.service.edits.update(editor.workspace["id"], doc, 0)
            expected = None
        elif change == "history":
            with editor.store.connection() as db:
                for i in range(7):
                    db.execute(
                        "INSERT INTO card_edit_runs VALUES (?,?,?,'{}','error','','',?)",
                        (f"failed{i}", editor.workspace["id"], f"failed{i}", 9999999999 + i),
                    )
            expected = "context_window_changed"
        elif change == "call_notifications":
            before["version"] = 3
            before["messages"].append(
                {
                    "role": "system",
                    "message_type": "last_call_notice",
                    "content": "本轮最后一次调用：只汇报结果和未完成项。",
                }
            )
            with editor.store.connection() as db:
                db.execute(
                    "UPDATE card_edit_contexts SET payload=? WHERE workspace=?",
                    (json.dumps(before), editor.workspace["id"]),
                )
            expected = "context_version_changed"
        else:
            # A literal handle in a new user request must not be restored to an old ID.
            doc = copy.deepcopy(editor.workspace["document"])
            doc["extensions"]["opaque"] = "12345678-1234-1234-1234-123456789abc"
            editor.service.edits.update(editor.workspace["id"], doc, 0)
            await editor.run([blocks("responses", "已读取新草稿")])
            text, expected = "保留字面量 @D1", "literal_handle_collision"
        run = await editor.run([blocks("responses", "已处理")], text)
        events = editor.service.edits.events(run["id"])
        if change == "reasoning":
            assert not any(e["kind"] == "context_baseline" for e in events)
            saved = editor.service.edits.context(editor.workspace["id"])
            assert saved["epoch"] == before["epoch"]
            assert saved["messages"][: len(before["messages"])] == before["messages"]
            assert editor.wire(editor.requests[-1])[: len(editor.wire(editor.requests[0]))] == editor.wire(
                editor.requests[0]
            )
            assert "encrypted_content" in json.dumps(editor.wire(editor.requests[-1]))
            assert json.loads(editor.requests[-1].content)["reasoning"]["effort"] == "high"
            return
        if change == "draft":
            assert not any(e["kind"] == "context_baseline" for e in events)
            saved = editor.service.edits.context(editor.workspace["id"])
            assert saved["epoch"] == before["epoch"]
            assert saved["messages"][: len(before["messages"])] == before["messages"]
            assert any(
                '"/description"' in m.get("content", "") and '"changes"' in m.get("content", "")
                for m in saved["messages"][len(before["messages"]) :]
            )
            return
        if change == "history":
            # Failed attempts no longer evict complete history after seven rows.
            assert not any(e["kind"] == "context_baseline" for e in events)
            assert editor.service.edits.context(editor.workspace["id"])["epoch"] == before["epoch"]
            return
        assert (
            next(e["payload"]["rebuild_reason"] for e in events if e["kind"] == "context_baseline")
            == expected
        )
        assert editor.service.edits.context(editor.workspace["id"])["epoch"] != before["epoch"]
        assert "encrypted_content" not in json.dumps(editor.wire(editor.requests[-1]))
        if change == "call_notifications":
            assert "last_call_notice" not in json.dumps(editor.wire(editor.requests[-1]))
            assert "本轮最后一次调用" not in json.dumps(editor.wire(editor.requests[-1]), ensure_ascii=False)
            saved = editor.service.edits.context(editor.workspace["id"])
            assert saved["version"] == 4 and "last_call_notice" not in str(saved["messages"])
    finally:
        await editor.service.close()


def test_editor_unicode_reasoning_only_rolls_over_at_actual_text_pressure():
    cfg = Settings()
    cfg.context_tokens = 12000
    owner = SimpleNamespace(
        cfg=cfg,
        definitions=[],
        environment=None,
        edits=SimpleNamespace(messages=lambda wid: []),
        tools=SimpleNamespace(overview=lambda: {"cards": []}),
        wid="context",
        rid="run",
        working={"cards": [], "state": {}},
        current_refs=[],
        trace=[],
        pending_calls=None,
        reads={},
        request={},
    )
    context = EditorContext(None, owner)
    reasoning = '我会保留完整原文，核对工具结果。"下一步"\n' * 300
    context.messages.append({"role": "assistant", "content": "已读取", "reasoning_content": reasoning})
    messages = context.prepare()
    assert not context.must_rollover
    assert "pressure" not in context.frames
    assert messages[1]["reasoning_content"] == reasoning
    # Genuine pressure must still require rollover; do not fix overcounting by
    # ignoring the reasoning payload or dropping any of its text.
    context.messages[1]["reasoning_content"] = reasoning * 5
    messages = context.prepare()
    assert context.must_rollover
    assert messages[1]["reasoning_content"] == reasoning * 5


async def test_editor_context_commits_atomically_and_survives_cleanup_backup(tmp_path):
    store = Store(tmp_path / "source")
    service = CardAuthorService(store)
    ws = service.edits.create(import_fixture(store)["id"])

    async def run(provider, text):
        service.provider_factory = lambda: provider
        current = service.edits.get(ws["id"])
        result = service.submit(
            ws["id"], CardAuthorRequest(text=text, operation_id=text, expected_revision=current["revision"])
        )
        await service.tasks[result["id"]]
        return service.edits.run(result["id"])

    try:
        assert (await run(ToolLLM([]), "完成一轮"))["status"] == "completed"
        saved = service.edits.context(ws["id"])

        class Failure(ToolLLM):
            async def chat(self, messages, **kwargs):
                if self.call_count:
                    raise ValueError("故意失败")
                async for chunk in super().chat(messages, **kwargs):
                    yield chunk

        failed = await run(
            Failure(
                [
                    (
                        "patch_draft",
                        {
                            "operations": [{"op": "replace", "path": "/name", "value": "不应提交"}],
                            "comment": "测试",
                        },
                    )
                ]
            ),
            "失败一轮",
        )
        assert failed["status"] == "error"
        assert service.edits.context(ws["id"]) == saved
        assert service.edits.get(ws["id"])["document"] == ws["document"]
        with store.connection() as db:
            db.execute("UPDATE card_edit_runs SET created=0")
        cleanup(store)
        assert service.edits.context(ws["id"]) == saved
        backup(store, tmp_path / "backup.zip")
        restored = CardAuthorService(restore(tmp_path / "backup.zip", tmp_path / "restored"))
        assert restored.edits.context(ws["id"]) == saved
        restored.edits.discard(ws["id"], 0)
        restored.edits.delete(ws["id"], 1)
        assert restored.edits.context(ws["id"]) is None
        await restored.close()
    finally:
        await service.close()


async def test_editor_preserves_short_handles_and_opaque_responses_across_runs(tmp_path, monkeypatch):
    editor = Editor(tmp_path, monkeypatch, "responses")
    opaque = "12345678-1234-1234-1234-123456789abc"
    doc = copy.deepcopy(editor.workspace["document"])
    doc["extensions"]["opaque"] = opaque
    editor.service.edits.update(editor.workspace["id"], doc, 0)
    first = editor.tool(1, "read_field", {"path": "/extensions/opaque"})
    first[0]["encrypted_content"] = opaque
    try:
        await editor.run([first, blocks("responses", "已读 @D1")])
        saved = editor.service.edits.context(editor.workspace["id"])
        assert saved["view"]["reverse"]["@D1"] == opaque
        assert editor.service.edits.get(editor.workspace["id"])["runs"][-1]["response"].endswith(opaque)
        patch = editor.tool(
            2,
            "patch_draft",
            {
                "operations": [{"op": "replace", "path": "/description", "value": "引用 @D1"}],
                "comment": "引用资料",
            },
        )
        await editor.run([patch, blocks("responses", "已改")])
        assert editor.service.edits.get(editor.workspace["id"])["document"]["description"] == "引用 " + opaque
        wire = editor.wire(editor.requests[-1])
        assert any(item.get("encrypted_content") == opaque for item in wire)
        assert any(
            item.get("type") == "function_call" and item.get("arguments") == patch[1]["arguments"]
            for item in wire
        )
    finally:
        await editor.service.close()
