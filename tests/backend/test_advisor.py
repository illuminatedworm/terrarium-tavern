"""Advisor authorization, complete context, branch ownership and recovery."""

import copy
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from tavern.advisor.context import recent_history
from tavern.advisor.runtime import ConsultationRequest, OptionsRequest
from tavern.application.api import create_app
from tavern.core.models import SessionCreate
from tavern.storage.store import dump, uid
from tests.backend.test_card_author import ToolLLM, import_fixture


def setup(tmp_path, mode="full_agent"):
    app = create_app(tmp_path)
    store = app.state.store
    card = import_fixture(store)
    session = store.new_session(SessionCreate(card_id=card["id"], agent_mode=mode))
    return app, store, session, app.state.advisor


async def ask(advisor, sid, provider, text="现在应该怎么办？", resume=None):
    advisor.provider_factory = lambda: provider
    run = advisor.submit(
        sid,
        ConsultationRequest(
            text=text,
            expected_revision=advisor.data.get(sid)["revision"],
            operation_id=uid(),
            resume_run_id=resume,
        ),
    )
    await advisor.tasks[run["id"]]
    return advisor.data.run(run["id"])


@pytest.mark.parametrize("mode", ["full_agent", "half_agent"])
async def test_readonly_chat_and_complete_history(tmp_path, mode):
    _, store, session, advisor = setup(tmp_path, mode)
    before = copy.deepcopy(store.session(session["id"]))
    provider = ToolLLM([("read_history", {"ref": "H1"})])
    result = await ask(advisor, session["id"], provider)
    assert result["status"] == "completed", result
    after = store.session(session["id"])
    assert before["snapshot"] == after["snapshot"]
    assert before["messages"] == after["messages"]
    assert before["revision"] == after["revision"]
    assert len(advisor.public(session["id"])["exchanges"]) == 1
    text = dump(provider.call_log)
    assert "开场一" in text and "handoff" in text and "plot" in text
    assert session["id"] not in text and session["head"] not in text
    names = {d.name for d in provider.schemas[0]}
    assert not names & {"write_handoff", "write_note", "write_plot", "patch_state", "revise_content"}
    assert ("search_memory" in names) == (mode == "half_agent")


async def test_history_spans_return_complete_messages_and_enforce_the_shared_limit():
    from tavern.advisor.tools import AdvisorTools, definitions
    from tavern.core.model_view import ModelView

    messages = [
        {
            "id": uid(),
            "role": "assistant" if index % 2 == 0 else "user",
            "raw": f"原文 {index + 1}：" + "完整剧情" * 600 + "保留尾部",
        }
        for index in range(9)
    ]
    story = {
        "agent_mode": "half_agent",
        "document": {},
        "persona": "用户",
        "snapshot": {},
        "messages": messages,
    }
    before = copy.deepcopy(story)
    tools = AdvisorTools(
        SimpleNamespace(story=story, definitions=definitions("half_agent"), view=ModelView(story))
    )
    result = await tools.execute("read_history", '{"ref":"H1-H8"}')
    assert result == {
        "messages": [
            {"ref": f"H{index + 1}", "role": message["role"], "text": message["raw"]}
            for index, message in enumerate(messages[:8])
        ]
    }
    assert await tools.execute("read_history", '{"ref":"H1"}') == result["messages"][0]
    with pytest.raises(ValueError, match="最多读取 8 条"):
        await tools.execute("read_history", '{"ref":"H1-H9"}')
    with pytest.raises(ValueError, match="没有此历史引用或区间"):
        await tools.execute("read_history", '{"ref":"H8-H10"}')
    assert story == before


