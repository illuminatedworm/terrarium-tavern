"""Roleplay Runtime window planning against Writer context admission results.

The cursor addresses original story messages, never lore or summaries. Lore
uses token capacity but no message quota and leaves with its anchor; only story needs archiving.
"""

import copy

from tavern.conversation.context import compile_context
from tavern.conversation.half_compaction import local_window_enabled
from tavern.core.context_usage import exceeds


def plan_window(
    document,
    session,
    history,
    snapshot,
    text,
    settings,
    seed,
    retrieval=None,
    *,
    replay_records=None,
    read_only=False,
):
    from tavern.memories.memory import fingerprint, window_valid

    window = snapshot.get("context_window")
    start = window["covered_count"] if window_valid(window, history) and local_window_enabled(settings) else 0
    count = start
    inputs = [i for i, m in enumerate(history) if m["role"] == "user"]
    protected = inputs[-2] if len(inputs) >= 2 else 0
    capacity = min(settings.daily_context_budget, settings.context_tokens - settings.reply_tokens - 2048)
    while True:
        draft = copy.deepcopy(snapshot)
        if count:
            draft["context_window"] = {"covered_count": count, "fingerprint": fingerprint(history[:count])}
        context = compile_context(
            document,
            session,
            history,
            draft,
            text,
            settings,
            seed,
            retrieval=retrieval,
            enforce_budget=False,
            replay_records=replay_records,
            read_only=read_only,
        )
        # One provider sample or rejection justifies a single pressure batch:
        # removing history changes the request prefix, so resolve() no longer
        # treats that old observation as covering the next compilation.
        over = context["window_message_count"] >= settings.history_message_cap or exceeds(
            context["context_usage"], capacity * settings.context_pressure_trigger
        )
        if not local_window_enabled(settings) or not over or count >= protected:
            return count, context
        removed = 0
        candidate = count
        # Count story messages only; anchored lore leaves with its message when
        # compile_context filters records against the remaining visible IDs.
        while candidate < protected:
            message = history[candidate]
            removed += message["role"] in ("user", "assistant")
            candidate += 1
            if message["role"] == "assistant" and removed >= settings.chunk_size:
                break
        if candidate <= count:
            return count, context
        count = candidate
