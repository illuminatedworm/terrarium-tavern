from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from tavern.application.api import create_app
from tavern.conversation.context import compile_context
from tavern.core.models import ContextRecipe, Settings
from tavern.core.prompts import (
    DEFAULT_RETRIEVAL_PROTOCOL,
    DEFAULT_SYSTEM_PROMPT,
    LEGACY_DEFAULT_RETRIEVAL_PROTOCOL,
    PROMPT_DEFINITIONS,
)
from tavern.full_agent.config import FULL_AGENT_PROMPT, FullAgentConfig, tool_protocol
from tavern.memories.memory import render_memory
from tavern.search.retrieval_text import query_instruction
from tests.support.memory_fixtures import memory_snapshot


def read_path(value, path):
    for part in path.split("."):
        value = value[part]
    return value


def test_defaults_and_custom_prompts():
    assert Settings(system_prompt="自定义", memory_system_prompt="自定义记忆").system_prompt == "自定义"
    defaults = Settings().model_dump()
    for item in PROMPT_DEFINITIONS:
        assert read_path(defaults, item["path"]) == item["default"]
    assert "<tavern_memory>" in DEFAULT_SYSTEM_PROMPT
    assert "MVU" not in DEFAULT_RETRIEVAL_PROTOCOL
    assert "玩家" not in DEFAULT_SYSTEM_PROMPT + FULL_AGENT_PROMPT
    assert all("用户对行动结果的描述，不自动决定其他人物的反应" in prompt for prompt in (DEFAULT_SYSTEM_PROMPT, FULL_AGENT_PROMPT))
    assert "玩家" not in tool_protocol(FullAgentConfig(), plot_enabled=True)
    assert "每个人物都有自己想要的东西，也有自己知道和不知道的事" in FULL_AGENT_PROMPT
    assert "handoff" not in FULL_AGENT_PROMPT and "new_context" not in FULL_AGENT_PROMPT


def test_saved_legacy_retrieval_default_is_upgraded_without_changing_custom_text(tmp_path):
    from tavern.storage.store import Store

    store = Store(tmp_path)
    settings = Settings(retrieval={"protocol_prompt": LEGACY_DEFAULT_RETRIEVAL_PROTOCOL})
    with store.connection() as db:
        db.execute("UPDATE meta SET value='1' WHERE key='schema_version'")
        db.execute("UPDATE meta SET value=? WHERE key='settings'", (settings.model_dump_json(),))
    store = Store(tmp_path)
    assert store.settings().retrieval.protocol_prompt == DEFAULT_RETRIEVAL_PROTOCOL
    settings = store.settings()
    settings.retrieval.protocol_prompt = "自定义检索协议"
    store.save_settings(settings)
    assert Store(tmp_path).settings().retrieval.protocol_prompt == "自定义检索协议"


@pytest.mark.parametrize("fixture", ["advisor_prompt_before_independent.txt", "advisor_prompt_before_gui.txt", "advisor_prompt_before_agent_name.txt", "advisor_prompt_before_tag_map.txt"])
def test_advisor_default_upgrade_preserves_custom_text_and_is_idempotent(tmp_path, fixture):
    from tavern.advisor.config import SYSTEM
    from tavern.storage.store import Store

    old = (Path(__file__).parents[1] / "fixtures" / fixture).read_text("utf-8")
    store = Store(tmp_path)
    config = store.settings_document()
    config.shared.advisor.system_prompt = old
    store.save_settings(config)
    revision = store.settings_document().revisions["shared"]

    upgraded = Store(tmp_path).settings_document()
    assert upgraded.shared.advisor.system_prompt == SYSTEM
    assert upgraded.revisions["shared"] == revision + 1
    assert Store(tmp_path).settings_document() == upgraded

    # Even a small user change to the old default must survive reopening.
    custom = old + "\n\n请保留我自行补充的要求。"
    upgraded.shared.advisor.system_prompt = custom
    store.save_settings(upgraded)
    saved = store.settings_document()
    assert Store(tmp_path).settings_document() == saved
    assert saved.shared.advisor.system_prompt == custom