async def test_full_agent_reads_validate_versions_without_mutating_the_snapshot():
    from tavern.advisor.config import AdvisorConfig
    from tavern.advisor.context import background
    from tavern.advisor.tools import AdvisorTools, ContextRead
    from tavern.full_agent.state import new_state

    story = {
        "document": {"name": "Guide"},
        "persona": "User",
        "snapshot": {"full_agent": new_state()},
        "agent_mode": "full_agent",
        "messages": [],
        "turn_preference": "",
        "context_strategy": "new_context",
    }
    tools = AdvisorTools(SimpleNamespace(story=story))
    args = ContextRead(field="handoff")
    assert await tools.dispatch("read_story_context", args) == {"available": True, "content": ""}
    state = story["snapshot"]["full_agent"]
    state["handoff"]["current"] = [{"text": "旧交接完整保留", "kind": "fact", "refs": []}]
    saved = copy.deepcopy(story["snapshot"])
    assert "旧交接完整保留" in str(background(story, AdvisorConfig()))
    assert "旧交接完整保留" in (await tools.dispatch("read_story_context", args))["content"]
    assert story["snapshot"] == saved

    state.update(version=1, handoff={"text": "旧格式", "revision": 0, "stale": False})
    with pytest.raises(ValueError, match="格式版本 1 不受支持"):
        background(story, AdvisorConfig())
    with pytest.raises(ValueError, match="格式版本 1 不受支持"):
        await tools.dispatch("read_story_context", args)


async def test_preference_commits_and_respects_latest_revision(tmp_path):
    _, store, session, advisor = setup(tmp_path)
    provider = ToolLLM(
        [("update_turn_preference", {"action": "append", "content": "多写对话", "request_ref": "Q1"})]
    )
    result = await ask(advisor, session["id"], provider, "把多写对话加入本轮偏好")
    assert result["status"] == "completed", result
    assert store.session(session["id"])["turn_preference"] == "多写对话"
    assert store.session(session["id"])["messages"] == session["messages"]


async def test_offers_are_visible_calls_without_story_writes(tmp_path):
    _, store, session, advisor = setup(tmp_path)
    provider = ToolLLM(
        [
            ("read_story_context", {"field": "title"}),
            ("offer_titles", {"titles": [" 雨夜来客 "], "request_ref": "Q1"}),
            ("offer_inputs", {"options": []}),
        ]
    )
    result = await ask(advisor, session["id"], provider, "给对话起几个标题")
    assert result["status"] == "completed", result
    after = store.session(session["id"])
    assert (after["title"], after["revision"]) == (session["title"], session["revision"])
    trace = advisor.public(session["id"])["exchanges"][0]["trace"]
    assert session["title"] in trace[1]["content"]
    assert "error" not in trace[3]["content"] and "error" in trace[5]["content"]

    # Prose beside tool calls stays in the reply; only the native end completes
    # it, and that last response may be empty once a reply exists.
    class ProseBesideTools(ToolLLM):
        async def chat(self, messages, **kwargs):
            async for _ in super().chat(messages, **kwargs):
                yield "可以先推门。" if self.native else ""

    provider = ProseBesideTools([("offer_inputs", {"options": ["推门进去"]})])
    result = await ask(advisor, session["id"], provider, "下一步？")
    assert (result["status"], provider.call_count) == ("completed", 2), result
    assert advisor.public(session["id"])["exchanges"][1]["response"] == "可以先推门。"


class Failure(ToolLLM):
    async def chat(self, messages, **kwargs):
        if self.call_count:
            raise RuntimeError("断线")
        async for chunk in super().chat(messages, **kwargs):
            yield chunk


async def test_failed_preference_is_staged_and_resume_is_exactly_once(tmp_path):
    _, store, session, advisor = setup(tmp_path)
    failed = await ask(
        advisor,
        session["id"],
        Failure([("update_turn_preference", {"action": "append", "content": "悬念", "request_ref": "Q1"})]),
        "加入偏好：悬念",
    )
    assert failed["resumable"]
    assert store.session(session["id"])["turn_preference"] == ""
    completed = await ask(advisor, session["id"], ToolLLM([]), "", failed["id"])
    assert completed["status"] == "completed", completed
    assert store.session(session["id"])["turn_preference"] == "悬念"
    assert len(advisor.public(session["id"])["exchanges"]) == 1


