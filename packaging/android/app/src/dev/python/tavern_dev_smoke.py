"""Small on-device probes, bundled only in the separate development application."""

import asyncio
import importlib
import json
import tempfile
import time
from pathlib import Path


def native_dependencies():
    importlib.import_module("tiktoken_ext.openai_public")
    from tavern.core.tokens import count_text
    from tavern.search.retrieval import tokenize

    assert count_text("本地分词验证") > 0
    assert tokenize("灯塔 テスト")


async def sessions(root, checks):
    from httpx import ASGITransport, AsyncClient

    from tavern.application.api import create_app

    app = create_app(root)
    # Lifespan closes jobs, realm clients and storage before TemporaryDirectory removes the fixture.
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
            document = {
                "name": "Android 自检",
                "first_mes": "欢迎 {{user}}。",
                "character_book": {
                    "entries": [{"id": 0, "comment": "[InitVar]", "content": "hp: 7", "enabled": False}]
                },
            }
            imported = await client.post(
                "/api/cards/import",
                files={"file": ("smoke.json", json.dumps(document).encode(), "application/json")},
            )
            imported.raise_for_status()
            card_id = imported.json()["id"]
            for mode in ("half_agent", "full_agent"):
                start = time.perf_counter()
                created = await client.post(
                    "/api/sessions", json={"card_id": card_id, "agent_mode": mode, "persona": "自检玩家"}
                )
                created.raise_for_status()
                session = created.json()
                assert session["snapshot"]["state"]["stat_data"]["hp"] == 7
                reopened = await client.get("/api/sessions/" + session["id"])
                reopened.raise_for_status()
                assert reopened.json()["snapshot"]["state"] == session["snapshot"]["state"]
                checks.append({"name": mode + "_session", "seconds": round(time.perf_counter() - start, 3)})
                print("PASS: " + mode + "_session", flush=True)


def run(cache_dir, suite):
    if suite not in {"all", "native", "sessions"}:
        raise ValueError("未知自检范围：" + suite)
    checks = []
    started = time.perf_counter()
    if suite in {"all", "native"}:
        start = time.perf_counter()
        native_dependencies()
        checks.append({"name": "native_dependencies", "seconds": round(time.perf_counter() - start, 3)})
        print("PASS: native_dependencies", flush=True)
    if suite in {"all", "sessions"}:
        # No reference to the persistent story directory or any user model credentials.
        with tempfile.TemporaryDirectory(prefix="tavern-smoke-", dir=cache_dir) as temporary:
            asyncio.run(sessions(Path(temporary), checks))
    return json.dumps({"ok": True, "checks": checks, "seconds": round(time.perf_counter() - started, 3)})
