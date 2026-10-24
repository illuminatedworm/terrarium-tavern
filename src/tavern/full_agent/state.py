"""Branch-local model-authored memory; no extraction or summarization jobs."""

import copy
import uuid

from tavern.core.history_refs import message_positions, message_ref

# v4 uses immutable source identities and pre-turn snapshots. Earlier stories are rejected.
VERSION = 4


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


def note_view(note, history, *, pending_reply_id=None):
    positions = message_positions(history)
    pending_source = pending_reply_id is not None and any(
        source["message_id"] == pending_reply_id for source in note.get("sources", [])
    )
    if pending_source:
        # A rejected draft keeps its reserved identity for existing note refs,
        # but must not become readable history or authorize new citations.
        positions[pending_reply_id] = len(history) + 1
    result = {k: copy.deepcopy(note[k]) for k in ("key", "content", "kind", "status", "revision")} | {
        "sources": [message_ref(positions[source["message_id"]]) for source in note.get("sources", [])],
    }
    if pending_source:
        result["notice"] = (
            f"来源 {message_ref(positions[pending_reply_id])} 的正文已撤回，暂不可读取或用于新的笔记引用；"
            "笔记原文与来源引用保留，请在重新交付正文后核对。"
        )
    return result


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
