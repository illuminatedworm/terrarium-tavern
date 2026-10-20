"""Explicit local-only validation harness; uses an isolated data directory."""

import argparse
import asyncio
import json
import tempfile
import time
from pathlib import Path

from tavern.core.models import Settings
from tavern.search.local_models import LocalModelManager
from tavern.storage.store import Store


async def main(download, model_dir):
    temporary = tempfile.TemporaryDirectory(prefix="model-validation-")
    store = Store(Path(temporary.name))
    manager = LocalModelManager(store)
    manager.root = Path(model_dir).resolve()
    settings = Settings()
    report = {}
    try:
        if download:
            for purpose in ("embedding", "reranker"):
                if not manager.status(settings)["models"][purpose]["downloaded"]:
                    job = manager.download(purpose, settings)
                    await manager.tasks[job["id"]]
                    print(json.dumps(job, ensure_ascii=False), flush=True)
                    if job["status"] != "complete":
                        raise RuntimeError(job.get("error", job["status"]))
        for purpose in ("embedding", "reranker"):
            start = time.perf_counter()
            await manager.warmup(purpose, settings)
            cold = time.perf_counter() - start
            start = time.perf_counter()
            result = await manager.infer(
                purpose,
                "embed" if purpose == "embedding" else "rerank",
                ["寻找灯塔守卫"] if purpose == "embedding" else [["寻找灯塔守卫", "守卫住在灯塔。"]],
                settings,
                timeout=30,
                query=purpose == "embedding",
            )
            report[purpose] = {
                "cold_seconds": cold,
                "inference_ms": (time.perf_counter() - start) * 1000,
                "dimensions_or_score": len(result[0]) if purpose == "embedding" else result[0],
            }
        Path(".cache").mkdir(exist_ok=True)
        Path(".cache/local-model-validation.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report, indent=2), flush=True)
    finally:
        await manager.close()
        temporary.cleanup()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--download", action="store_true")
    parser.add_argument("--model-dir", default="data/models")
    args = parser.parse_args()
    asyncio.run(main(args.download, args.model_dir))
