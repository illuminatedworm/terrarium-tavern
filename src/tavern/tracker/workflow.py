"""Deterministic scheduling and coverage, separate from Tracker's semantic work."""

from tavern.conversation.half_compaction import local_window_enabled
from tavern.conversation.half_context import load_trace
from tavern.conversation.window import plan_window
from tavern.memories.memory import completed_rounds, fingerprint, window_history, window_valid
from tavern.memories.memory_archive import read_archive
from tavern.memories.memory_records import install_memory, read_memory, source_hash
from tavern.memories.memory_retention import retain_memory


def install_coverage(snapshot, history, *, rounds=None):
    snapshot["memory_archive"] = {
        "covered_count": len(history),
        "fingerprint": fingerprint(history),
        "last_round": completed_rounds(history) if rounds is None else rounds,
    }
    snapshot.pop("memory_warning", None)


async def before_story(
    store, tid, settings, session, history, snapshot, request, document, retrieval, run_task
):
    for key in ("context_window", "memory_archive"):
        if snapshot.get(key) and not window_valid(snapshot[key], history):
            snapshot.pop(key)
            store.event(tid, "memory_window_reset", {"kind": key, "reason": "维护来源不在当前分支"})
    try:
        install_memory(
            snapshot,
            retain_memory(
                read_memory(snapshot),
                history,
                settings,
                event=lambda kind, payload: store.event(tid, kind, payload),
            ),
        )
    except ValueError as error:
        snapshot["memory_warning"] = str(error)
    edited = False
    while True:
        visible, _ = window_history(history, snapshot, enabled=local_window_enabled(settings))
        prepared = await retrieval.prepare(
            document,
            visible,
            request.text,
            snapshot,
            settings,
            session,
            tid,
            turn_number=sum(m["role"] == "user" for m in history) + 1,
            archive=read_archive(history, snapshot),
        )
        count, _ = plan_window(
            document,
            session,
            history,
            snapshot,
            request.text,
            settings,
            tid,
            prepared,
            replay_records=load_trace(store, session["id"], history),
        )
        old_count = (
            snapshot.get("context_window", {}).get("covered_count", 0)
            if local_window_enabled(settings)
            else 0
        )
        # Retries start at the same pre-turn snapshot. Recompute this exchange's
        # exit once; attempt count must not become another story/window advance.
        trimming = count > old_count
        covered = snapshot.get("memory_archive", {}).get("covered_count", 0)
        memory_needed = trimming and covered < count
        edit_needed = request.allow_content_edit and not edited
        changed = False
        if memory_needed or edit_needed:
            # Existing citations may be newer than the proposed exit boundary.
            # Process their complete exchange too before advancing coverage.
            target = count
            if memory_needed:
                positions = {m["id"]: i + 1 for i, m in enumerate(history)}
                target = max(
                    target,
                    max(
                        (
                            positions.get(s["message_id"], 0)
                            for r in read_memory(snapshot)["records"]
                            for s in r.get("sources", [])
                        ),
                        default=0,
                    ),
                )
                while target < len(history) and history[target - 1]["role"] != "assistant":
                    target += 1
            task_history = history[:target] if memory_needed else history
            result = await run_task(
                "pre",
                document,
                task_history,
                snapshot,
                memory_start=covered if memory_needed else None,
                content=edit_needed,
                user_input=request.text,
            )
            changed = result.document != document
            document = result.document
            snapshot.clear()
            snapshot.update(result.snapshot)
            if memory_needed:
                install_coverage(snapshot, history[:target], rounds=completed_rounds(history))
                store.event(tid, "memory_ready", {"kind": "memory", "covered_count": target})
            edited |= edit_needed
        if trimming:
            snapshot["context_window"] = {"covered_count": count, "fingerprint": fingerprint(history[:count])}
            store.event(
                tid,
                "memory_chunk",
                {"from": old_count, "to": count, "source_ids": [m["id"] for m in history[old_count:count]]},
            )
        if not trimming and not changed:
            return document, prepared


async def after_story(
    store, tid, settings, session, history, snapshot, request, document, run_task, *, committed_history
):
    rounds = completed_rounds(history)
    archive = snapshot.get("memory_archive", {})
    due = rounds - archive.get("last_round", 0) >= settings.memory_interval_turns and archive.get(
        "covered_count", 0
    ) < len(history)
    if not (due or request.update_variables or request.allow_content_edit):
        return document, []
    try:
        result = await run_task(
            "post",
            document,
            history,
            snapshot,
            memory_start=archive.get("covered_count", 0) if due else None,
            variables=request.update_variables,
            content=request.allow_content_edit,
            user_input=request.text,
            narrative=history[-1]["model"],
            committed_history=committed_history,
        )
    except Exception as error:
        if request.update_variables or request.allow_content_edit:
            raise
        # An optional task is atomic too: discard its card and memory changes,
        # keeping the prior draft and coverage while the narrative may commit.
        snapshot["memory_warning"] = str(error)
        store.event(
            tid, "memory_failed", {"purpose": "tracker_post", "message": str(error), "kept_original": True}
        )
        return document, []
    snapshot.clear()
    snapshot.update(result.snapshot)
    if due:
        install_coverage(snapshot, history)
        store.event(tid, "memory_ready", {"kind": "memory", "covered_count": len(history)})
    return result.document, result.patch or []


def finalize_sources(snapshot, messages, *, document, persona):
    """Bind pending citations only after all callbacks and transformations finish."""
    by_id = {m["id"]: m for m in messages}
    for record in snapshot.get("memory_document", {}).get("records", []):
        for source in record.get("sources", []):
            original = by_id.get(source["message_id"])
            if original is None:
                continue
            # The batch used expanded source text; restore the final macro view
            # before comparing. Never silently cite text no longer present.
            from tavern.conversation.scene import archive_history

            expanded = archive_history([original], document, persona)
            text = expanded[0].get("model", expanded[0]["raw"])
            if source["excerpt"] not in text:
                raise ValueError("Tracker 记忆依据在最终正文处理后发生变化，本轮未提交")
            source["fingerprint"] = source_hash(original)
