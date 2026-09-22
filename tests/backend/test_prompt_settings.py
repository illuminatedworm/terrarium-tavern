from httpx import ASGITransport, AsyncClient

from tavern.application.api import create_app
from tavern.conversation.context import compile_context
from tavern.core.models import ContextRecipe, Settings
from tavern.core.prompts import (
    DEFAULT_SYSTEM_PROMPT,
    PROMPT_DEFINITIONS,
)
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


async def test_all_prompts_save_reload_and_restore_default(tmp_path):
    app = create_app(tmp_path)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1") as client:
            response = (await client.get("/api/settings")).json()
            assert len(response["prompt_definitions"]) == 11
            for item in response["prompt_definitions"]:
                parts = item["path"].split(".")
                owner = response if len(parts) == 1 else response[parts[0]]
                owner[parts[-1]] = "CUSTOM " + item["path"]
            saved = await client.put("/api/settings", json=response)
            assert saved.status_code == 200, saved.text
            response = (await client.get("/api/settings")).json()
            for item in response["prompt_definitions"]:
                assert read_path(response, item["path"]) == "CUSTOM " + item["path"]
                parts = item["path"].split(".")
                owner = response if len(parts) == 1 else response[parts[0]]
                owner[parts[-1]] = item["default"]
            restored = (await client.put("/api/settings", json=response)).json()
            for item in PROMPT_DEFINITIONS:
                assert read_path(restored, item["path"]) == item["default"]
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
            saved = await client.put("/api/settings", json={"probe_system_prompt": "PROBE CUSTOM",
                "probe_marker_prompt": "MARKER CUSTOM TAVERN_MID_42"})
            assert saved.status_code == 200
            response = await client.post("/api/settings/probe")
            assert response.status_code == 200, response.text
            assert seen[0]["content"] == "PROBE CUSTOM"
            assert seen[3]["content"] == "MARKER CUSTOM TAVERN_MID_42"
    finally:
        await app.state.service.close()
