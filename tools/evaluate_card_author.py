"""Opt-in live Card Author evaluation. Credentials are read-only; data is isolated.

Run with --baseline before changing the editor, then without it for the new editor.
Every outbound inference attempt shares the persistent 60-request budget.
"""

import argparse
import asyncio
import copy
import json
import os
import sqlite3
import time
from pathlib import Path

import httpx

from tavern.character.card_author import CardAuthorService
from tavern.character.cards import lore_entries
from tavern.character.editor_config import blank_card
from tavern.chatgpt.provider import ChatGPTProvider
from tavern.core.models import CardAuthorRequest
from tavern.core.settings import migrate_settings
from tavern.scripting.model_transport import ModelTransport
from tavern.storage.store import Store, dump, uid


async def evaluate(args):
    args.output.mkdir(parents=True, exist_ok=True)
    budget_file = args.output / "budget.json"
    budget = json.loads(budget_file.read_text(encoding="utf-8")) if budget_file.exists() else {"requests": 0}
    with sqlite3.connect(args.data.resolve().as_uri() + "?mode=ro", uri=True) as db:
        settings = migrate_settings(
            json.loads(db.execute("SELECT value FROM meta WHERE key='settings'").fetchone()[0])
        )
    root = args.output / ("baseline-" + uid() if args.baseline else "evaluation-" + uid())
    store = Store(root)
    # The isolated database contains no production settings or credentials.
    # Channel selection and transport obtain their settings from memory only.
    store.settings_document = lambda: settings.model_copy(deep=True)
    if args.cases == "navigation":
        settings.shared.card_author_config.web.mode = "off"
        settings.shared.card_author_config.max_steps = args.request_limit
    service = CardAuthorService(store)
    cfg = store.settings().for_role("card_author")
    results = []

    async def run(workspace, text, label="baseline", *, interrupt_after_edit=False, **options):
        if budget["requests"] >= args.request_limit:
            results.append({"label": label, "status": "not_run_budget_exhausted", "requests": 0})
            return workspace
        fault = {"edited": False, "raised": False}
        original_event = service.edits.event

        def event(rid, kind, payload):
            if kind == "draft_change":
                fault["edited"] = True
            original_event(rid, kind, payload)

        service.edits.event = event

        async def counted(request):
            if interrupt_after_edit and fault["edited"] and not fault["raised"]:
                fault["raised"] = True
                raise ValueError("evaluation-injected interruption after durable edit")
            if budget["requests"] >= args.request_limit:
                raise ValueError("真实模型评测已达到本次累计请求上限")
            budget["requests"] += 1
            budget_file.write_text(dump(budget), encoding="utf-8")

        client = httpx.AsyncClient(
            proxy=args.proxy or ((cfg.proxy or None) if cfg.use_proxy else None),
            trust_env=False,
            timeout=120,
            event_hooks={"request": [counted]},
        )
        transport = ChatGPTProvider if cfg.auth_mode == "openai_api_key" else ModelTransport
        key = (
            cfg.api_key
            if cfg.auth_mode == "openai_api_key"
            else cfg.api_key or os.environ.get("TAVERN_API_KEY", "")
        )
        provider = transport(cfg, key, http_client=client)
        service.provider_factory = lambda: provider
        request = CardAuthorRequest(
            text=text, expected_revision=workspace["revision"], operation_id=uid(), **options
        )
        before = budget["requests"]
        started = time.monotonic()
        task = service.submit(workspace["id"], request, legacy=args.baseline)
        try:
            await service.tasks[task["id"]]
        finally:
            service.edits.event = original_event
        result = service.edits.run(task["id"])
        current = service.edits.conversation(workspace["id"])
        results.append(
            {
                "label": label,
                "request": text,
                "status": result["status"],
                "response": result["response"],
                "error": result["error"],
                "requests": budget["requests"] - before,
                "usage": getattr(provider, "usage_history", []),
                "workspace": workspace["id"],
                "duration_s": round(time.monotonic() - started, 2),
                "cards": [
                    {
                        "ref": c["ref"],
                        "document": c["document"],
                        "brief": c.get("brief"),
                    }
                    for c in current["cards"]
                ],
                "state": current["state"],
            }
        )
        (root / "report.json").write_text(dump(results), encoding="utf-8")
        print(
            dump(
                {
                    "case": label,
                    "status": result["status"],
                    "requests": budget["requests"] - before,
                    "total_requests": budget["requests"],
                }
            ),
            flush=True,
        )
        return current

    document = {
        "spec": "chara_card_v2",
        "spec_version": "2.0",
        "data": {
            "name": "雾港评测卡",
            "description": "书店店主林间，温和、敏锐，替旅人保管未寄出的信。",
            "first_mes": "雨夜，书店铜铃响了。林间递来一块毛巾。",
            "extensions": {"unknown": {"keep": "KEEP_72913"}},
            "character_book": {
                "entries": [
                    {
                        "id": "lighthouse",
                        "keys": ["灯塔"],
                        "enabled": True,
                        "content": "旧灯塔每逢秋分亮起蓝灯，林间知道原因，却从不主动谈起。",
                    }
                ]
            },
        },
    }
    try:
        if args.cases == "navigation" and not args.baseline:
            workspace = service.edits.create_conversation()
            reference, target = blank_card("雾港"), blank_card("雾港")
            reference["data"].update(
                description="摆渡人惜字，喜欢用短句和潮声作答。",
                mes_example="“潮低。上船。”他把缆绳盘好。“雾散，再问。”",
            )
            origin = "暮色压过塔檐。潮尺仍然停在第三格。\n他请你进来。"
            target["data"].update(first_mes="门外风大。", alternate_greetings=[origin, "翌日清晨。"])
            target["data"]["character_book"]["entries"] = [
                {"comment": f"港区地志 {i}", "keys": [f"栈桥{i}"], "content": f"第{i}号栈桥存放帆布。"}
                for i in range(400)
            ]
            target["data"]["character_book"]["entries"].insert(
                263,
                {
                    "comment": "潮务手册",
                    "enabled": True,
                    "constant": True,
                    "content": "".join(f"第{i}次巡检，窗闩完好，潮尺刻线清晰。\n" for i in range(260))
                    + "守塔人受灰鲸议会托付，必须亲手交回第七枚铜铃才能离港。",
                },
            )
            for doc in (reference, target):
                cid = store.import_card(dump(doc).encode())["id"]
                workspace = service.edits.attach(workspace["id"], cid, workspace["revision"])
            workspace = await run(
                workspace,
                "先解释目标卡里的守塔人为什么不能离港，再参考另一张同名卡的口吻，仅润色目标卡界面中第二个开场的最后一句。其余原文保留。",
                "navigation",
                target_refs=["C2"],
            )
            item = results[-1]
            actual = workspace["cards"][1]["document"]
            opening = actual["data"]["alternate_greetings"][0]
            expected = copy.deepcopy(target)
            expected["data"]["alternate_greetings"][0] = opening
            item["checks"] = {
                "tail_fact": all(word in item.get("response", "") for word in ("灰鲸", "铜铃")),
                "targeted_edit": opening != origin
                and opening.startswith(origin.rsplit("\n", 1)[0] + "\n")
                and actual == expected,
                "reference_intact": workspace["cards"][0]["document"] == reference,
            }
            item["objective_pass"] = item["status"] == "completed" and all(item["checks"].values())
            (root / "report.json").write_text(dump(results), encoding="utf-8")
            print(
                dump(
                    {
                        "report": str(root / "report.json"),
                        "checks": item["checks"],
                        "requests": budget["requests"],
                    }
                ),
                flush=True,
            )
            return
        card = store.import_card(dump(document).encode())
        workspace = service.edits.create(card["id"])
        if args.baseline:
            workspace = await run(
                workspace,
                "只把角色描述完整替换为：林间经营雾港书店，说话克制，暗中追查灯塔失踪案。其他字段原样保留。",
            )
            await run(
                workspace, "只分析这张卡的描述、开场和世界书分别用于什么，指出目前的创作缺口。不要修改草稿。"
            )
        else:

            async def attached(count=1, doc=None):
                value = service.edits.create_conversation()
                for _ in range(count):
                    added = store.import_card(dump(doc or document).encode())
                    value = service.edits.attach(value["id"], added["id"], value["revision"])
                return value

            if args.cases in {"core", "all"}:
                workspace = await run(
                    workspace,
                    "只把 C1 的角色描述完整替换为：林间经营雾港书店，说话克制，暗中追查灯塔失踪案。其他字段原样保留。",
                    "targeted_edit",
                )
                expected = copy.deepcopy(document)
                expected["data"]["description"] = "林间经营雾港书店，说话克制，暗中追查灯塔失踪案。"
                results[-1]["objective_pass"] = workspace["cards"][0]["document"] == expected
                empty = service.edits.create_conversation()
                empty = await run(
                    empty,
                    "设计一个神秘的雨夜书店店主角色卡。先给简短方案，暂不制作；不加入变量或脚本。",
                    "creation_proposal",
                )
                results[-1]["objective_pass"] = not empty["cards"]
                empty = await run(
                    empty,
                    "确认方案，请制作一张简洁文本卡，全部资料控制在800字以内，包含开场和一段示例。",
                    "confirmed_creation",
                )
                results[-1]["objective_pass"] = len(empty["cards"]) == 1 and all(
                    empty["cards"][0]["document"].get("data", {}).get(k)
                    for k in ("description", "first_mes", "mes_example")
                )
                two = await attached(2)
                originals = [c["document"] for c in two["cards"]]
                two = await run(
                    two,
                    "只比较 C1 与 C2 的描述、开场、世界书，指出它们是否重复及各字段用途。不要修改或新建卡片，简要回答。",
                    "multi_reference_analysis",
                )
                results[-1]["objective_pass"] = [c["document"] for c in two["cards"]] == originals
            if args.cases in {"remaining", "all"}:
                two = await attached(2)
                two = await run(
                    two,
                    "C1 描述改为『店主甲守护灯塔。』，C2 描述改为『店主乙追查失踪案。』。只改这两个字段。",
                    "multi_card_edit",
                )
                results[-1]["objective_pass"] = [
                    c["document"]["data"]["description"] for c in two["cards"]
                ] == ["店主甲守护灯塔。", "店主乙追查失踪案。"]
                variable = await attached()
                variable = await run(
                    variable,
                    "请直接给 C1 增加好感度变量，初始值为 0，帮助店主时增加 1，上限为 10。用世界书条目写变量规则，并做本地初始化验证。",
                    "variable_rules",
                )
                results[-1]["objective_pass"] = bool(
                    lore_entries(variable["cards"][0]["document"], "mvu_rules")
                )
                broken = copy.deepcopy(document)
                broken["data"]["extensions"]["tavern_helper"] = {
                    "scripts": [{"name": "startup", "content": "throw new Error('STARTUP_BUG');"}]
                }
                repair = await attached(doc=broken)
                await run(
                    repair,
                    "C1 开场初始化时报 STARTUP_BUG。请读取脚本，修复这个故意抛错的测试逻辑，其他资料保留，并进行本地验证。",
                    "sandbox_repair",
                )
                history = await attached()
                history = await run(
                    history,
                    "确认创作约束：保留开场和世界书原文，店主永远不主动泄露灯塔秘密。请把约束记入进度，不修改卡片。",
                    "history_constraints",
                )
                history = await run(
                    history,
                    "请先调用 new_context 换窗，然后只在 C1 描述末尾追加一句『他绝不主动泄露灯塔秘密。』。",
                    "context_rollover",
                )
                results[-1]["objective_pass"] = (
                    history["state"].get("window", 0) > 0
                    and history["cards"][0]["document"]["data"]["first_mes"] == document["data"]["first_mes"]
                )
                resumed = await attached()
                resumed = await run(
                    resumed,
                    "只把 C1 的描述改成『店主守着一封旧信。』。",
                    "interruption",
                    interrupt_after_edit=True,
                )
                if resumed["pending"]:
                    resumed = await run(
                        resumed,
                        "保留已完成的描述修改，再在末尾追加『信封上没有署名。』。",
                        "resume_steering",
                        resume_run_id=resumed["pending"]["run"],
                    )
                    results[-1]["objective_pass"] = (
                        resumed["cards"][0]["document"]["data"]["description"]
                        == "店主守着一封旧信。信封上没有署名。"
                    )
            (root / "report.json").write_text(dump(results), encoding="utf-8")
    finally:
        await service.close()
    print(dump({"report": str(root / "report.json"), "requests": budget["requests"]}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path("data/tavern.sqlite3"))
    parser.add_argument("--output", type=Path, default=Path(".cache/card-author-evaluation"))
    parser.add_argument("--baseline", action="store_true")
    parser.add_argument("--cases", choices=("navigation", "core", "remaining", "all"), default="all")
    parser.add_argument("--request-limit", type=int, choices=range(2, 61), default=60, metavar="2..60")
    parser.add_argument("--proxy", help="Explicit proxy for evaluation requests")
    asyncio.run(evaluate(parser.parse_args()))
