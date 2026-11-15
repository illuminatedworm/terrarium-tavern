"""Upgrade structured card copies without editing user text or historical tool records."""

import jsonpatch

from tavern.character.cards import upgrade_card_purposes
from tavern.character.lore_purpose import upgrade_lore_purposes
from tavern.storage.migrate_json import rewrite_json_records


def upgrade_card_record(record):
    return "document" in record and upgrade_card_purposes(record["document"])


def upgrade_snapshot(snapshot):
    return upgrade_card_record(snapshot.get("content") or {})


def _message(message):
    return upgrade_snapshot(message.get("snapshot", {}))


def _review_changes(changes):
    changed = False
    for change in changes:
        if change["kind"] == "lorebook":
            before, after = change["before"], change["after"]
            updated = upgrade_lore_purposes(before) | upgrade_lore_purposes(after)
            if updated:
                change["operations"] = jsonpatch.make_patch(before, after).patch
                changed = True
    return changed


def _pending(pending):
    changed = upgrade_snapshot(pending.get("before", {}))
    for message in pending.get("messages", []):
        changed |= _message(message)
    return _review_changes(pending.get("review_changes", [])) | changed


def _event(event):
    changed = _message(event.get("message", {}))
    for key in ("before", "after"):
        changed |= upgrade_snapshot(event.get(key, {}))
    return _review_changes(event.get("changes", [])) | changed


def _story_progress(progress):
    changed = upgrade_card_record(progress) | upgrade_snapshot(progress["snapshot"])
    changed |= _message(progress["user"])
    mirror = progress.get("script_mirror", {})
    if "lore" in mirror:
        changed |= upgrade_lore_purposes({"entries": mirror["lore"]})
    return changed


def _advisor_progress(progress):
    story = progress["story"]
    changed = upgrade_card_record(story) | upgrade_snapshot(story["snapshot"])
    for message in story["messages"]:
        changed |= _message(message)
    return changed


def _edit_item(item):
    changed = upgrade_card_record(item) | upgrade_card_record(item.get("source") or {})
    if "base_document" in item:
        changed |= upgrade_card_purposes(item["base_document"])
    return changed


def _workspace(workspace):
    changed = False
    for item in workspace.get("cards", []):
        changed |= _edit_item(item)
    return changed


def _editor_progress(progress):
    return _workspace(progress["base"]) | _workspace(progress["working"])


def upgrade_stored_lore_purposes(db):
    fields = (
        ("cards", "document", upgrade_card_purposes, ""),
        ("worldbooks", "book", upgrade_lore_purposes, ""),
        ("sessions", "card_data", upgrade_card_record, ""),
        ("messages", "snapshot", upgrade_snapshot, ""),
        ("changes", "before_state", upgrade_snapshot, ""),
        ("changes", "after_state", upgrade_snapshot, ""),
        ("turns", "pending", _pending, ""),
        ("events", "payload", _event, "AND kind IN ('input_ready','awaiting_review')"),
        ("story_progress", "payload", _story_progress, ""),
        ("advisor_runs", "progress", _advisor_progress, ""),
        ("card_author_conversations", "payload", _workspace, ""),
        ("card_author_progress", "payload", _editor_progress, ""),
        ("card_edit_versions", "document", upgrade_card_purposes, ""),
        ("card_edit_versions", "snapshot", _edit_item, ""),
        ("card_edit_saves", "result", upgrade_card_record, ""),
    )
    rewrite_json_records(db, fields, '"audience"')
