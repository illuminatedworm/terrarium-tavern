"""Exercise lazy sandbox history, opt-in execution and transactional isolation."""

import asyncio
import copy

import pytest

from tavern.scripting import runtime as runtime_module
from tavern.scripting.runtime import ScriptRuntime


@pytest.fixture
async def runtime():
    value = ScriptRuntime()
    yield value
    await value.close()


async def host(*args):
    return None


def records():
    return [
        {
            "id": str(i),
            "role": "assistant",
            "raw": f"完整正文{i}",
            "display": "正文",
            "snapshot": {"state": {"stat_data": {"hp": i}, f"key{i}": i}, "scopes": {}},
        }
        for i in range(4)
    ]


async def test_lazy_reads_scope_precedence_writes_and_sparse_rollback(runtime):
    history = records()
    html = """<div>panel</div><script>
    window.initial = getAllVariables();
    eventOn(Mvu.events.VARIABLE_UPDATE_ENDED, data => {data.stat_data.hp += getChatMessages(0)[0].data.stat_data.hp;});
    </script>"""
    history[-1]["display"] = html
    original = copy.deepcopy(history)
    session = await runtime.open({"name": "卡"}, history, history[-1]["snapshot"], True, host)
    try:
        frame = session.frames[0]
        assert await frame.evaluate("() => typeof MVUReference") == "undefined"
        saved = await frame.evaluate("() => TavernRuntime.checkpoint()")
        assert saved["loaded"] == []
        assert await frame.evaluate("() => initial") == {
            "stat_data": {"hp": 3},
            **{f"key{i}": i for i in range(4)},
        }
        assert await frame.evaluate("() => getChatMessages(-2)[0].message") == "完整正文2"
        await frame.evaluate("() => replaceVariables({stat_data:{hp:99}},{type:'message',message_id:3})")
        assert (await frame.evaluate("() => getAllVariables()"))["stat_data"]["hp"] == 99
        await frame.evaluate("saved => TavernRuntime.restore(saved)", saved)
        assert (await frame.evaluate("() => getAllVariables()"))["stat_data"]["hp"] == 3
        assert (await frame.evaluate("() => TavernRuntime.checkpoint()"))["loaded"] == []
        # Full legacy reads remain complete; only requested records are materialized.
        assert len(await frame.evaluate("() => getChatMessages()")) == 4
        result = await frame.evaluate("() => Mvu.parseMessage(\"_.add('hp', 1);\", {stat_data:{hp:3}})")
        assert result["stat_data"]["hp"] == 4
        assert await frame.evaluate("() => typeof MVUReference") == "object"
        assert history == original
        assert await frame.evaluate(
            "async () => {try {await fetch('https://example.com/private');return false;}catch{return true;}}"
        )
    finally:
        await session.close()


async def test_display_only_skips_backend_but_legacy_callbacks_survive_reopens(runtime):
    history = records()
    history[0]["display"] = (
        '<meta name="tavern-capabilities" content="display-only live-state"><div>UI</div><script>throw Error("UI only")</script>'
    )
    history[1]["display"] = (
        "<div>legacy</div><script>eventOn(Mvu.events.VARIABLE_UPDATE_ENDED,data=>data.stat_data.hp=17)</script>"
    )
    for _ in range(2):
        session = await runtime.open({"name": "卡"}, history, history[-1]["snapshot"], True, host)
        try:
            assert len(session.frames) == 1
            result = await session.call("process", "", [{"op": "replace", "path": "/hp", "value": 5}], "")
            assert result["state"]["stat_data"]["hp"] == 17
        finally:
            await session.close()
    assert len(runtime.documents) == 2


async def test_failed_callback_restores_sparse_frame_and_next_patch_can_retry(runtime):
    history = records()
    history[-1]["display"] = """<div>回调</div><script>
    window.listener=eventOn(Mvu.events.VARIABLE_UPDATE_ENDED,()=>{
        replaceVariables({stat_data:{hp:999}},{type:'message',message_id:0});
        throw Error('callback rejected');
    });</script>"""
    session = await runtime.open({"name": "卡"}, history, history[-1]["snapshot"], True, host)
    try:
        before = await session.call("snapshot")
        with pytest.raises(Exception, match="callback rejected"):
            await session.call("process", "", [{"op": "replace", "path": "/hp", "value": 5}], "")
        assert await session.call("snapshot") == before
        assert (await session.frames[0].evaluate("() => TavernRuntime.checkpoint()"))["loaded"] == []
        await session.frames[0].evaluate("() => listener.stop()")
        result = await session.call("process", "", [{"op": "replace", "path": "/hp", "value": 5}], "")
        assert result["state"]["stat_data"]["hp"] == 5
        assert await session.frames[0].evaluate("() => getChatMessages(0)[0].variables.stat_data.hp") == 0
    finally:
        await session.close()


async def test_cancel_and_setup_failure_close_contexts(runtime, monkeypatch):
    started = asyncio.Event()

    async def boundary(method, args):
        if method == "log":
            started.set()

    task = asyncio.create_task(
        runtime.open(
            {"name": "卡"},
            [],
            {"state": {}, "scopes": {}},
            True,
            boundary,
            scripts_override=[{"content": "toastr.info('started');await new Promise(()=>{})"}],
        )
    )
    await asyncio.wait_for(started.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert runtime.backend.browser.contexts == []

    def unavailable(path):
        raise OSError("fixture SDK unavailable")

    monkeypatch.setattr(runtime_module, "script_source", unavailable)
    with pytest.raises(OSError, match="unavailable"):
        await runtime.open({"name": "卡"}, [], {"state": {}, "scopes": {}}, False, host, eager=True)
    assert runtime.backend.browser.contexts == []
    # A deferred session starts its realm on the first JavaScript call; that setup
    # failure must close the context just like an eager open.
    deferred = await runtime.open({"name": "卡"}, [], {"state": {}, "scopes": {}}, False, host)
    with pytest.raises(OSError, match="unavailable"):
        await deferred.call(
            "transform", "正文", [{"findRegex": "/正/g", "replaceString": "x"}], 2, "display", 0
        )
    assert runtime.backend.browser.contexts == []


async def test_cached_templates_do_not_share_branch_variables(runtime):
    template = "<div>分支</div><script>window.initial=getAllVariables();</script>"
    first_history, second_history = records(), records()
    first_history[-1]["display"] = second_history[-1]["display"] = template
    second_history[-1]["snapshot"]["state"]["stat_data"]["hp"] = 88
    first = await runtime.open({"name": "卡"}, first_history, first_history[-1]["snapshot"], True, host)
    second = await runtime.open({"name": "卡"}, second_history, second_history[-1]["snapshot"], True, host)
    try:
        assert (await first.frames[0].evaluate("() => initial"))["stat_data"]["hp"] == 3
        assert (await second.frames[0].evaluate("() => initial"))["stat_data"]["hp"] == 88
        assert len(runtime.documents) == 1
        assert first.prefix != second.prefix
    finally:
        await first.close()
        await second.close()
