"""Read-only historical evidence selection; never changes Writer lore admission.

Frozen injections are evidence of what was supplied, not evidence of an event.
Missing historical card snapshots are explicitly labelled as current fallback.
Only messages at or before this batch's cutoff are inspected.
"""
import json

from tavern.character.cards import lore_entries, normalize
from tavern.core.model_view import state_for_model
from tavern.memories.memory_records import digest
from tavern.search.retrieval import BM25, tokenize


def evidence_pool(history, document, start, end):
    pool = {}

    def add(kind, text, anchor, identity, version, historical=True):
        if not str(text).strip():
            return
        key = digest([kind, identity, version, text])
        pool.setdefault(key, {"id": key, "kind": kind, "text": str(text), "anchor": anchor,
                              "identity": identity, "version": version, "historical": historical})

    # Include the start boundary as well as the end: a final state alone cannot
    # tell whether a transfer occurred, was reversed, or predated the batch.
    for m in history[max(0, start - 1):end]:
        snap = m.get("snapshot", {})
        historical = (snap.get("content") or {}).get("document")
        doc = historical or document or {}
        version = digest(doc)
        for r in snap.get("lore_injections", []):
            if not r.get("manually_removed"):
                add("frozen_lore", r["content"], m["id"], r["id"], digest(r["content"]))
        card = normalize(doc)
        for field in ("description", "personality", "scenario"):
            add("character", card.get(field, ""), m["id"], field, version, historical is not None)
        for e in lore_entries(doc, "writer"):
            if e["enabled"]:
                add("lore", e.get("content", ""), m["id"], e["id"], version, historical is not None)
        if m is history[max(0, start - 1)] or m is history[end - 1]:
            for key, value in state_for_model(snap.get("state", {})).items():
                add("state_reference_only", json.dumps({key: value}, ensure_ascii=False),
                    m["id"], key, digest(value))
    return list(pool.values())


def select_evidence(pool, query, settings, *, exclude=()):
    """Rank complete historical records without shortening selected evidence."""
    if settings.memory_evidence_tokens == 0:
        return []
    candidates = []
    for record in pool:
        r = {**record, "reference": digest([record["id"], record["text"]])}
        r["id"] = "e" + r["reference"][:16]
        if r["id"] not in exclude:
            candidates.append(r)
    if not candidates:
        return []
    index = BM25([tokenize(r["text"]) for r in candidates])
    scores = index.score(tokenize(query), 1.2, .75)
    ranked = sorted(range(len(candidates)), key=lambda i: (
        -int(candidates[i]["kind"] == "frozen_lore"), -scores.get(i, 0), i))
    selected = []
    seen_text = set()
    for i in ranked:
        r = candidates[i]
        normalized = " ".join(r["text"].split())
        if normalized in seen_text:
            continue
        if not scores.get(i) and r["kind"] not in ("frozen_lore", "state_reference_only"):
            continue
        selected.append(r)
        seen_text.add(normalized)
        if len(selected) == 12:
            break
    return selected
