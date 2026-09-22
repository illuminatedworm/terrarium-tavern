"""Read-only historical evidence selection; never changes Writer lore admission.

Frozen injections are evidence of what was supplied, not evidence of an event.
Missing historical card snapshots are explicitly labelled as current fallback.
Only messages at or before this batch's cutoff are inspected.
"""
import json

from tavern.character.cards import lore_entries, normalize
from tavern.core.model_view import state_for_model
from tavern.core.tokens import count_text
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


def select_evidence(pool, query, settings, *, exclude=(), budget=None):
    """Bounded lexical ranking independent of lore cooldown, GPU and tools.

Long records are paragraph/token bounded before ranking. IDs bind to the exact
supplied fragment, not a title that may refer to several versions.
"""
    fragments = []
    for record in pool:
        # Character slicing only creates candidates. Actual admission is always
        # measured using the configured background tokenizer, including metadata.
        for offset in range(0, len(record["text"]), 1200):
            r = {**record, "text": record["text"][offset:offset + 1200], "offset": offset}
            # Use a short local handle; the full version and fragment identity
            # remain in the retained evidence metadata. Asking a model to copy
            # 64-character hashes adds cost and caused a real evaluation reject.
            r["reference"] = digest([record["id"], offset, r["text"]])
            r["id"] = "e" + r["reference"][:16]
            if r["id"] not in exclude:
                fragments.append(r)
    if not fragments:
        return []
    index = BM25([tokenize(r["text"]) for r in fragments])
    scores = index.score(tokenize(query[:8000]), 1.2, .75)
    ranked = sorted(range(len(fragments)), key=lambda i: (
        -int(fragments[i]["kind"] == "frozen_lore"), -scores.get(i, 0), i))
    remaining = settings.memory_evidence_tokens if budget is None else budget
    selected = []
    seen_text = set()
    for i in ranked:
        r = fragments[i]
        normalized = " ".join(r["text"].split())
        if normalized in seen_text:
            continue
        if not scores.get(i) and r["kind"] not in ("frozen_lore", "state_reference_only"):
            continue
        cost = count_text(json.dumps(r, ensure_ascii=False), settings)
        if cost <= remaining:
            selected.append(r)
            seen_text.add(normalized)
            remaining -= cost
        if len(selected) == 12:
            break
    return selected
