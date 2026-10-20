"""Half Agent request history, opaque replay and compatibility boundaries (offline)."""

import copy
import json
from uuid import UUID, uuid4

import httpx
import pytest

from tavern.application.service import RoleplayRuntime
from tavern.conversation.context import compile_context
from tavern.conversation.half_context import apply_context, finalize_record, load_trace
from tavern.core.kemini import PREFIX_SOURCES, control_text, prefix_frames
from tavern.core.message_adapter import MessageAdapterConfig, adapt_messages
from tavern.core.models import ContextRecipe, SessionCreate, Settings, TurnRequest
from tavern.memories.history_recall import HISTORY_TOOL, HistoryBudget, complete_with_recall
from tavern.scripting.model_transport import ModelTransport
from tavern.scripting.native_response import attachment
from tavern.scripting.protocols import native_messages
from tavern.scripting.request_context import OperationContexts, configure_operation, response_origin
from tavern.storage.store import Store
from tests.backend.test_full_agent_alignment import reply
from tests.backend.test_history_recall import history as recall_history
from tests.backend.test_history_recall import request as recall_request


class Story:
    def __init__(self, api="anthropic"):
        self.cfg = Settings(
            api_mode=api,
            model="claude-opus-5-5" if api == "anthropic" else "gpt-6-sol",
            api_key="fixture-secret",
            base_url="https://fixture.test/v1",
            use_proxy=False,
            cache={"mode": "runtime", "retention": "1h" if api == "anthropic" else "default"},
        )
        self.session = {
            "id": uuid4().hex,
            "recipe": ContextRecipe().model_dump(),
            "persona": "用户",
            "lore_injection_mode": "conversation",
        }
        self.document = {
            "name": "测试",
            "description": "固定设定",
            "post_history_instructions": "角色后置要求",
        }
        self.snapshot = {"state": {"stat_data": {"hp": 3}}, "memory_document": {"version": 2, "records": []}}
        self.history = [{"id": "opening", "role": "assistant", "raw": "开场", "model": "开场"}]
        self.records = {}
        self.note = "作者注 A"

    def compile(self, **kwargs):
        self.input_id = f"input-{len(self.history)}"
        gen = kwargs.pop("generate", {"overrides": {"chat_history": {"author_note": self.note}}})
        return compile_context(
            self.document,
            self.session,
            self.history,
            self.snapshot,
            "继续",
            self.cfg,
            "fixture",
            generate=gen,
            replay_records=self.records,
            input_message_id=self.input_id,
            **kwargs,
        )

    def commit(self, context, text="正文", native=None, model=None):
        output = {
            "id": f"output-{len(self.history)}",
            "role": "assistant",
            "raw": text,
            "model": text if model is None else model,
        }
        self.records[output["id"]] = finalize_record(context["half_agent_record"], native, text, output)
        self.history.extend([{"id": self.input_id, "role": "user", "raw": "继续", "model": "继续"}, output])
        return output


def blocks(api, text):
    if api == "anthropic":
        return [
            {"type": "thinking", "thinking": "思考", "signature": "signed-opaque"},
            {"type": "text", "text": text},
        ]
    return [
        {"type": "reasoning", "id": "rs_opaque", "summary": [], "encrypted_content": "encrypted"},
        {
            "type": "message",
            "id": "msg_opaque",
            "role": "assistant",
            "content": [{"type": "output_text", "text": text, "annotations": []}],
        },
    ]


