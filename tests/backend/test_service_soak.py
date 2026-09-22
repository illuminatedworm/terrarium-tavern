"""Two hundred real orchestration turns, with isolated runtime and scripted LLMs."""
import asyncio
import json
from types import SimpleNamespace

from kohakuterrarium.testing.llm import ScriptedLLM

from tavern.application.service import RoleplayRuntime
from tavern.core.models import SessionCreate, Settings, TurnRequest
from tavern.storage.store import Store


async def test_two_hundred_turns_with_regeneration_branches_and_retention(tmp_path):
    store = Store(tmp_path)
    store.save_settings(Settings(history_message_cap=14, chunk_size=5, memory_interval_turns=5,
                                 retrieval={"channels": ["keyword"]}))
    card = store.import_card(json.dumps({"name": "守塔人", "first_mes": "欢迎来到海岸。"}).encode(), "card.json")
    current = store.new_session(SessionCreate(card_id=card["id"]))
    service = RoleplayRuntime(store)
    service.provider_factory = lambda: ScriptedLLM(["海风吹过石阶。"])

    class Memory:
        async def chat_complete(self, messages, **kwargs):
            return SimpleNamespace(content=json.dumps({"operations": [], "questions": []}), usage={})

        async def close(self):
            pass

    service.memory_provider_factory = Memory
    try:
        for index in range(200):
            req = TurnRequest(text="继续前进。", operation_id=f"send-{index}", expected_revision=current["revision"])
            turn = service.submit(current["id"], req)
            assert service.submit(current["id"], req)["id"] == turn["id"]
            await service.tasks[turn["id"]]
            assert store.turn(turn["id"])["status"] == "committed"
            assert sum(e["kind"] == "committed" for e in store.events(turn["id"])) == 1
            current = store.session(current["id"])
            if index % 25 == 24:
                count = len(current["messages"])
                retry = service.submit(current["id"], TurnRequest(mode="regenerate", operation_id=f"regen-{index}",
                                                                 expected_revision=current["revision"]))
                await service.tasks[retry["id"]]
                assert store.turn(retry["id"])["status"] == "committed"
                current = store.session(current["id"])
                assert len(current["messages"]) == count
                clone = store.fork_session(current["id"], current["head"], current["revision"])
                assert clone["snapshot"].get("memory_document") == current["snapshot"].get("memory_document")
                assert clone["messages"][0]["id"] != current["messages"][0]["id"]
                store.delete_session(clone["id"])
            await asyncio.sleep(0)
            assert len(service.tasks) <= 30
            assert not service.agents
        assert len(current["messages"]) == 401
        assert current["snapshot"]["context_window"]["covered_count"] > 380
    finally:
        await service.close()
