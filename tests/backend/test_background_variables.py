import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from kohakuterrarium.testing.llm import ScriptedLLM

from tavern.application.service import RoleplayRuntime
from tavern.core.models import SessionCreate, TurnRequest
from tavern.scripting.variables import analyze_variables
from tavern.storage.store import Store
from tests.support.tracker_model import PatchModel


class VariableProvider:
    def __init__(self, content='[{"op":"replace","path":"/能量","value":4}]'):
        self.content = content
        self.messages = None
        self.closed = False
        self.native = PatchModel(content)

    @property
    def last_tool_calls(self):
        return self.native.last_tool_calls

    async def chat_complete(self, messages, **kwargs):
        self.messages = messages
        if "extra_body" in kwargs:
            if self.closed:
                self.native = PatchModel(self.content)
                self.closed = False
            return await self.native.chat_complete(messages, **kwargs)
        return SimpleNamespace(content=self.content, usage={})

    async def close(self):
        self.closed = True


@pytest.mark.parametrize(
    "enabled,result,status",
    [
        (False, "invalid", "committed"),
        (True, '[{"op":"replace","path":"/能量","value":4}]', "committed"),
        (True, "invalid", "error"),
        (True, '[{"op":"replace","path":"/missing/value","value":4}]', "error"),
    ],
)
async def test_variable_turn_is_separate_and_atomic(tmp_path, enabled, result, status):
    store = Store(tmp_path)
    card = store.import_card(Path("samples/星航状态卡.json").read_bytes(), "card.json")
    with store.connection() as db:
        db.execute("UPDATE cards SET permission=1 WHERE id=?", (card["id"],))
    session = store.new_session(SessionCreate(card_id=card["id"]))
    service = RoleplayRuntime(store)
    # Even a stale card instruction causing Writer MVU output must never mutate state.
    narrative = "走向观测窗。\n_.add('能量', -99);"
    service.provider_factory = lambda: ScriptedLLM([narrative])
    provider = VariableProvider(result)
    service.variable_provider_factory = lambda: provider
    try:
        turn = service.submit(
            session["id"],
            TurnRequest(
                text="走过去",
                operation_id="one",
                expected_revision=0,
                update_variables=enabled,
            ),
        )
        await service.tasks[turn["id"]]
        saved = store.turn(turn["id"])
        assert saved["status"] == status, saved.get("error")
        current = store.session(session["id"])
        if status == "error":
            assert current["head"] == session["head"]
            assert current["snapshot"] == session["snapshot"]
            assert saved["draft"] == narrative
        else:
            assert current["snapshot"]["state"]["stat_data"]["能量"] == (4 if enabled else 5)
            assert current["messages"][-1]["raw"] == narrative
        assert (provider.messages is not None) == enabled
        assert provider.closed == enabled
        if enabled and status == "committed":
            turn = service.submit(
                session["id"],
                TurnRequest(
                    mode="regenerate",
                    operation_id="two",
                    expected_revision=1,
                    update_variables=True,
                ),
            )
            await service.tasks[turn["id"]]
            assert store.turn(turn["id"])["status"] == "committed"
            payload = json.loads(next(m["content"] for m in provider.messages if m["role"] == "user"))
            assert payload["current_variables"]["能量"] == 5
            assert len(store.session(session["id"])["messages"]) == 3
    finally:
        await service.close()


@pytest.mark.parametrize("with_rules", [False, True])
async def test_recent_context_and_provider_cleanup(with_rules):
    events = []
    store = SimpleNamespace(event=lambda *args: events.append(args))
    settings = SimpleNamespace(memory_timeout=10)
    provider = VariableProvider("[]")
    document = (
        {
            "name": "测试",
            "character_book": {
                "entries": [
                    {
                        "id": "rule",
                        "content": "变量规则 {{user}}",
                        "extensions": {"terrarium": {"audience": "tracker_mvu"}},
                    }
                ]
            },
        }
        if with_rules
        else None
    )
    operations = await analyze_variables(
        store,
        "turn",
        settings,
        [{"role": "assistant", "raw": str(i)} for i in range(12)],
        {"stat_data": {"hp": 2}},
        "input",
        "new prose",
        lambda: provider,
        document=document,
    )
    assert operations == []
    payload = json.loads(next(m["content"] for m in provider.messages if m["role"] == "user"))
    rules = [m["content"] for m in provider.messages if m["content"].startswith("<mvu_rules>\n")]
    assert len(rules) == int(with_rules)
    if with_rules:
        assert "变量规则 用户" in rules[0] and rules[0].endswith("\n</mvu_rules>")
    assert [m["content"] for m in payload["recent_history"]] == [str(i) for i in range(6, 12)]
    assert payload["current_turn"] == {"user": "input", "assistant": "new prose"}
    assert provider.closed


async def test_cancel_during_analysis_rolls_back(tmp_path):
    store = Store(tmp_path)
    card = store.import_card(Path("samples/星航状态卡.json").read_bytes(), "card.json")
    with store.connection() as db:
        db.execute("UPDATE cards SET permission=1 WHERE id=?", (card["id"],))
    session = store.new_session(SessionCreate(card_id=card["id"]))
    service = RoleplayRuntime(store)
    service.provider_factory = lambda: ScriptedLLM(["正文草稿"])
    started = asyncio.Event()
    provider = VariableProvider()

    async def waiting(messages, **kwargs):
        started.set()
        await asyncio.Event().wait()

    provider.chat_complete = waiting
    service.variable_provider_factory = lambda: provider
    try:
        turn = service.submit(
            session["id"],
            TurnRequest(
                text="继续",
                operation_id="cancel",
                expected_revision=0,
                update_variables=True,
            ),
        )
        await asyncio.wait_for(started.wait(), 30)
        await service.cancel(turn["id"])
        assert store.turn(turn["id"])["status"] == "cancelled"
        assert store.session(session["id"])["head"] == session["head"]
        assert provider.closed
    finally:
        await service.close()