def sse(api, payload):
    events = []
    if api == "anthropic":
        events.append({"type": "message_start", "message": {**payload, "content": [], "stop_reason": None}})
        for index, part in enumerate(payload["content"]):
            if part["type"] not in ("text", "thinking"):
                events.append({"type": "content_block_start", "index": index, "content_block": part})
                continue
            events.append(
                {
                    "type": "content_block_start",
                    "index": index,
                    "content_block": {
                        **part,
                        **({"text": ""} if part["type"] == "text" else {"thinking": "", "signature": ""}),
                    },
                }
            )
            if part["type"] == "text":
                events.append(
                    {
                        "type": "content_block_delta",
                        "index": index,
                        "delta": {"type": "text_delta", "text": part["text"]},
                    }
                )
            else:
                for field in ("thinking", "signature"):
                    events.append(
                        {
                            "type": "content_block_delta",
                            "index": index,
                            "delta": {"type": field + "_delta", field: part[field]},
                        }
                    )
        events.extend(
            [
                {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": payload["usage"]},
                {"type": "message_stop"},
            ]
        )
    else:
        text = "".join(
            p["text"] for item in payload["output"] if item["type"] == "message" for p in item["content"]
        )
        events = [
            {"type": "response.output_text.delta", "delta": text},
            {"type": "response.completed", "response": payload},
        ]
    return httpx.Response(
        200,
        content="".join("data: " + json.dumps(e) + "\n\n" for e in events),
        headers={"content-type": "text/event-stream", "x-codex-turn-state": "routing"},
    )


def test_kemini_order_survives_projection_and_turn_replay():
    story = Story("gemini")
    story.cfg.kemini.enabled = True
    story.cfg.kemini.normal_disclaimer_enabled = True
    story.cfg.kemini.filler_text = "小说填充必须最前"
    story.cfg.message_adapter = MessageAdapterConfig(
        mappings={"gemini": {"state": {"role": "system", "wrap": True}}}
    )
    # Even an insertion deeper than all history remains behind the absolute prefix.
    generate = {"injects": [{"role": "system", "content": "最深注入", "depth": 999}]}
    first = story.compile(generate=generate)
    rows = first["trace"]
    sources = [row["source"] for row in rows]
    assert sources[:4] == list(PREFIX_SOURCES)
    assert sources[4] == "rules"
    assert sources.index("inject") + 1 == sources.index("history")
    assert not any("insertion_depth" in row for row in rows[:4])
    expected_prefix = [{"role": "user", "content": row["content"]} for row in prefix_frames(story.cfg.kemini)]
    assert first["messages"][:4] == expected_prefix
    assert (
        sources.index("input")
        < sources.index("kemini_roleplay")
        < sources.index("kemini_acknowledgement")
        < sources.index("kemini_control")
    )
    assert (
        next(row for row in rows if row["source"] == "kemini_roleplay")["content"]
        == story.cfg.kemini.roleplay_prompt
    )
    assert (
        next(row for row in rows if row["source"] == "kemini_acknowledgement")["content"]
        == story.cfg.kemini.acknowledgement
    )
    assert next(row for row in rows if row["source"] == "kemini_control")["content"] == control_text(
        story.cfg.kemini
    )
    assert all(row["role"] != "system" for row in rows)
    wire, ordered, diagnostics = adapt_messages(first["runtime_messages"], story.cfg, managed=False)
    assert [row["message_type"] for row in ordered] == [row["message_type"] for row in rows]
    system, body, _ = native_messages(wire, "gemini")
    assert system == []
    assert body[:4] == [{"role": "user", "parts": [{"text": row["content"]}]} for row in expected_prefix]
    assert any("Kemini" in warning for warning in diagnostics)
    story.commit(first)
    second = story.compile(generate=generate)
    assert [row["source"] for row in second["trace"]].count("kemini_roleplay") == 1
    assert [row["source"] for row in second["trace"]].count("kemini_acknowledgement") == 1
    assert [row["source"] for row in second["trace"]].count("kemini_control") == 1
    assert second["messages"][: sources.index("history")] == first["messages"][: sources.index("history")]
    story.cfg.kemini.filler_text = ""
    unfilled = story.compile()
    assert unfilled["messages"][0]["role"] == "system"
    assert next(row for row in unfilled["trace"] if row["source"] == "state")["role"] == "user"
    story.cfg.kemini.enabled = False
    disabled = story.compile()
    assert not any(row["source"].startswith("kemini_") for row in disabled["trace"])
    assert next(row for row in disabled["trace"] if row["source"] == "state")["role"] == "system"
    assert story.cfg.message_adapter.mappings["gemini"]["state"].role == "system"


def test_dynamic_sources_retain_prefix_and_compare_current_versions():
    story = Story()
    first = story.compile()
    for source, tag in (
        ("author_note", "author_note"),
        ("post", "card_post_history_instructions"),
        ("state", "tavern_state"),
    ):
        content = next(m["content"] for m in first["trace"] if m["source"] == source)
        assert content.startswith(f"<{tag}>\n[当前") and content.endswith(f"\n</{tag}>")
        assert content.count(f"<{tag}>") == content.count(f"</{tag}>") == 1
    story.commit(first)
    second = story.compile()
    assert second["context_ledger"]["rebuild_reason"] is None
    assert second["messages"][: len(first["messages"])] == first["messages"]
    assert second["context_ledger"]["updates"] == []
    story.commit(second)
    for note in ("作者注 B", "作者注 A", ""):
        story.note = note
        current = story.compile()
        assert current["context_ledger"]["rebuild_reason"] is None
        assert current["context_ledger"]["updates"] == [{"source": "author_note", "cleared": not bool(note)}]
        content = [m["content"] for m in current["trace"] if m["source"] == "author_note"][-1]
        assert content.startswith("<author_note>\n") and content.endswith("\n</author_note>")
        story.commit(current)
    unchanged = story.compile()
    assert unchanged["context_ledger"]["updates"] == []
    story.snapshot["state"]["stat_data"]["hp"] = 4
    changed = story.compile()
    assert changed["context_ledger"]["updates"] == [{"source": "state", "cleared": False}]
    assert sum("角色后置要求" in m["content"] for m in changed["messages"]) == 1
    story.commit(changed)
    story.document["post_history_instructions"] = ""
    cleared = story.compile()
    assert cleared["context_ledger"]["updates"] == [{"source": "post", "cleared": True}]
    content = [m["content"] for m in cleared["trace"] if m["source"] == "post"][-1]
    assert content.startswith("<card_post_history_instructions>\n[此前")
    assert content.endswith("\n</card_post_history_instructions>")


@pytest.mark.parametrize("api", ["anthropic", "responses"])
@pytest.mark.parametrize("removal", ["disabled", "condition"])
async def test_state_block_revokes_once_and_reopening_appends_even_the_same_value(api, removal):
    story = Story(api)
    block = next(b for b in story.session["recipe"]["blocks"] if b["source"] == "state")
    if removal == "condition":
        block["condition"] = "stat_data.hp"
    provider = ModelTransport(story.cfg, "fixture-secret")

    def request_rows(context):
        body = provider._request(context["messages"])
        return body["messages" if api == "anthropic" else "input"]

    try:
        first = story.compile()
        prefix = request_rows(first)
        story.commit(first)
        # A hidden state change must not leave the previously sent value current.
        story.snapshot["state"]["stat_data"]["hp"] = 0
        if removal == "disabled":
            block["enabled"] = False
        closed = story.compile()
        assert closed["context_ledger"]["rebuild_reason"] is None
        assert closed["context_ledger"]["updates"] == [{"source": "state", "cleared": True}]
        frames = [m["content"] for m in closed["trace"] if m["source"] == "state"]
        assert len(frames) == 2
        assert frames[-1].startswith("<tavern_state>\n[此前剧情状态")
        assert frames[-1].endswith("已撤销；旧版本不再作为当前要求或状态。]\n</tavern_state>")
        rows = request_rows(closed)
        assert rows[: len(prefix)] == prefix
        assert "已撤销" in json.dumps(rows[-1], ensure_ascii=False)
        assert json.dumps(rows, ensure_ascii=False).count("已撤销") == 1
        story.commit(closed)

        for _ in range(2):
            still_closed = story.compile()
            assert still_closed["context_ledger"]["updates"] == []
            later = request_rows(still_closed)
            assert later[: len(rows)] == rows
            assert json.dumps(later, ensure_ascii=False).count("已撤销") == 1
            rows = later
            story.commit(still_closed)

        # Restoring the old value still needs a new frame after its revocation.
        story.snapshot["state"]["stat_data"]["hp"] = 3
        block["enabled"] = True
        reopened = story.compile()
        assert reopened["context_ledger"]["rebuild_reason"] is None
        assert reopened["context_ledger"]["updates"] == [{"source": "state", "cleared": False}]
        later = request_rows(reopened)
        assert later[: len(rows)] == rows
        assert json.dumps({"stat_data": {"hp": 3}}) in reopened["messages"][-1]["content"]
        assert "当前剧情状态" in json.dumps(later[-1], ensure_ascii=False)
        assert "已撤销" not in json.dumps(later[-1], ensure_ascii=False)
        story.commit(reopened)
        assert story.compile()["context_ledger"]["updates"] == []
    finally:
        await provider.close()


@pytest.mark.parametrize("api", ["anthropic", "responses"])
@pytest.mark.parametrize("empty_state", [{}, {"stat_data": {}}])
async def test_empty_state_replaces_prior_values_and_restoring_appends_once(api, empty_state):
    story = Story(api)
    original = copy.deepcopy(story.snapshot["state"])
    provider = ModelTransport(story.cfg, "fixture-secret")

    def request_rows(context):
        body = provider._request(context["messages"])
        return body["messages" if api == "anthropic" else "input"]

    try:
        first = story.compile()
        prefix = request_rows(first)
        story.commit(first)
        story.snapshot["state"] = copy.deepcopy(empty_state)
        cleared = story.compile()
        assert cleared["context_ledger"]["rebuild_reason"] is None
        assert cleared["context_ledger"]["updates"] == [{"source": "state", "cleared": False}]
        content = cleared["messages"][-1]["content"]
        assert "取代此前同来源版本" in content
        assert content.endswith(json.dumps(empty_state) + "\n</tavern_state>")
        assert "hp" not in content
        rows = request_rows(cleared)
        assert rows[: len(prefix)] == prefix
        assert "hp" not in json.dumps(rows[-1], ensure_ascii=False)
        story.commit(cleared)

        unchanged = story.compile()
        assert unchanged["context_ledger"]["updates"] == []
        assert request_rows(unchanged)[: len(rows)] == rows
        story.commit(unchanged)
        story.snapshot["state"] = original
        restored = story.compile()
        assert restored["context_ledger"]["updates"] == [{"source": "state", "cleared": False}]
        assert restored["messages"][-1]["content"].endswith(json.dumps(original) + "\n</tavern_state>")
        assert "当前剧情状态" in json.dumps(request_rows(restored)[-1], ensure_ascii=False)
        story.commit(restored)
        assert story.compile()["context_ledger"]["updates"] == []
    finally:
        await provider.close()


@pytest.mark.parametrize("change", ["history", "fixed", "depth", "order", "window", "format"])
def test_rebuild_preserves_compiler_positions_and_drops_obsolete_frames(change):
    story = Story()
    first = story.compile()
    story.commit(first)
    options = {}
    if change == "history":
        story.history[0]["model"] = "已编辑开场"
    elif change == "fixed":
        story.document["description"] = "已编辑固定资料"
    elif change == "depth":
        options["generate"] = {"injects": [{"role": "system", "content": "深度规则", "depth": 2}]}
    elif change == "order":
        options["generate"] = {"ordered_prompts": ["chat_history", "character", "user_input"]}
    elif change == "format":
        for record in story.records.values():
            record["version"] = 1
            for frame in record["frames"]:
                frame["content"] = "OLD_UNTAGGED_FRAME"
    else:
        from tavern.memories.memory import fingerprint as history_hash

        story.snapshot["context_window"] = {
            "covered_count": 1,
            "fingerprint": history_hash(story.history[:1]),
        }
    current = story.compile(**options)
    assert current["context_ledger"]["rebuild_reason"]
    assert all("OLD_UNTAGGED_FRAME" not in m["content"] for m in current["messages"])
    assert sum("当前剧情状态：" in m["content"] for m in current["messages"]) <= 1
    if change == "depth":
        positioned = next(i for i, m in enumerate(current["trace"]) if m["source"] == "inject")
        latest = next(i for i, m in enumerate(current["trace"]) if m.get("message_id") == story.input_id)
        assert positioned < latest


@pytest.mark.parametrize("api", ["anthropic", "responses"])
def test_native_footer_projection_edits_and_model_switch_do_not_resurrect(api):
    story = Story(api)
    first = story.compile()
    raw = "正文<tavern_retrieval>hidden-query</tavern_retrieval>"
    native = attachment(reply(api, blocks(api, raw)), response_origin(story.cfg), {"content": raw})
    output = story.commit(first, native=native)
    output["display"] = "显示正则结果"
    second = story.compile()
    assert any(m.get("_native_response") == native for m in second["messages"])
    assert all("hidden-query" not in m["content"] for m in second["trace"])
    assert all("hidden-query" not in m["model"] for m in story.history)
    story.commit(second)
    story.cfg.model += "-other"
    switched = story.compile()
    assert not any(m.get("_native_response") for m in switched["messages"])
    story.commit(switched)
    story.cfg.model = story.cfg.model.removesuffix("-other")
    assert not any(m.get("_native_response") for m in story.compile()["messages"])
    output["model"] = "用户编辑正文"
    edited = story.compile()
    assert any(m["content"] == "用户编辑正文" for m in edited["messages"])


def test_inflight_tool_updates_append_after_results_without_rewriting_old_prefix():
    story = Story()
    context = compile_context(
        story.document, story.session, story.history, story.snapshot, "继续", story.cfg, "one"
    )
    first = apply_context(
        context, {}, story.session, story.history, story.snapshot, story.cfg, purpose="maintenance"
    )
    prefix = copy.deepcopy(first["messages"])
    previous, sent = copy.deepcopy(first["half_agent_record"]), copy.deepcopy(first["trace"])
    continuation = [
        {
            "role": "assistant",
            "content": "调用",
            "tool_calls": [{"id": "call1", "function": {"name": "write_memory", "arguments": "{}"}}],
        },
        {"role": "tool", "tool_call_id": "call1", "content": "成功"},
    ]
    story.snapshot["state"]["stat_data"]["hp"] = 8
    fresh = compile_context(
        story.document, story.session, story.history, story.snapshot, "继续", story.cfg, "one"
    )
    second = apply_context(
        fresh,
        {},
        story.session,
        story.history,
        story.snapshot,
        story.cfg,
        purpose="maintenance",
        previous_record=previous,
        sent=sent,
        continuation=continuation,
    )
    assert second["messages"][: len(prefix)] == prefix
    assert second["messages"][len(prefix) : len(prefix) + 2] == continuation
    assert second["messages"][-1]["content"].endswith('{"stat_data": {"hp": 8}}\n</tavern_state>')


def test_memory_macro_conditions_and_turn_scoping_are_independent():
    story = Story()
    story.note = "给 {{user}} 的作者注"
    first = story.compile(turn_instruction="本轮提醒")
    assert any("给 用户 的作者注" in m["content"] for m in first["messages"])
    story.commit(first)
    second = story.compile(turn_instruction="本轮提醒")
    assert second["context_ledger"]["updates"] == [{"source": "turn_instruction", "cleared": False}]
    story.commit(second)
    from tests.support.memory_fixtures import memory_snapshot

    story.snapshot.update(memory_snapshot("新记忆"))
    third = story.compile()
    assert {u["source"] for u in third["context_ledger"]["updates"]} == {"memory", "turn_instruction"}
    story.commit(third)
    assert story.compile()["context_ledger"]["updates"] == []
    block = next(b for b in story.session["recipe"]["blocks"] if b["source"] == "author_note")
    block["condition"] = "stat_data.show_note"
    removed = story.compile()
    assert {"source": "author_note", "cleared": True} in removed["context_ledger"]["updates"]


@pytest.mark.parametrize("edited", ["model", "script", "macro"])
def test_processed_model_text_retires_native_without_losing_current_text(edited):
    story = Story()
    first = story.compile()
    clean = "正文 {{user}}" if edited == "macro" else "正文"
    native = attachment(
        reply("anthropic", blocks("anthropic", clean)), response_origin(story.cfg), {"content": clean}
    )
    output = story.commit(first, clean, native, model="正则修改正文" if edited == "model" else None)
    if edited == "script":
        output["raw"] = output["model"] = "脚本改写正文"
    compiled = story.compile()
    assert not any(m.get("_native_response") for m in compiled["messages"])
    assert compiled["context_ledger"]["native_replay_invalidations"][0]["reason"] == "message_changed"


async def test_durable_record_commits_only_with_story_and_survives_backup(tmp_path):
    from tavern.storage.backup import backup, restore

    store = Store(tmp_path / "game")
    card = store.import_card(b'{"name":"fixture","first_mes":"opening"}', "card.json")
    session = store.new_session(SessionCreate(card_id=card["id"], policy="preview"))
    service = RoleplayRuntime(store)

    def staged(operation):
        tid, _ = store.create_turn(
            session["id"], TurnRequest(text="继续", operation_id=operation, expected_revision=0)
        )
        uid, aid = uuid4().hex, uuid4().hex
        context = compile_context(
            store.session_card(session["id"])["document"],
            session,
            session["messages"],
            session["snapshot"],
            "继续",
            store.settings(),
            tid,
            input_message_id=uid,
            replay_records={},
        )
        user = {
            "id": uid,
            "parent": session["head"],
            "role": "user",
            "raw": "继续",
            "model": "继续",
            "display": "继续",
            "snapshot": session["snapshot"],
        }
        output = {
            **user,
            "id": aid,
            "parent": uid,
            "role": "assistant",
            "raw": "正文",
            "model": "正文",
            "display": "正文",
        }
        pending = {
            "messages": [user, output],
            "before": session["snapshot"],
            "half_agent_record": finalize_record(context["half_agent_record"], None, "正文", output),
        }
        store.set_turn(tid, status="awaiting_review", pending=json.dumps(pending))
        return tid

    try:
        rejected = staged("rejected")
        assert load_trace(store, session["id"], session["messages"]) == {}
        service.review(rejected, False)
        accepted = staged("accepted")
        service.review(accepted, True)
        session = store.session(session["id"])
        original = load_trace(store, session["id"], session["messages"])
        assert len(original) == 1
        store.commit_turn(accepted, {})  # Idempotent terminal operation.
        archive = tmp_path / "half.zip"
        backup(store, archive)
        restored = restore(archive, tmp_path / "restored")
        assert load_trace(restored, session["id"], restored.session(session["id"])["messages"]) == original
        previous = store.update_session(session["id"], session["revision"], head=session["messages"][0]["id"])
        assert load_trace(store, session["id"], previous["messages"]) == {}
    finally:
        await service.close()


@pytest.mark.parametrize("api", ["anthropic", "responses"])
async def test_real_streaming_writer_persists_replays_and_forks(tmp_path, monkeypatch, api):
    import tavern.scripting.agent as agent_module

    store = Store(tmp_path)
    settings = store.settings_document()
    settings.full_agent.model.api_mode = api
    settings.half_agent.foreground.api_mode = api
    settings.half_agent.background.api_mode = api
    settings.shared.card_author.api_mode = api
    settings.shared.providers[0].api_key = "fixture-secret"
    settings.shared.providers[0].base_url = "https://fixture.test"
    settings.shared.network.api_proxy = False
    settings.half_agent.foreground.model = "claude-opus-5-5" if api == "anthropic" else "gpt-6-sol"
    settings.half_agent.foreground.reasoning_effort = "high"
    settings.half_agent.chunk_mode = "off"
    settings.half_agent.memory_interval_turns = 100
    store.save_settings(settings)
    card = store.import_card(
        json.dumps(
            {
                "name": "测试",
                "first_mes": "开场",
                "description": "固定",
                "post_history_instructions": "后置规则",
            }
        ).encode(),
        "card.json",
    )
    session = store.new_session(SessionCreate(card_id=card["id"]))
    service = RoleplayRuntime(store)
    requests = []
    fail = False
    raw = "完整正文<tavern_retrieval>隐藏线索</tavern_retrieval>"
    parts = blocks(api, raw)

    def route(request):
        requests.append(request)
        if fail:
            return httpx.Response(
                200, content='data: {"type":"ping"}\n\n', headers={"content-type": "text/event-stream"}
            )
        return sse(api, reply(api, parts))

    async def factory(cfg, capture=None, **kwargs):
        return ModelTransport(
            cfg, cfg.api_key, capture, http_client=httpx.AsyncClient(transport=httpx.MockTransport(route))
        )

    monkeypatch.setattr(agent_module, "make_provider", factory)
    try:
        for number in range(2):
            turn = service.submit(
                session["id"],
                TurnRequest(text="继续", operation_id=str(number), expected_revision=session["revision"]),
            )
            await service.tasks[turn["id"]]
            assert store.turn(turn["id"])["status"] == "committed", store.turn(turn["id"])["error"]
            session = store.session(session["id"])
        assert all(m["model"] == m["raw"] == "完整正文" for m in session["messages"][2::2])
        body = json.loads(requests[-1].content)
        if api == "anthropic":
            assert (
                next(
                    m["content"]
                    for m in body["messages"]
                    if m["role"] == "assistant" and any(p.get("type") == "thinking" for p in m["content"])
                )
                == parts
            )
        else:
            assert [i for i in body["input"] if i.get("id") in ("rs_opaque", "msg_opaque")] == parts
        header = "x-claude-code-session-id" if api == "anthropic" else "session-id"
        assert requests[0].headers[header] == requests[1].headers[header] == str(UUID(session["id"]))
        assert "x-codex-turn-state" not in requests[-1].headers
        record = load_trace(Store(tmp_path), session["id"], session["messages"])
        assert len(record) == 2
        fork = store.fork_session(session["id"], session["head"], session["revision"])
        cloned = load_trace(store, fork["id"], fork["messages"])
        assert len(cloned) == 2 and not set(cloned) & set(record)
        assert all(r["window"] == "forked" for r in cloned.values())
        assert OperationContexts(store, fork["id"], "turn").new().session_id != requests[-1].headers[header]
        cfg = store.settings().for_role("foreground")
        preview = compile_context(
            store.session_card(session["id"])["document"],
            session,
            session["messages"],
            session["snapshot"],
            "继续",
            cfg,
            "preview",
            replay_records=record,
        )
        assert preview["context_ledger"]["rebuild_reason"] is None
        assert sum("后置规则" in m["content"] for m in preview["messages"]) == 1
        fail = True
        failed = service.submit(
            session["id"],
            TurnRequest(text="继续", operation_id="failed", expected_revision=session["revision"]),
        )
        await service.tasks[failed["id"]]
        assert store.turn(failed["id"])["status"] == "error"
        assert store.session(session["id"])["head"] == session["head"]
        assert load_trace(store, session["id"], session["messages"]) == record
        store.delete_session(fork["id"])
        with store.connection() as db:
            assert (
                db.execute("SELECT count(*) FROM half_agent_trace WHERE session=?", (fork["id"],)).fetchone()[
                    0
                ]
                == 0
            )
        # The global switch only permits Kemini conversations; ordinary ones stay
        # untouched. A Kemini final call never enters the opaque-replay path.
        settings.shared.kemini.enabled = True
        settings.shared.kemini.normal_disclaimer_enabled = True
        store.save_settings(settings)
        ordinary = session
        session = store.new_session(SessionCreate(card_id=card["id"], kemini=True))
        assert store.session_settings(session).kemini.enabled
        assert not store.session_settings(ordinary).kemini.enabled
        fail = False
        carried = "<thinking>实验辅助内容</thinking>实验正文<disclaimer>附加声明</disclaimer><tavern_retrieval>线索</tavern_retrieval>"
        if api == "anthropic":
            parts = [
                {
                    "type": "tool_use",
                    "id": "reply_call",
                    "name": "kemini_emit_story",
                    "input": {"content": carried},
                }
            ]
        else:
            parts = [
                {
                    "type": "function_call",
                    "id": "reply_item",
                    "call_id": "reply_call",
                    "name": "kemini_emit_story",
                    "arguments": json.dumps({"content": carried}),
                }
            ]
        experimental = service.submit(
            session["id"],
            TurnRequest(text="继续", operation_id="kemini", expected_revision=session["revision"]),
        )
        await service.tasks[experimental["id"]]
        assert store.turn(experimental["id"])["status"] == "committed", store.turn(experimental["id"])[
            "error"
        ]
        session = store.session(session["id"])
        final = session["messages"][-1]
        assert final["raw"] == final["model"] == "实验正文"
        assert final["auxiliary"] == [
            {"kind": "thinking", "text": "实验辅助内容"},
            {"kind": "disclaimer", "text": "附加声明"},
        ]
        record = load_trace(store, session["id"], session["messages"])
        assert "response" not in record[final["id"]]
        cfg = store.settings().for_role("foreground")
        preview = compile_context(
            store.session_card(session["id"])["document"],
            session,
            session["messages"],
            session["snapshot"],
            "继续",
            cfg,
            "preview",
            replay_records=record,
        )
        assert "实验辅助内容" not in str(preview["messages"])
        cloned = store.fork_session(session["id"], session["head"], session["revision"])
        assert cloned["messages"][-1]["auxiliary"] == final["auxiliary"]
        edited = store.edit_message(session["id"], final["id"], "用户编辑正文", session["revision"])
        assert edited["messages"][-1]["auxiliary"] == final["auxiliary"]
    finally:
        await service.close()


@pytest.mark.parametrize("api", ["anthropic", "responses"])
async def test_background_native_tool_loop_routes_only_within_operation(tmp_path, api):
    story = Story(api)
    store = Store(tmp_path)
    contexts = OperationContexts(store, story.session["id"], "turn")
    context = contexts.new("memory")
    requests, events = [], []
    name = HISTORY_TOOL["function"]["name"]
    parts = blocks(api, "核对")
    call = (
        {"type": "tool_use", "id": "tool1", "name": name, "input": {"query": "银戒"}}
        if api == "anthropic"
        else {
            "type": "function_call",
            "id": "fc1",
            "call_id": "tool1",
            "name": name,
            "arguments": '{"query":"银戒"}',
        }
    )
    parts.insert(1, call)

    def route(request):
        requests.append(request)
        return httpx.Response(
            200,
            json=reply(api, parts if len(requests) == 1 else blocks(api, "完成")),
            headers={"x-codex-turn-state": "memory-route"},
        )

    provider = ModelTransport(
        story.cfg, story.cfg.api_key, http_client=httpx.AsyncClient(transport=httpx.MockTransport(route))
    )
    configure_operation(provider, context, lambda k, p: events.append((k, p)), "memory")
    try:
        result = await complete_with_recall(
            provider,
            recall_request(),
            history=recall_history(),
            settings=story.cfg,
            budget=HistoryBudget(),
            phase="memory",
        )
        assert str(result) == "完成"
        body = json.loads(requests[-1].content)
        if api == "anthropic":
            assert body["messages"][-2]["content"] == parts
        else:
            assert body["input"][-4:-1] == parts
            assert requests[1].headers["x-codex-turn-state"] == "memory-route"
        assert len(events) == 2
        assert context.session_id != str(UUID(story.session["id"]))
        assert contexts.new("memory").session_id == context.session_id
        assert contexts.new("memory").routing == {}
        assert contexts.new("variables").session_id != context.session_id
    finally:
        await provider.close()


@pytest.mark.parametrize("api", ["anthropic", "responses"])
async def test_maintenance_framework_preserves_native_blocks_and_uses_separate_identity(tmp_path, api):
    from tavern.scripting.agent import Writer

    store = Store(tmp_path)
    cfg = Story(api).cfg
    settings = store.settings_document()
    settings.full_agent.model.api_mode = api
    settings.half_agent.foreground.api_mode = api
    settings.half_agent.background.api_mode = api
    settings.shared.card_author.api_mode = api
    settings.shared.providers[0].base_url = "https://fixture.test"
    settings.shared.providers[0].api_key = cfg.api_key
    settings.shared.network.api_proxy = False
    settings.half_agent.background.model = cfg.model
    settings.half_agent.background.reasoning_effort = "high"
    store.save_settings(settings)
    card = store.import_card(b'{"name":"fixture","first_mes":"opening"}', "card.json")
    session = store.new_session(SessionCreate(card_id=card["id"]))
    req = TurnRequest(
        text="查看资料", operation_id="maintenance", expected_revision=0, allow_content_edit=True
    )
    tid, _ = store.create_turn(session["id"], req)
    store.set_turn(tid, status="running")
    writer = Writer(
        store,
        tid,
        card["document"],
        session,
        session["messages"],
        copy.deepcopy(session["snapshot"]),
        req,
        None,
    )
    requests = []
    parts = blocks(api, "先查资料")
    tool = (
        {"type": "tool_use", "id": "tool1", "name": "read_character", "input": {"field": "name"}}
        if api == "anthropic"
        else {
            "type": "function_call",
            "id": "fc1",
            "call_id": "tool1",
            "name": "read_character",
            "arguments": '{"field":"name"}',
        }
    )
    parts.insert(1, tool)

    def route(request):
        requests.append(request)
        payload = reply(api, parts if len(requests) == 1 else blocks(api, "维护完成"))
        return (
            sse(api, payload)
            if json.loads(request.content).get("stream")
            else httpx.Response(200, json=payload)
        )

    provider = ModelTransport(
        cfg, cfg.api_key, http_client=httpx.AsyncClient(transport=httpx.MockTransport(route))
    )
    try:
        assert await writer._run(provider, maintenance=True) == "维护完成"
        assert len(requests) == 2
        body = json.loads(requests[-1].content)
        if api == "anthropic":
            assert body["messages"][-2]["content"] == parts
        else:
            assert body["input"][-4:-1] == parts
        header = "x-claude-code-session-id" if api == "anthropic" else "session-id"
        assert requests[0].headers[header] == requests[1].headers[header] != str(UUID(session["id"]))
        assert load_trace(store, session["id"], session["messages"]) == {}
    finally:
        await provider.close()
