"""Branch-local model-authored memory; no extraction or summarization jobs."""

import copy
import hashlib
import uuid

from tavern.core.history_refs import message_positions, message_ref

# v3: chapter/current handoff is model-maintained. Earlier formats are rejected,
# including unreleased v2 development data, rather than migrated.
VERSION = 3


def new_state():
    return {
        "version": VERSION,
        "window": {"id": uuid.uuid4().hex, "number": 1, "start": 0},
        "handoff": {"chapters": [], "current": [], "revision": 0},
        "notes": {},
        "plot": {"text": "", "revision": 0},
    }


def full_state(snapshot):
    state = snapshot.setdefault("full_agent", new_state())
    if state.get("version") != VERSION:
        raise ValueError(
            f"Full Agent 状态格式版本 {state.get('version')} 不受支持；此对话来自旧版本，请新建对话"
        )
    state.setdefault("plot", {"text": "", "revision": 0})
    return state


def read_full_state(snapshot):
    """Validate a saved read-only view without installing defaults into its source."""
    state = snapshot.get("full_agent")
    return full_state({"full_agent": copy.deepcopy(state)}) if state is not None else {}


def source_hash(message):
    return hashlib.sha256(message["raw"].encode()).hexdigest()


def note_view(note, history):
    positions = message_positions(history)
    sources, stale = [], False
    for source in note.get("sources", []):
        position = positions.get(source["message_id"])
        stale |= position is None or source_hash(history[position - 1]) != source["hash"]
        if position is not None:
            sources.append(message_ref(position))
    return {k: copy.deepcopy(note[k]) for k in ("key", "content", "kind", "status", "revision")} | {
        "sources": sources,
        "stale": bool(stale),
    }


def rollover_start(state, history, keep_recent):
    """First retained 0-based position; whole user/assistant exchanges stay together."""
    start = max(state["window"]["start"], len(history) - keep_recent)
    if 0 < start < len(history) and history[start]["role"] == "assistant":
        start -= 1
    return start


def rollover(state, history, handoff, keep_recent):
    """Switch after coverage checks; None keeps memory and its source revision intact."""
    start = rollover_start(state, history, keep_recent)
    if handoff is not None:
        state["handoff"] = {**handoff, "revision": state["handoff"]["revision"] + 1}
    state["window"] = {"id": uuid.uuid4().hex, "number": state["window"]["number"] + 1, "start": start}


def mark_edited(snapshot, message_id, history, *, notify_ai=False):
    state = snapshot.get("full_agent")
    # Handoff and note staleness are derived from source fingerprints. Retiring
    # tool context is an explicit edit choice: quiet edits must retain complete
    # note/version evidence.
    if state and notify_ai and any(m["id"] == message_id for m in history):
        state["window"]["id"] = uuid.uuid4().hex


def remap_sources(snapshot, ids):
    state = snapshot.get("full_agent")
    if state:
        from tavern.full_agent.handoff import remap_handoff

        remap_handoff(state["handoff"], ids)
        for ref, value in state.get("model_references", {}).items():
            state["model_references"][ref] = ids.get(value, value)
        for note in state["notes"].values():
            for source in note.get("sources", []):
                source["message_id"] = ids.get(source["message_id"], source["message_id"])
