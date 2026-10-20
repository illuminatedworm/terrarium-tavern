"""Deterministic retention for the active memory projection.

Story messages remain in the branch. Retention only removes automatic records
from the current projection; manual records and source-stale records are never
silently changed. Every pass uses the source's latest completed story round,
not the model's wording or the time a consolidation request happened.
"""

import copy

from tavern.core.tokens import count_text
from tavern.memories.memory_records import is_retained_record, render_document, source_hash


def _round_positions(history):
    positions = {}
    completed = 0
    pending = []
    for index, message in enumerate(history):
        mid = message["id"]
        if message["role"] == "user":
            pending.append((mid, index))
            continue
        if message["role"] == "assistant" and pending:
            completed += 1
            for user_id, user_index in pending:
                positions[user_id] = (completed, user_index)
            pending.clear()
        positions[mid] = (completed, index)
    for user_id, user_index in pending:
        positions[user_id] = (completed, user_index)
    return completed, positions


def retain_memory(memory, history, settings, *, event=None):
    result = copy.deepcopy(memory)
    records = result["records"]
    total_rounds, positions = _round_positions(history)
    messages = {m["id"]: m for m in history}
    removed = {"callback": [], "current": [], "episode_age": [], "episode_budget": []}

    def eligible(record):
        if (
            record.get("manual")
            or record.get("source_stale")
            or record["status"] in ("deleted", "superseded")
        ):
            return False
        for source in record.get("sources", []):
            message = messages.get(source["message_id"])
            if (
                message is None
                or not source.get("fingerprint")
                or source_hash(message) != source["fingerprint"]
            ):
                return False
        return True

    def age_key(record):
        return max(
            (positions.get(s["message_id"], (-1, -1)) for s in record.get("sources", [])), default=(-1, -1)
        )

    def retire(record, reason):
        record["status"] = "superseded"
        removed[reason].append(record["id"])

    callbacks = [r for r in records if r.get("callback") and r["status"] == "active" and eligible(r)]
    while callbacks and (
        len(callbacks) > 3 or count_text("\n".join(r["content"] for r in callbacks), settings) > 300
    ):
        retire(callbacks.pop(0), "callback")

    current = [
        r
        for r in records
        if r["category"] == "current" and r["status"] not in ("deleted", "superseded") and not r.get("manual")
    ]
    removable_current = sorted((r for r in current if eligible(r)), key=age_key)
    while len(current) > settings.memory_current_max_records and removable_current:
        oldest = removable_current.pop(0)
        retire(oldest, "current")
        current.remove(oldest)
    if len(current) > settings.memory_current_max_records:
        raise ValueError("当前进展超过条目上限，且剩余条目受来源保护；请手动整理或提高上限")

    cooldown = settings.memory_episode_cooldown_rounds
    if cooldown:
        for record in records:
            if record["category"] != "episode" or not eligible(record):
                continue
            latest_round = age_key(record)[0]
            if latest_round >= 0 and total_rounds - latest_round >= cooldown:
                retire(record, "episode_age")

    while count_text(render_document(result), settings) > settings.memory_capacity_tokens:
        episodes = [r for r in records if r["category"] == "episode" and eligible(r)]
        if not episodes:
            raise ValueError("活跃记忆超过容量；可退出的经历已用尽，请提高容量或手动合并受保护条目")
        retire(min(episodes, key=age_key), "episode_budget")

    result["records"] = [r for r in records if is_retained_record(r)]
    if event and any(removed.values()):
        counts = {key: len(value) for key, value in removed.items()}
        event(
            "memory_retention",
            {
                "purpose": "memory",
                "counts": counts,
                "record_ids": removed,
                "message": "记忆准出："
                + "、".join(f"{key} {count}" for key, count in counts.items() if count),
            },
        )
    return result
