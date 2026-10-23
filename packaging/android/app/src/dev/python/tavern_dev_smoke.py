"""Small on-device probes, bundled only in the separate development application."""

import asyncio
import importlib
import json
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace


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


async def local_models(root, cache_dir, checks):
    from starlette.datastructures import UploadFile

    from tavern.android.local_models import SOURCES, mobile_config
    from tavern.core.models import Settings
    from tavern.search.local_models import create_local_model_manager
    from tavern.search.retrieval_config import LocalModel

    started = time.perf_counter()
    manager = create_local_model_manager(SimpleNamespace(root=root))
    settings = Settings()
    for purpose in ("embedding", "reranker"):
        setattr(settings.local_models, purpose, LocalModel(**mobile_config(purpose)))
    settings.local_models.embedding.batch_size = 2
    settings.local_models.reranker.batch_size = 2
    documents = ["北岸港口的守卫阿舟住在灯塔二层，每晚巡视码头。", "南山茶园的采茶季从四月开始。"]
    query = "北岸港口的守卫住在哪里？"
    try:
        for purpose in ("embedding", "reranker"):
            print("IMPORT: " + purpose, flush=True)
            path = Path(cache_dir) / "model-fixtures" / SOURCES[purpose]["filename"]
            with path.open("rb") as source:
                file = UploadFile(source, size=path.stat().st_size, filename=path.name)
                await manager.import_model(purpose, file, settings)
            print("WARMUP: " + purpose, flush=True)
            await manager.warmup(purpose, settings)
        assert all(item["ready"] for item in manager.status(settings)["models"].values())
        vectors = await manager.infer("embedding", "embed", documents, settings, timeout=45)
        assert len(vectors) == 2 and all(len(row) == 1024 for row in vectors)
        # Exercise native last-token pooling across the 512-token microbatch boundary.
        long_text = documents[0] * 35
        assert len(await manager.servers["embedding"].tokenize(long_text)) > 512
        long_vector = await manager.infer("embedding", "embed", [long_text], settings, timeout=45)
        assert len(long_vector[0]) == 1024
        settings.retrieval.chunk_tokens = 128
        settings.retrieval.chunk_overlap = 16
        indexed = await manager.infer(
            "embedding",
            "index",
            [{"id": "fixture", "keys": ["码头"], "content": documents[0] * 10}],
            settings,
            timeout=45,
        )
        assert len(indexed[0]["fragments"]) >= 2
        assert len(indexed[0]["vectors"]) == len(indexed[0]["fragments"])
        scores = await manager.infer(
            "reranker", "rerank", [[query, [text]] for text in documents], settings, timeout=45
        )
        assert len(scores) == 2 and 0 <= scores[1] < scores[0] <= 1
    finally:
        await manager.close()
    assert not manager.servers and not manager.ready
    checks.append(
        {
            "name": "local_models",
            "seconds": round(time.perf_counter() - started, 3),
            "indexed_fragments": len(indexed[0]["fragments"]),
            "rerank_scores": scores,
        }
    )
    print("PASS: local_models", flush=True)


def run(cache_dir, suite):
    if suite not in {"all", "native", "sessions", "models"}:
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
    if suite == "models":
        # Deliberately opt-in: normal daily checks do not require or load model weights.
        with tempfile.TemporaryDirectory(prefix="tavern-model-smoke-", dir=cache_dir) as temporary:
            asyncio.run(local_models(Path(temporary), cache_dir, checks))
    return json.dumps({"ok": True, "checks": checks, "seconds": round(time.perf_counter() - started, 3)})