async def test_all_prompts_save_reload_and_restore_default(tmp_path):
    app = create_app(tmp_path)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1") as client:
            response = (await client.get("/api/settings")).json()
            assert {item["scope"] for item in response["prompt_definitions"]} == {"full_agent", "half_agent", "shared"}
            system_prompts = {item["scope"]: item for item in response["prompt_definitions"]
                              if item["path"] == "system_prompt"}
            assert system_prompts["full_agent"]["default"] == FULL_AGENT_PROMPT
            assert system_prompts["half_agent"]["default"] == DEFAULT_SYSTEM_PROMPT
            assert "叙事、连续性与可选剧情规划原则" in system_prompts["full_agent"]["description"]
            assert "工具授权、记录职责和提交规则由运行时协议提供" in system_prompts["full_agent"]["description"]
            assert "前台 Writer" in system_prompts["half_agent"]["description"]
            for scope in ("full_agent", "half_agent", "shared"):
                prompts = [item for item in response["prompt_definitions"] if item["scope"] == scope]
                changes = {item["path"]: "CUSTOM " + scope + item["path"] for item in prompts}
                saved = await client.patch(f"/api/settings/{scope}", json={"expected_revision": response["revisions"][scope], "changes": changes})
                assert saved.status_code == 200, saved.text
                response = (await client.get("/api/settings")).json()
                for item in prompts:
                    assert read_path(response[scope], item["path"]) == changes[item["path"]]
                restored = await client.patch(f"/api/settings/{scope}", json={"expected_revision": response["revisions"][scope],
                    "changes": {item["path"]: item["default"] for item in prompts}})
                assert restored.status_code == 200, restored.text
                response = restored.json()
                for item in prompts:
                    assert read_path(response[scope], item["path"]) == item["default"]
    finally:
        await app.state.service.close()


def test_custom_instructions_and_memory_reach_wire_messages():
    settings = Settings(system_prompt="RP CUSTOM", preference_prompt="PREF CUSTOM",
                        regenerate_prompt="REGEN CUSTOM", retrieval={"channels": ["keyword"],
                        "protocol_prompt": "PROTOCOL CUSTOM", "query_prompt": "QUERY CUSTOM"})
    result = compile_context({"name": "角色"}, {"id": "s", "persona": "玩家",
        "turn_preference": "放缓节奏", "recipe": ContextRecipe().model_dump()}, [],
        {"state": {}, **memory_snapshot("昨日抵达灯塔", "约定明日见面")},
        "继续", settings, "seed", turn_instruction="多用对白")
    text = "\n".join(m["content"] for m in result["messages"])
    for expected in ["RP CUSTOM", "PREF CUSTOM\n放缓节奏", "REGEN CUSTOM\n多用对白",
                     "PROTOCOL CUSTOM", "QUERY CUSTOM", "昨日抵达灯塔", "约定明日见面", "<tavern_memory>"]:
        assert expected in text
    assert render_memory({}) == ""
    assert "<tavern_retrieval>" in query_instruction(settings.retrieval)


async def test_local_prompt_overrides_reach_worker(tmp_path):
    from tavern.search.local_models import LocalModelManager
    from tavern.search.retrieval import digest
    from tavern.storage.store import Store

    manager = LocalModelManager(Store(tmp_path))
    config = Settings(local_models={"instruction_prompt": "LOCAL TASK", "reranker_prompt": "LOCAL JUDGE"})
    sent = []

    class Pipe:
        def send(self, payload):
            sent.append(payload)

    async def receive(*args):
        return {"result": [0.9]}

    manager.receive = receive
    manager.ready.add("reranker")
    manager.workers["reranker"] = (None, Pipe(), digest(config.local_models.reranker.model_dump()))
    result = await manager.infer("reranker", "rerank", [["query", "document"]], config, timeout=1)
    assert result == [0.9]
    assert sent[0]["instruction_prompt"] == "LOCAL TASK"
    assert sent[0]["reranker_prompt"] == "LOCAL JUDGE"


async def test_probe_uses_saved_prompt_overrides(tmp_path, monkeypatch):
    from types import SimpleNamespace

    import tavern.scripting.agent

    app = create_app(tmp_path)
    seen = []

    class Provider:
        async def chat_complete(self, messages):
            seen.extend(messages)
            return SimpleNamespace(content="TAVERN_MID_42")

        async def close(self):
            pass

    async def make_provider(*args):
        return Provider()

    monkeypatch.setattr(tavern.scripting.agent, "make_provider", make_provider)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1") as client:
            saved = await client.patch("/api/settings/half_agent", json={"expected_revision": 0, "changes": {
                "probe_system_prompt": "PROBE CUSTOM", "probe_marker_prompt": "MARKER CUSTOM TAVERN_MID_42"}})
            assert saved.status_code == 200
            response = await client.post("/api/settings/probe")
            assert response.status_code == 200, response.text
            assert seen[0]["content"] == "PROBE CUSTOM"
            assert seen[3]["content"] == "MARKER CUSTOM TAVERN_MID_42"
    finally:
        await app.state.service.close()
