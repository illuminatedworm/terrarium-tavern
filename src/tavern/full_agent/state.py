"""Branch-local model-authored memory; no extraction or summarization jobs."""
import copy
import hashlib
import uuid


def new_state():
    return {"version": 1, "window": {"id": uuid.uuid4().hex, "number": 1, "start": 0},
            "handoff": {"text": "", "revision": 0, "stale": False}, "notes": {}}


def full_state(snapshot):
    return snapshot.setdefault("full_agent", new_state())


def source_hash(message):
    return hashlib.sha256(message["raw"].encode()).hexdigest()


def note_view(note, history):
    by_id = {m["id"]: (i + 1, m) for i, m in enumerate(history)}
    sources, stale = [], False
    for source in note.get("sources", []):
        found = by_id.get(source["message_id"])
        stale |= not found or source_hash(found[1]) != source["hash"]
        if found:
            sources.append(f"H{found[0]}")
    return {k: copy.deepcopy(note[k]) for k in ("key", "content", "kind", "status", "revision")} | {
        "sources": sources, "stale": bool(stale)}


def rollover(state, history, handoff, keep_recent):
    """Retain whole user/assistant exchanges; never leave a dangling tool block."""
    start = max(state["window"]["start"], len(history) - keep_recent)
    if 0 < start < len(history) and history[start]["role"] == "assistant":
        start -= 1
    state["handoff"] = {"text": handoff, "revision": state["handoff"]["revision"] + 1,
                           "stale": False}
    state["window"] = {"id": uuid.uuid4().hex, "number": state["window"]["number"] + 1, "start": start}


def mark_edited(snapshot, message_id, history):
    state = snapshot.get("full_agent")
    if state and any(m["id"] == message_id for m in history):
        state["handoff"]["stale"] = bool(state["handoff"]["text"])
        state.setdefault("edited_sources", [])
        if message_id not in state["edited_sources"]:
            state["edited_sources"].append(message_id)
        # Retire potentially stale tool continuations, without discarding dialogue.
        state["window"]["id"] = uuid.uuid4().hex


def remap_sources(snapshot, ids):
    state = snapshot.get("full_agent")
    if state:
        for ref, value in state.get("model_references", {}).items():
            state["model_references"][ref] = ids.get(value, value)
        for note in state["notes"].values():
            for source in note.get("sources", []):
                source["message_id"] = ids.get(source["message_id"], source["message_id"])
        state["edited_sources"] = [ids.get(mid, mid) for mid in state.get("edited_sources", [])]
