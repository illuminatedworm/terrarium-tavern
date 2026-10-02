"""Deterministic card probes with no story storage, downloads or nested models."""
import asyncio
import copy

from tavern.character.card_workflow import validate_document, workflow
from tavern.character.cards import normalize, regex_scripts
from tavern.core.model_view import state_for_model
from tavern.scripting.dependencies import IMPORTS, Dependencies
from tavern.scripting.runtime import ScriptRuntime


async def test_card(store, item, index=0, samples=()):
    document = copy.deepcopy(item["document"])
    validate_document(document)
    data = normalize(document)
    greetings = [data.get("first_mes", ""), *data.get("alternate_greetings", [])]
    if index >= len(greetings):
        raise ValueError("开场编号不存在")
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
        metadata = workflow(document)
        session = await runtime.open(document, history, snapshot, True, host, scripts,
            lambda html: dependencies.markup(source, html), persona="测试用户", opening=True,
            greeting_id=metadata["greetings"][index]["id"] if metadata else None)
        mirror = await session.call("snapshot")
        checks.append({"check": "initialization_and_startup", "status": "passed", "state": state_for_model(mirror["state"])})
        for text in [greetings[index], *samples]:
            transformed = {}
            for placement, name in ((1, "input"), (2, "output")):
                for target in ("model", "display"):
                    transformed[f"{name}_{target}"] = await session.call("transform", text, regex_scripts(document), placement, target, 0)
            checks.append({"check": "regex", "status": "passed", "source": text, "observed": transformed})
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
    return {"card_ref": item["ref"], "opening_index": index, **result, "logs": logs,
            "untested": ["真实模型的叙事及变量判断", "其他开场", "完整视觉和交互行为"], "model_calls": 0}
