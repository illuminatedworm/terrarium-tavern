"""One serial retrieval lane for automatic and model-authored queries."""

import asyncio
import copy
import json

from tavern.character.cards import lore_entries, lore_title, normalize
from tavern.conversation.context import activate_lore, macros
from tavern.core.message_adapter import unwrapped
from tavern.story.protocol import ToolError


def read_present_lore(owner, requested):
    """Use the frozen anchor or tool exchange, even after macro values change."""
    references = owner.lore_refs()
    selected = {ref: references.get(ref) for ref in requested}
    anchors = {r["id"]: r for r in owner.snapshot.get("lore_injections", [])}
    frozen, recalled = {}, set()
    for eid in selected.values():
        if eid in anchors and not anchors[eid].get("manually_removed"):
            frozen[eid] = anchors[eid]["content"]
        elif eid not in anchors and owner.snapshot.get("lore", {}).get(eid, {}).get("present"):
            recalled.add(eid)
        else:
            raise ToolError("INVALID_REFERENCE", "该编号没有在场的世界书正文，请重新召回。")
    if recalled:
        frozen.update(recalled_bodies(owner.lore_tool_messages(owner.visible_lore_history()), recalled))
    # The read itself can outlive the exchange that introduced the preview.
    owner.recall_ids = list(selected.values())
    return {"entries": [{"ref": ref, "text": frozen[eid]} for ref, eid in selected.items()]}


def recalled_bodies(messages, requested: set[str]) -> dict[str, str]:
    bodies = {}
    # A re-admitted entry can have older versions in the same window. Parse
    # only the newest exchange for each requested identity, once per batch.
    for message in reversed(list(messages)):
        ids = message["lore_ids"]
        if all(eid not in requested or eid in bodies for eid in ids):
            continue
        data = json.loads(message["content"])["data"]
        matched = "matches" in data
        for eid, row in zip(ids, data["matches" if matched else "entries"], strict=True):
            if eid not in requested or eid in bodies:
                continue
            if "preview" in row:
                body = unwrapped(row["preview"], "lorebook") + message["_lore_tails"][eid]
            else:
                # Short entries and pre-preview recalls already contain the entire body.
                body = unwrapped(row["text"], "lorebook") if matched else row["text"]
            bodies[eid] = body
        if len(bodies) == len(requested):
            return bodies
    raise ToolError("INVALID_REFERENCE", "旧记录没有可定位的正文；可用 read_lore_entry 读取当前设定。")


class RetrievalLane:
    def __init__(self, engine):
        self.engine = engine
        self.condition = asyncio.Condition()
        self.busy = False
        self.agent_waiters = 0

    async def prepare(self, *args, source="auto", **kwargs):
        async with self.condition:
            if source == "agent":
                self.agent_waiters += 1
            try:
                await self.condition.wait_for(
                    lambda: not self.busy and (source == "agent" or not self.agent_waiters)
                )
                self.busy = True
            finally:
                if source == "agent":
                    self.agent_waiters -= 1
                self.condition.notify_all()
        try:
            # Do not overlap an abandoned to_thread/model job with its successor.
            # Cancellation waits for the existing search to finish before releasing the lane.
            job = asyncio.create_task(self.engine.prepare(*args, **kwargs))
            try:
                return await asyncio.shield(job)
            except asyncio.CancelledError:
                await job
                raise
        finally:
            async with self.condition:
                self.busy = False
                self.condition.notify_all()


def retain_lore(document, snapshot, visible, input_id, turn_number, *, recalled=()):
    allowed = {e["id"] for e in lore_entries(document, "narrative") if e["enabled"] and not e.get("constant")}
    anchors = {m["id"] for m in visible} | {input_id}
    records = [
        r for r in snapshot.get("lore_injections", []) if r["anchor_id"] in anchors and r["id"] in allowed
    ]
    # Tool bodies already live in their native exchange, so only their identities
    # join presence tracking; they must not acquire a second system injection.
    # A setting edit clears usage state, invalidating older tool bodies as well.
    retained = {r["id"] for r in records} | {
        eid for eid in recalled if eid in allowed and snapshot.get("lore", {}).get(eid, {}).get("present")
    }
    exiting = set()
    for eid, old in snapshot.get("lore", {}).items():
        if old.get("present") and eid not in retained:
            old.update(present=False, removed_at=turn_number)
            exiting.add(eid)
    snapshot["lore_injections"] = records
    return exiting


def inject_lore(
    document, session, history, snapshot, query, settings, seed, input_id, retrieval, exiting, *, persist=True
):
    card = normalize(document)
    records = snapshot.get("lore_injections", [])
    retained = {r["id"] for r in records} | {
        eid for eid, old in snapshot.get("lore", {}).items() if old.get("present")
    }
    retrieval = {**retrieval, "ids": [eid for eid in retrieval["ids"] if eid not in exiting]}
    entries, diagnostics, _ = activate_lore(
        document,
        history,
        query,
        snapshot.get("lore", {}),
        seed,
        retrieval=retrieval,
        query=retrieval["query"],
        session_id=session["id"],
        turn_number=sum(m["role"] == "user" for m in session["messages"]) + 1,
        retained=retained | exiting,
        max_entries=settings.lore_max_entries,
        title_heading=settings.retrieval.title_in_retrieval,
        expand_content=lambda text: macros(
            text, card, session["persona"], snapshot["state"], snapshot.get("scopes")
        ),
    )
    first_turn = not any(m["role"] == "user" for m in session["messages"])
    anchor = history[0]["id"] if first_turn and history else input_id
    added = []
    for entry in entries:
        if entry.get("constant") or entry["id"] in retained:
            continue
        content = macros(
            entry["content"], card, session["persona"], snapshot["state"], snapshot.get("scopes")
        )
        if not content.strip():
            continue
        record = {
            "id": entry["id"],
            "title": lore_title(entry) or "世界书",
            "content": content,
            "anchor_id": anchor,
            "source_ids": [m["id"] for m in history] + [input_id],
            "evidence": copy.deepcopy(retrieval.get("evidence", {}).get(entry["id"], {})),
            "prevent_recursion": bool(entry.get("preventRecursion")),
        }
        if persist:
            snapshot.setdefault("lore_injections", records).append(record)
            snapshot.setdefault("lore", {}).setdefault(entry["id"], {}).update(present=True)
        added.append(record)
    return added, diagnostics
