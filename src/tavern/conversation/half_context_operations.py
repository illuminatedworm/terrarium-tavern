"""Half Agent mode transitions: original sources and Tracker coverage, never handoff."""

import copy
from dataclasses import asdict

from tavern.conversation.context import compile_context
from tavern.conversation.half_compaction import STATE, source_lore, valid_source_lore
from tavern.conversation.half_context import load_trace
from tavern.conversation.scene import archive_history
from tavern.conversation.window import plan_window
from tavern.core.cache_policy import fingerprint
from tavern.core.message_adapter import adapter_snapshot
from tavern.core.provider import make_provider
from tavern.memories.history_recall import HistoryBudget
from tavern.memories.memory import completed_rounds, window_valid
from tavern.memories.memory_engine import CallBudget, plan_batch
from tavern.memories.memory_records import read_memory
from tavern.scripting.request_context import response_origin
from tavern.tracker.task import TrackerTask
from tavern.tracker.workflow import finalize_sources, install_coverage


def compile_sources(store, session, snapshot, document, settings):
    return compile_context(
        document,
        session,
        session["messages"],
        snapshot,
        "",
        settings,
        "context-operation",
        replay_records=load_trace(store, session["id"], session["messages"]),
        read_only=True,
        retrieval=empty_retrieval(),
    )


def empty_retrieval():
    # Maintenance replays frozen sources and must never discover new lore.
    return {"entries": [], "ids": [], "rankings": {}, "diagnostics": [], "query": ""}


async def rebuild_local(
    store,
    oid,
    session,
    snapshot,
    document,
    settings,
    *,
    provider_factory=None,
    progress=None,
    save=None,
    retrieval=None,
):
    history = session["messages"]
    records = load_trace(store, session["id"], history)
    cfg = settings.for_role("background")
    identity = fingerprint(
        [history, snapshot, settings.model_dump(), adapter_snapshot(settings), response_origin(cfg)]
    )
    state = copy.deepcopy(progress or {})
    if state.get("identity") != identity:
        draft = copy.deepcopy(snapshot)
        draft["lore_injections"] = valid_source_lore(document, source_lore(history, snapshot, records))
        draft.pop("context_window", None)
        draft.pop(STATE, None)
        if not window_valid(draft.get("memory_archive"), history):
            draft.pop("memory_archive", None)
        state = {
            "identity": identity,
            "snapshot": draft,
            "batches": 0,
            "processed": 0,
            "total": len(history),
            "usage": [],
            "history_budget": {},
            "review_calls": 0,
        }
    draft = state["snapshot"]
    history_budget = HistoryBudget(**state["history_budget"])
    budget = CallBudget(manual=True, review=state["review_calls"], history=history_budget)
    expanded = archive_history(history, document, session["persona"])

    while True:
        count, compiled = plan_window(
            document,
            session,
            history,
            draft,
            "",
            settings,
            oid,
            empty_retrieval(),
            replay_records=records,
            read_only=True,
        )
        covered = draft.get("memory_archive", {}).get("covered_count", 0)
        if count <= covered:
            if count:
                from tavern.memories.memory import fingerprint as history_fingerprint

                draft["context_window"] = {
                    "covered_count": count,
                    "fingerprint": history_fingerprint(history[:count]),
                }
            draft["lore"], draft["lore_injections"] = compiled["next_lore"], compiled["next_lore_injections"]
            state.update(processed=len(history), snapshot=draft)
            return draft, state

        # Preserve the same citation boundary as before_story: a record may
        # already cite a newer exchange than the proposed exit boundary.
        positions = {m["id"]: i + 1 for i, m in enumerate(history)}
        target = max(
            count,
            max(
                (
                    positions.get(ref["message_id"], 0)
                    for row in read_memory(draft)["records"]
                    for ref in row.get("sources", [])
                ),
                default=0,
            ),
        )
        while target < len(history) and history[target - 1]["role"] != "assistant":
            target += 1
        end, _, _, _ = plan_batch(
            history[:target],
            expanded[:target],
            covered,
            read_memory(draft),
            document,
            cfg,
            session["persona"],
        )
        provider = (
            provider_factory(cfg)
            if provider_factory
            else await make_provider(cfg, request_timeout=settings.memory_timeout)
        )
        task = TrackerTask(
            store,
            oid,
            settings,
            session,
            document,
            history[:end],
            draft,
            phase="rebuild",
            memory=True,
            history_budget=history_budget,
            provider=provider,
        )
        task.retrieval = retrieval

        def event(kind, payload):
            if kind == "usage":
                state["usage"].append(copy.deepcopy(payload["usage"]))

        task.event = event
        try:
            await task.run(memory_start=covered, memory_budget=budget)
        finally:
            await provider.close()
            state.update(history_budget=asdict(history_budget), review_calls=budget.review)
            # Failed/incomplete batches cannot advance the coverage cursor, but
            # retain spent usage and recall limits for an explicit retry.
            if save:
                await save(copy.deepcopy(state))
        draft = task.snapshot
        finalize_sources(draft, history[:end], document=document, persona=session["persona"])
        install_coverage(draft, history[:end], rounds=completed_rounds(history))
        state.update(snapshot=draft, processed=end, batches=state["batches"] + 1)
        if save:
            await save(copy.deepcopy(state))
