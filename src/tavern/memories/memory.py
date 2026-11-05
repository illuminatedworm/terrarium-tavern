"""后台 Tracker and Roleplay Runtime memory/window coordination.

Only a new player send may advance the window. Generation, regeneration,
streaming, MVU callbacks and preview never promote a window by themselves.

memory_archive tracks what has been processed; context_window tracks
what has left the request. Their covered_count values must not be conflated:
periodic/manual archives can advance while every original message stays visible.
Configuration policy and examples: docs/guides/history-maintenance-settings.md.
"""

import hashlib
import json


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
    return history[window["covered_count"] :] if enabled and window_valid(window, history) else history


def render_memory(snapshot, history=None):
    from tavern.memories.memory_records import read_memory, render_document, with_source_status

    memory = read_memory(snapshot)
    text = render_document(with_source_status(memory, history) if history is not None else memory)
    return "<tavern_memory>\n" + text + "\n</tavern_memory>" if text else ""
