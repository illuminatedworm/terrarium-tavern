"""Deterministic card probes with no story storage, downloads or nested models."""
import asyncio
import copy
import json
import re

from tavern.character.card_workflow import validate_document
from tavern.character.cards import normalize, regex_scripts
from tavern.core.model_view import state_for_model
from tavern.scripting.dependencies import IMPORTS, Dependencies
from tavern.scripting.runtime import SDK, ScriptRuntime


async def check_display(session, html, mirror, assertions, host, *, after=False):
    """Mount fresh HTML with the committed mirror, like StoryMessage's snapshot rebuild.

    Use real document parsing so DOMContentLoaded, inline scripts and ready handlers
    execute in order. The existing runtime context denies all network requests.
    """
    presentation = await session.call("parseStoryDisplay", html)
    parts = presentation["panel"] or presentation["body"]
    frames, errors = [], []
    try:
        data = {**mirror, "ui": True, "serverMain": False, "currentMessage": len(mirror["messages"]) - 1}
        bootstrap = "TavernRuntime.install(" + json.dumps(data) + ", (m,a)=>__tavernHost(m,a));"
        scripts = [SDK.read_text(encoding="utf-8"), (SDK.parent / "mvu-core.js").read_text(encoding="utf-8"), bootstrap]
        head = "".join("<script>" + re.sub(r"</script", r"<\\/script", code, flags=re.I) + "</script>" for code in scripts)
        for part in parts:
            markup = await session.call("snapshotMacros", part["text"], mirror["state"], mirror.get("scopes", {}))
            markup = markup.replace("{{user}}", mirror.get("persona", "测试用户")).replace("{{char}}", mirror.get("character", ""))
            if part["format"] == "markdown":
                markup = await session.call("renderMarkdown", markup)
            frame = await session.context.new_page()
            frames.append(frame)
            frame.on("pageerror", lambda error: errors.append(str(error)))
            await frame.expose_function("__tavernHost", host)
            await frame.set_content(
                "<!doctype html><html><head><meta http-equiv=\"Content-Security-Policy\" "
                "content=\"default-src 'none'; script-src 'unsafe-inline' 'unsafe-eval'; "
                "style-src 'unsafe-inline' data:; img-src data: blob:; connect-src 'none'\">"
                + head + "</head><body>" + markup + "</body></html>", wait_until="load")
            await frame.evaluate("async () => { await new Promise(resolve => $(resolve)); await TavernRuntime.flush(); }")
        if errors:
            raise ValueError("展示脚本执行失败：" + "\n".join(errors))
        observed = []
        for assertion in assertions:
            expected = assertion.get("after_text") if after else assertion["text"]
            if expected is None:
                continue
            locators = [frame.locator(assertion["selector"]) for frame in frames]
            counts = [await locator.count() for locator in locators]
            count = sum(counts)
            actual = (await locators[counts.index(1)].text_content()).strip() if count == 1 else None
            observed.append({"selector": assertion["selector"], "expected": expected,
                             "actual": actual, "matches": count,
                             "status": "passed" if count == 1 and actual == expected else "failed"})
        return observed
    finally:
        for frame in frames:
            await frame.close()


async def test_card(store, item, index=0, samples=(), display_assertions=(), state_patch=()):
    document = copy.deepcopy(item["document"])
    validate_document(document)
    data = normalize(document)
    greetings = [data.get("first_mes", ""), *data.get("alternate_greetings", [])]
    if index >= len(greetings):
        raise ValueError("开场编号不存在")
    if any(a["sample_index"] > len(samples) for a in display_assertions):
        raise ValueError("展示断言的 sample_index 超出开场和样例范围")
    if any(a.get("after_text") is not None for a in display_assertions) and not state_patch:
        raise ValueError("after_text 需要提供 state_patch")
    source = {**item.get("source", {}), "id": item.get("card") or "editor-probe", "document": document}
    manifest = source.get("dependencies", {"scripts": {}, "resources": {}})
    manifest.setdefault("scripts", {})
    manifest.setdefault("resources", {})
    dependencies = Dependencies(store)
    dependencies.read = lambda card: manifest
    checks, logs = [], []
    runtime, session = ScriptRuntime(), None

    async def host(method, args):
        if method == "log":
            logs.append(args)
            return None
        raise ValueError("本地验证不开放此宿主能力：" + method)

    async def probe():
        nonlocal session
        scripts = dependencies.scripts(source)
        if any(IMPORTS.search(s.get("content", "")) and not s.get("bundled") for s in scripts):
            return {"status": "blocked", "checks": [{"check": "dependencies", "status": "blocked", "message": "脚本模块尚未缓存；本地验证不下载依赖"}]}
        snapshot = {"state": {}, "scopes": {}}
        history = [{"id": "probe-opening", "role": "assistant", "raw": greetings[index], "snapshot": snapshot}]
        session = await runtime.open(document, history, snapshot, True, host, scripts,
            lambda html: dependencies.markup(source, html), persona="测试用户", opening=True)
        mirror = await session.call("snapshot")
        checks.append({"check": "initialization_and_startup", "status": "passed", "state": state_for_model(mirror["state"])})
        displays = []
        rules = regex_scripts(document)
        for sample_index, text in enumerate([greetings[index], *samples]):
            transformed = {}
            for placement, name in ((1, "input"), (2, "output")):
                for target in ("model", "display"):
                    transformed[f"{name}_{target}"] = await session.call("transform", text, rules, placement, target, 0)
            displays.append(transformed["output_display"])
            checks.append({"check": "regex", "status": "observed" if rules else "skipped", "sample_index": sample_index,
                           "rule_count": len(rules), "source": text, "observed": transformed,
                           "message": "仅记录转换结果；是否命中预期展示需用 display_assertions 验证"})
        for after in (False, True):
            if after:
                if not state_patch:
                    break
                mirror = await session.call("process", "", list(state_patch), "")
                checks.append({"check": "state_patch", "status": "passed", "state": state_for_model(mirror["state"])})
            for sample_index, html in enumerate(displays):
                assertions = [a for a in display_assertions if a["sample_index"] == sample_index
                              and (not after or a.get("after_text") is not None)]
                if not assertions:
                    continue
                rendered = await check_display(session, dependencies.markup(source, html), mirror, assertions, host, after=after)
                checks.append({"check": "display_after_patch" if after else "display", "sample_index": sample_index,
                               "status": "passed" if all(r["status"] == "passed" for r in rendered) else "failed",
                               "assertions": rendered})
        if any(check["status"] == "failed" for check in checks):
            return {"status": "failed", "checks": checks}
        return {"status": "passed", "checks": checks}

    try:
        result = await asyncio.wait_for(probe(), timeout=30)
    except TimeoutError:
        result = {"status": "blocked", "checks": checks, "message": "本地验证超时，未完成的检查不能视为通过"}
    except Exception as error:
        message = str(error)
        result = {"status": "blocked" if "尚未缓存" in message or "尚未构建" in message else "failed",
                  "checks": checks, "message": message}
    finally:
        if session:
            await session.close()
        await runtime.close()
    untested = ["真实模型的叙事及变量判断", "其他开场", "完整视觉和交互行为"]
    if not display_assertions:
        untested.append("HTML/MVU 前端渲染与状态绑定（未提供 display_assertions）")
    if not state_patch or not any(a.get("after_text") is not None for a in display_assertions):
        untested.append("变量补丁后的前端显示")
    return {"card_ref": item["ref"], "opening_index": index, **result, "logs": logs,
            "untested": untested, "model_calls": 0}
