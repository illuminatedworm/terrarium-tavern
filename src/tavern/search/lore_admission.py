"""Evidence gates shared by normal, recursive and degraded lexical retrieval.

These are explainable rules, not calibrated probabilities. Ranking never grants
admission by itself. A common/body-only short key cannot establish a subject.
"""

import re
from collections import Counter


def keyword_evidence(entries, query, user_input):
    from tavern.conversation.context import matches

    frequencies = Counter(k.casefold() for e in entries for k in set(e.get("keys", [])))
    accepted, evidence = set(), {}
    for entry in entries:
        heading = " ".join(str(entry.get(k, "")) for k in ("name", "comment"))
        heading += " " + entry.get("content", "").split("\n", 1)[0]
        hits, useful = [], []
        for key in entry.get("keys", []):
            if not matches(key, query, entry):
                continue
            explicit = key.startswith("/") and key.rfind("/") > 0
            explicit |= bool(entry.get("use_regex") and re.search(r"[\^$*+?{}\[\]|()]", key))
            common = (
                frequencies[key.casefold()] >= 3 and frequencies[key.casefold()] / max(1, len(entries)) > 0.1
            )
            subject = matches(key, heading, entry)
            item = {
                "key": key,
                "source": "input" if matches(key, user_input, entry) else "clue",
                "subject": subject,
                "common": common,
            }
            hits.append(item)
            if explicit or (len(key.strip()) >= 2 and (subject or not common)):
                useful.append(item)
        evidence[entry["id"]] = {"keyword_hits": hits, "lexical_admitted": bool(useful)}
        if useful:
            accepted.add(entry["id"])
    return accepted, evidence


def recursive_candidates(entries, accepted, examined, depth):
    """Expand only this request's admitted seeds; frozen history is never a seed."""
    from tavern.conversation.context import matches

    by_id = {e["id"]: e for e in entries}
    parents = [by_id[eid] for eid in accepted if eid in by_id and not by_id[eid].get("preventRecursion")]
    found = {}
    for entry in entries:
        if entry["id"] in examined or entry.get("excludeRecursion"):
            continue
        sources = [
            p["id"]
            for p in parents
            if any(matches(k, p.get("content", ""), entry) for k in entry.get("keys", []))
        ]
        if sources:
            found[entry["id"]] = {"recursive_parents": sources, "recursion_depth": depth}
    return found
