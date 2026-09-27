"""One serial retrieval lane for automatic and model-authored queries."""
import asyncio
import copy

from tavern.character.cards import lore_entries, normalize
from tavern.conversation.context import activate_lore, macros


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
                await self.condition.wait_for(lambda: not self.busy and
                                              (source == "agent" or not self.agent_waiters))
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


def retain_lore(document, snapshot, visible, input_id, turn_number):
    allowed = {e["id"] for e in lore_entries(document, "writer") if e["enabled"] and not e.get("constant")}
    anchors = {m["id"] for m in visible} | {input_id}
    records = [r for r in snapshot.get("lore_injections", []) if r["anchor_id"] in anchors and r["id"] in allowed]
    retained = {r["id"] for r in records}
    exiting = set()
    for eid, old in snapshot.get("lore", {}).items():
        if old.get("present") and eid not in retained:
            old.update(present=False, removed_at=turn_number)
            exiting.add(eid)
    snapshot["lore_injections"] = records
    return exiting


def inject_lore(document, session, history, snapshot, query, settings, seed, input_id, retrieval, exiting,
                *, persist=True):
    card = normalize(document)
    records = snapshot.get("lore_injections", [])
    retained = {r["id"] for r in records}
    retrieval = {**retrieval, "ids": [eid for eid in retrieval["ids"] if eid not in exiting]}
    entries, diagnostics, _ = activate_lore(
        document, history, query, snapshot.get("lore", {}), seed,
        retrieval=retrieval, query=retrieval["query"], session_id=session["id"],
        turn_number=sum(m["role"] == "user" for m in session["messages"]) + 1,
        retained=retained | exiting, recursive=False,
        max_entries=settings.lore_max_entries, max_chars=settings.lore_max_chars,
        expand_content=lambda text: macros(text, card, session["persona"], snapshot["state"]),
    )
    first_turn = not any(m["role"] == "user" for m in session["messages"])
    anchor = history[0]["id"] if first_turn and history else input_id
    added = []
    for entry in entries:
        if entry.get("constant") or entry["id"] in retained:
            continue
        content = macros(entry["content"], card, session["persona"], snapshot["state"])
        if not content.strip():
            continue
        record = {"id": entry["id"], "title": entry.get("comment") or entry.get("name") or "世界书",
                  "content": content, "anchor_id": anchor,
                  "source_ids": [m["id"] for m in history] + [input_id],
                  "evidence": copy.deepcopy(retrieval.get("evidence", {}).get(entry["id"], {})),
                  "prevent_recursion": bool(entry.get("preventRecursion"))}
        if persist:
            snapshot.setdefault("lore_injections", records).append(record)
            snapshot.setdefault("lore", {}).setdefault(entry["id"], {}).update(present=True)
        added.append(record)
    return added, diagnostics