async def test_discard_never_applies_staged_preference(tmp_path):
    app, store, session, advisor = setup(tmp_path)
    await ask(
        advisor,
        session["id"],
        Failure(
            [("update_turn_preference", {"action": "replace", "content": "旧提案", "request_ref": "Q1"})]
        ),
        "替换偏好",
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1") as client:
        response = await client.post(
            f"/api/sessions/{session['id']}/advisor/discard",
            json={
                "expected_revision": advisor.data.get(session["id"])["revision"],
            },
        )
        assert response.status_code == 200, response.text
    assert store.session(session["id"])["turn_preference"] == ""
    assert not advisor.public(session["id"])["run"]["resumable"]


async def test_runtime_denies_unlisted_writes(tmp_path):
    _, store, session, advisor = setup(tmp_path)
    provider = ToolLLM([("write_plot", {"content": "被修改"}), ("patch_state", {"operations": []})])
    result = await ask(advisor, session["id"], provider)
    assert result["status"] == "completed"
    assert "没有此工具权限" in dump(provider.call_log)
    assert store.session(session["id"])["snapshot"] == session["snapshot"]


def test_recent_range_zero_and_complete_exchanges(tmp_path):
    _, store, session, advisor = setup(tmp_path)
    assert recent_history(session["messages"], 0) == []
    synthetic = [{"role": "assistant", "raw": "开场"}]
    for i in range(5):
        synthetic += [{"role": "user", "raw": f"问{i}"}, {"role": "assistant", "raw": f"答{i}"}]
    assert [m["raw"] for m in recent_history(synthetic, 3)] == ["问2", "答2", "问3", "答3", "问4", "答4"]
    advisor.options(session["id"], OptionsRequest(expected_revision=0, recent_exchanges=0))
    assert not advisor.preview(session["id"])["recent_refs"]
    assert advisor.public(session["id"])["effective"]["range_source"] == "story"
    revision = advisor.data.get(session["id"])["revision"]
    advisor.options(session["id"], OptionsRequest(expected_revision=revision, recent_exchanges=None))
    assert advisor.public(session["id"])["effective"]["recent_exchanges"] == 3


async def test_long_source_reaches_final_request_without_ids(tmp_path):
    _, store, session, advisor = setup(tmp_path)
    text = "世界资料" * 10000 + "尾部必须完整"
    with store.connection() as db:
        db.execute("UPDATE messages SET raw=?,model=? WHERE id=?", (text, text, session["head"]))
    provider = ToolLLM([("read_history", {"ref": "H1"})])
    result = await ask(advisor, session["id"], provider)
    assert result["status"] == "completed", result
    assert text in dump(provider.call_log[-1])


async def test_branch_and_delete_lifecycle(tmp_path):
    _, store, session, advisor = setup(tmp_path)
    await ask(advisor, session["id"], ToolLLM([]))
    fork = store.fork_session(session["id"], session["head"], session["revision"])
    assert not advisor.public(fork["id"])["exchanges"]
    await ask(advisor, fork["id"], ToolLLM([]), "分支独立问题")
    assert len(advisor.public(session["id"])["exchanges"]) == 1
    store.delete_session(fork["id"])
    with store.connection() as db:
        assert not db.execute("SELECT 1 FROM advisor_exchanges WHERE session=?", (fork["id"],)).fetchone()


async def test_api_exposes_readonly_preview_and_settings(tmp_path):
    app, _, session, advisor = setup(tmp_path)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.get(f"/api/sessions/{session['id']}/advisor")
        assert response.status_code == 200
        result = (await client.get("/api/settings")).json()
        assert result["shared"]["advisor"]["recent_exchanges"] == 3
        assert any(p["path"] == "advisor.system_prompt" for p in result["prompt_definitions"])
        assert (await client.get(f"/api/sessions/{session['id']}/advisor/preview")).status_code == 200
    assert advisor.data.get(session["id"])["revision"] == 0
