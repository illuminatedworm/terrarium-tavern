"""Read a private playthrough without calling a remote model or modifying its DB.

Only aggregate sizes/counts are emitted. Keep the source DB and derived cache out
of distributions. Narrative/MVU outputs are replayed, not generated anew.
"""

import argparse
import asyncio
import copy
import json
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

from tavern.conversation.context import compile_context
from tavern.core.models import Settings
from tavern.core.tokens import request_tokens
from tavern.search.local_models import LocalModelManager
from tavern.search.retrieval import RetrievalEngine
from tavern.storage.store import Store


async def replay(source, model_dir):
    with closing(sqlite3.connect(Path(source).resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        session = dict(db.execute("SELECT * FROM sessions ORDER BY created DESC LIMIT 1").fetchone())
        for key in ("recipe", "persona_data"):
            session[key] = json.loads(session[key])
        document = json.loads(session["card_data"])["document"]
        messages = {
            r["id"]: dict(r) for r in db.execute("SELECT * FROM messages WHERE session=?", (session["id"],))
        }
        for message in messages.values():
            message["snapshot"] = json.loads(message["snapshot"])
        chain, cursor = [], session["head"]
        while cursor:
            chain.append(messages[cursor])
            cursor = messages[cursor]["parent"]
        chain.reverse()
        baseline = {}
        for turn in db.execute(
            "SELECT id FROM turns WHERE session=? AND status='committed'", (session["id"],)
        ):
            events = [
                (r["kind"], json.loads(r["payload"]))
                for r in db.execute(
                    "SELECT kind,payload FROM events WHERE turn=? ORDER BY seq", (turn["id"],)
                )
            ]
            mid = next((p.get("message_id") for kind, p in reversed(events) if kind == "committed"), None)
            request = next(
                (
                    p
                    for kind, p in reversed(events)
                    if kind == "provider_request" and p.get("purpose") == "narrative"
                ),
                None,
            )
            if mid and request:
                baseline[mid] = request_tokens(request["messages"])
    temporary = tempfile.TemporaryDirectory(prefix="context-replay-")
    store = Store(Path(temporary.name))
    models = LocalModelManager(store)
    models.root = Path(model_dir).resolve()
    engine = RetrievalEngine(store, models)
    cfg = Settings(
        retrieval={"channels": ["keyword", "bm25", "embedding"]},
        foreground={"context_tokens": 200000, "reply_tokens": 8192},
    ).for_role("foreground")
    cfg.local_models.reranker_enabled = True
    report = []
    try:
        for purpose in ("embedding", "reranker"):
            await models.warmup(purpose, cfg)
        snapshot = {
            "state": chain[0]["snapshot"].get("state", {}),
            "memory_document": {"version": 1, "records": []},
        }
        await engine.prepare(document, [], "", snapshot, cfg, session, "build", index_only=True)
        await asyncio.gather(*list(models.tasks.values()))
        for index, user in enumerate(chain):
            if user["role"] != "user" or index + 1 >= len(chain):
                continue
            answer = chain[index + 1]
            snapshot["state"] = copy.deepcopy(user["snapshot"].get("state", {}))
            result = await engine.prepare(
                document, chain[:index], user["model"], snapshot, cfg, session, str(index)
            )
            context = compile_context(
                document,
                session,
                chain[:index],
                snapshot,
                user["model"],
                cfg,
                str(index),
                retrieval=result,
                input_message_id=user["id"],
            )
            snapshot.update(lore=context["next_lore"], lore_injections=context["next_lore_injections"])
            report.append(
                {
                    "round": len(report) + 1,
                    "recorded_input_tokens": baseline.get(answer["id"]),
                    "replayed_input_tokens": context["estimated_tokens"],
                    "token_counting": context["token_counting"],
                    "new_lore_count": sum(not e.get("constant") for e in context["lore"]),
                    "retained_lore_count": len(context["next_lore_injections"]),
                    "retrieval_ms": result["elapsed_ms"],
                    "degraded": any(
                        "不可用" in n["reason"] or "降级" in n["reason"] for n in result["diagnostics"]
                    ),
                }
            )
        output = Path(".cache/release-validation/private-replay-metrics.json")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False))
    finally:
        await models.close()
        temporary.cleanup()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("source", help="Read-only backup database")
    parser.add_argument("--model-dir", default="data/models")
    args = parser.parse_args()
    asyncio.run(replay(args.source, args.model_dir))
