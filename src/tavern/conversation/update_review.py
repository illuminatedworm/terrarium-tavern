"""Review only actual variables and card content; commit every turn atomically."""

import json

import jsonpatch

from tavern.character.cards import normalize
from tavern.core.model_view import state_for_model
from tavern.core.update_policy import normalize_policy


def review_snapshot(snapshot, document=None):
    variables = state_for_model(snapshot.get("state", {}))
    if variables.get("stat_data") == {}:
        variables.pop("stat_data")
    card = normalize((snapshot.get("content") or {}).get("document", document or {}))
    return {
        "variables": variables,
        "script_variables": {k: v for k, v in snapshot.get("scopes", {}).items() if v},
        "character": {k: v for k, v in card.items() if k != "character_book"},
        "lorebook": card.get("character_book") or {},
    }


def review_changes(before, after, document=None):
    old, new = review_snapshot(before, document), review_snapshot(after, document)
    labels = {
        "variables": "剧情变量",
        "script_variables": "脚本变量",
        "character": "角色设定",
        "lorebook": "世界书",
    }
    return [
        {
            "kind": key,
            "label": label,
            "before": old[key],
            "after": new[key],
            "operations": jsonpatch.make_patch(old[key], new[key]).patch,
        }
        for key, label in labels.items()
        if old[key] != new[key]
    ]


def finish_turn(store, tid, session, pending, snapshot, *, draft=None):
    """Shared review boundary for both agents, script actions and manual edits.

    Notes, retrieval bookkeeping and schema/cache maintenance are saved with the
    turn but cannot trigger review. Pending turns still commit or reject as one
    unit; no memory or narrative is saved early while a real edit awaits review.
    """
    if normalize_policy(session["policy_effective"]) != "review":
        store.commit_turn(tid, pending)
        return
    changes = review_changes(pending["before"], snapshot, store.session_card(session["id"])["document"])
    if changes:
        pending["review_changes"] = changes
        fields = {"status": "awaiting_review", "pending": json.dumps(pending, ensure_ascii=False)}
        if draft is not None:
            fields["draft"] = draft
        store.set_turn(tid, **fields)
        store.event(
            tid, "awaiting_review", {"before": pending["before"], "after": snapshot, "changes": changes}
        )
    else:
        store.commit_turn(tid, pending)
