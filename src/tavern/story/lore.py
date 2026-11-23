"""One serial retrieval lane for automatic and model-authored queries."""

import asyncio
import copy
import json
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import chain

from tavern.character.cards import is_initvar, lore_entries, lore_title, normalize
from tavern.conversation.context import activate_lore, macros
from tavern.core.cache_policy import fingerprint
from tavern.core.message_adapter import unwrapped
from tavern.core.model_view import DOCUMENT_HANDLE
from tavern.story.protocol import ToolError


def lore_preview(body: str, limit: int) -> str:
    """Keep a projected @D handle whole even if the character limit falls inside it."""
    if len(body) <= limit:
        return body
    if body[limit] == "D" or body[limit].isdecimal():
        start = body.rfind("@", 0, limit)
        if start >= 0 and (handle := DOCUMENT_HANDLE.match(body, start)) and limit < handle.end():
            limit = start
    return body[:limit]


def read_present_lore(owner, requested):
    """Use the frozen anchor or tool exchange, even after macro values change."""
    references = owner.lore_refs()
    selected = {ref: references.get(ref) for ref in requested}
    anchors = {r["id"]: r for r in owner.snapshot.get("lore_injections", [])}
    frozen, origins, recalled = {}, {}, set()
    for eid in selected.values():
        if eid in anchors and not anchors[eid].get("manually_removed"):
            frozen[eid] = anchors[eid]["content"]
            origins[eid] = anchors[eid]["anchor_id"]
        elif eid not in anchors and owner.snapshot.get("lore", {}).get(eid, {}).get("present"):
            recalled.add(eid)
        else:
            raise ToolError("INVALID_REFERENCE", "该编号没有在场的世界书正文，请重新召回。")
    if recalled:
        for eid, (body, anchor) in recalled_bodies(
            owner.lore_tool_messages(owner.visible_lore_history()), recalled
        ).items():
            frozen[eid] = body
            if anchor is not None:
                origins[eid] = anchor
    # The read itself can outlive the exchange that introduced the preview.
    owner.lore_result_metadata = {"lore_ids": list(selected.values()), "_lore_anchors": origins}
    return {"entries": [{"ref": ref, "text": frozen[eid]} for ref, eid in selected.items()]}


def recalled_bodies(messages, requested: set[str]) -> dict[str, tuple[str, str | None]]:
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
            bodies[eid] = body, message.get("_lore_anchors", {}).get(eid)
        if len(bodies) == len(requested):
            return bodies
    raise ToolError("INVALID_REFERENCE", "旧记录没有可定位的正文；可用 read_lore_entry 读取当前设定。")


@dataclass
class LoreReplay:
    """Branch-local tool history shared by compilation, presence tracking and full reads."""

    trace: Sequence[dict]
    removed_signature: str = ""

    @classmethod
    def for_branch(cls, history, snapshot, trace: Sequence[dict]) -> "LoreReplay":
        by_owner, read_calls = {}, {}
        source_ids, legacy_reads = set(), False
        for index, item in enumerate(trace):
            mid, message = item["message_id"], item["message"]
            if message["role"] == "assistant":
                read_calls[mid] = {
                    call["id"]
                    for call in message.get("tool_calls", ())
                    if call["function"]["name"] == "read_lore_full"
                }
            elif message["role"] == "tool" and message.get("lore_ids"):
                full_read = "_lore_anchors" in message or message["tool_call_id"] in read_calls.get(mid, ())
                by_owner.setdefault(mid, []).append((index, full_read))
                if full_read:
                    if "_lore_anchors" in message:
                        source_ids.update(message["_lore_anchors"].values())
                    else:
                        legacy_reads = True
        if not legacy_reads:
            # Removal also marks the anchor's own snapshot. In the common case
            # we need inspect only those sources, not every historical worldbook.
            source_snapshots = (m["snapshot"] for m in history if m["id"] in source_ids and "snapshot" in m)
            if not source_ids or not any(
                r.get("manually_removed") and r["anchor_id"] in source_ids
                for snap in chain((snapshot,), source_snapshots)
                for r in snap.get("lore_injections", ())
            ):
                return cls(trace)

        # The original anchor may have left the live window. Its removal marker
        # is still in the branch snapshots, including after a fork or import.
        admissions = {}
        for snap in chain((m.get("snapshot", {}) for m in history), (snapshot,)):
            for record in snap.get("lore_injections", ()):
                admissions.setdefault(record["anchor_id"], {})[record["id"]] = bool(
                    record.get("manually_removed")
                )

        projected, removed, origins = list(trace), set(), {}
        for owner in history:
            mid = owner["id"]
            origins.update(dict.fromkeys(admissions.get(mid, ()), mid))
            for index, full_read in by_owner.get(mid, ()):
                item = trace[index]
                message = item["message"]
                ids = message["lore_ids"]
                if not full_read:
                    for eid in ids:
                        origins.pop(eid, None)
                    continue
                # Schema 34 reads carry entry IDs only. An automatic admission
                # starts at its anchor; an independent recall starts a new source.
                anchors = message.get("_lore_anchors")
                if anchors is None:
                    anchors = {eid: origins[eid] for eid in ids if eid in origins}
                    message = {**message, "_lore_anchors": anchors}
                excluded = {eid for eid, anchor in anchors.items() if admissions.get(anchor, {}).get(eid)}
                if excluded:
                    removed.update((eid, anchors[eid]) for eid in excluded)
                    message = _without_removed_entries(message, excluded)
                if message is not item["message"]:
                    projected[index] = {**item, "message": message}
        return cls(projected, fingerprint(sorted(removed)) if removed else "")


def _without_removed_entries(message, excluded):
    # Keep the tool call paired even when all returned entries were withdrawn.
    ids = message["lore_ids"]
    result = json.loads(message["content"])
    data = result["data"]
    data["entries"] = [row for eid, row in zip(ids, data["entries"], strict=True) if eid not in excluded]
    data["notice"] = "此处已省略用户手动移除的世界书注入。"
    return {
        **message,
        "content": json.dumps(result, ensure_ascii=False, separators=(",", ":")),
        "lore_ids": [eid for eid in ids if eid not in excluded],
        "_lore_anchors": {
            eid: anchor for eid, anchor in message["_lore_anchors"].items() if eid not in excluded
        },
    }


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


def _dynamic(entry) -> bool:
    return entry is not None and entry["enabled"] and not entry.get("constant") and not is_initvar(entry)


def retained_injections(snapshot, narrative, visible, input_id):
    anchors = {m["id"] for m in visible} | {input_id}
    return [
        r
        for r in snapshot.get("lore_injections", [])
        if r["anchor_id"] in anchors and _dynamic(narrative.get(r["id"]))
    ]


def retain_lore(document, snapshot, visible, input_id, turn_number, *, recalled=()):
    narrative = {e["id"]: e for e in lore_entries(document, "narrative")}
    records = retained_injections(snapshot, narrative, visible, input_id)
    # Tool bodies already live in their native exchange, so only their identities
    # join presence tracking; they must not acquire a second system injection.
    # A setting edit clears usage state, invalidating older tool bodies as well.
    retained = {r["id"] for r in records} | {
        eid
        for eid in recalled
        if _dynamic(narrative.get(eid)) and snapshot.get("lore", {}).get(eid, {}).get("present")
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
