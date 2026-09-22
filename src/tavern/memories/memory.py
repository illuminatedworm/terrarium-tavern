"""后台 Tracker and Roleplay Runtime memory/window coordination.

Only a new player send may advance the window. Generation, regeneration,
streaming, MVU callbacks and preview never promote a window by themselves.

memory_archive tracks what has been processed; context_window tracks
what has left the request. Their covered_count values must not be conflated:
periodic/manual archives can advance while every original message stays visible.
Configuration policy and examples: docs/guides/history-maintenance-settings.md.
"""

import copy
import hashlib
import json
import time


def fingerprint(history):
    payload = [(m["id"], m.get("model", m["raw"])) for m in history]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False).encode()).hexdigest()


def completed_rounds(history):
    rounds, pending = 0, False
    for message in history:
        if message["role"] == "user":
            pending = True
        elif message["role"] == "assistant" and pending:
            rounds += 1
            pending = False
    return rounds


def window_valid(window, history):
    if not window:
        return False
    count = window.get("covered_count", 0)
    return 0 < count <= len(history) and fingerprint(history[:count]) == window.get("fingerprint")


def window_history(history, snapshot, *, enabled=True):
    window = snapshot.get("context_window")
    visible = history[window["covered_count"]:] if enabled and window_valid(window, history) else history
    return visible, ""


def render_memory(snapshot, history=None):
    from tavern.memories.memory_records import read_memory, render_document, with_source_status
    memory = read_memory(snapshot)
    text = render_document(with_source_status(memory, history) if history is not None else memory)
    return "<tavern_memory>\n" + text + "\n</tavern_memory>" if text else ""


class Tracker:
    """后台 Tracker: consolidate branch memory and perform authorized maintenance.

    Runtime calls prepare() to enforce coverage before moving its history window.
    Model output is a draft; only Runtime may commit it. No autonomous loop.
    """

    async def maintain(self, turn_agent, provider=None):
        """Run Tracker tool mode on the shared draft when Runtime authorizes it."""
        return await turn_agent._run(provider, maintenance=True)

    def __init__(self, store):
        self.store = store

    async def prepare(self, tid, document, session, history, snapshot, request, settings, summarize,
                      *, retrieval=None, periodic=True, call_budget=None):
        from tavern.conversation.window import plan_window
        from tavern.memories.memory_engine import CallBudget, consolidate
        from tavern.memories.memory_records import (
            install_memory,
            read_memory,
            usage_fields,
            with_source_status,
        )

        draft = copy.deepcopy(snapshot)
        for key in ("context_window", "memory_archive"):
            if draft.get(key) and not window_valid(draft[key], history):
                draft.pop(key)
                self.store.event(tid, "memory_window_reset", {"reason": "维护来源不在当前分支", "kind": key})
        if request.mode != "send":
            return
        window = draft.get("context_window")
        old_count = window["covered_count"] if window and settings.chunk_mode != "off" else 0
        rounds = completed_rounds(history)
        count, _ = plan_window(document, session, history, draft, request.text, settings, tid, retrieval)
        trimming = count > old_count
        eligible = len(history) - 4
        while eligible > 0 and history[eligible - 1]["role"] != "assistant":
            eligible -= 1
        # Closing the window retains all original messages; periodic archives may
        # cover all completed exchanges because they cannot move the window.
        if settings.chunk_mode == "off":
            eligible = len(history)
        for kind, key, interval in (
            ("memory", "memory_archive", settings.memory_interval_turns),
        ):
            archive = draft.get(key)
            covered = archive.get("covered_count", 0) if archive else 0
            due = periodic and rounds - (archive or {}).get("last_round", 0) >= interval
            target = max(count if trimming else 0, (len(history) if kind == "memory" else eligible) if due else 0)
            # Existing tool memories may cite recent protected messages. Include
            # those sources without allowing the archive to move the window.
            if kind == "memory" and target > covered:
                positions = {m["id"]: i + 1 for i, m in enumerate(history)}
                remembered_count = max(
                    (positions.get(source["message_id"], 0)
                     for m in draft.get("memory_document", {}).get("records", []) for source in m.get("sources", [])),
                    default=0,
                )
                target = max(target, remembered_count)
            if target <= covered:
                continue
            self.store.event(tid, "archive_started", {"kind": kind, "from": covered, "to": target,
                                                     "forced": trimming and covered < count})
            try:
                usage = {}

                async def generate(messages, *, phase="memory", output_tokens=None):
                    nonlocal usage
                    begun = time.perf_counter()
                    result, usage = await summarize(messages, phase=phase, output_tokens=output_tokens)
                    if usage is not None:
                        self.store.event(tid, "usage", {"purpose": phase, "usage": usage_fields(usage),
                                                       "duration_s": time.perf_counter() - begun, "calls": 1})
                    return result

                cfg = settings.for_role("background")
                result = await consolidate(history[:target], cfg, generate,
                    memory=with_source_status(read_memory(draft), history), start=covered, document=document,
                    persona=session["persona"], budget=call_budget or CallBudget(),
                    event=lambda kind, payload: self.store.event(tid, kind, payload))
                install_memory(draft, result)
                draft[key] = {"fingerprint": fingerprint(history[:target]), "covered_count": target,
                              "last_round": rounds}
                draft.pop("memory_warning", None)
                cached = False
                self.store.event(tid, "memory_ready",
                                 {"kind": kind, "covered_count": target, "cached": bool(cached), "result": result})
            except Exception as error:
                message = str(error).strip()
                if not message:
                    message = (f"记忆模型请求超过 {settings.memory_timeout} 秒未完成"
                               if isinstance(error, TimeoutError) else type(error).__name__)
                draft["memory_warning"] = message
                self.store.event(tid, "memory_failed",
                                 {"message": message, "kept_original": True})
        if trimming:
            if any(draft.get(key, {}).get("covered_count", 0) < count for key in ("memory_archive",)):
                raise ValueError("裁剪前的记忆整理失败，原窗口已保留；请重试或调整窗口与维护预算")
            draft["context_window"] = {
                "fingerprint": fingerprint(history[:count]), "covered_count": count,
            }
            self.store.event(tid, "memory_chunk", {"from": old_count, "to": count,
                                                   "source_ids": [m["id"] for m in history[old_count:count]]})
        snapshot.clear()
        snapshot.update(draft)
