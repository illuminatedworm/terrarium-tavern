"""Deterministic scheduling and coverage, separate from Tracker's semantic work."""
import copy

from tavern.character.cards import lore_entries
from tavern.conversation.half_context import load_trace
from tavern.conversation.window import plan_window
from tavern.memories.memory import completed_rounds, fingerprint, window_history, window_valid
from tavern.memories.memory_records import read_memory, source_hash
from tavern.memories.memory_retention import retain_memory


def install_coverage(snapshot, history, *, rounds=None):
    snapshot["memory_archive"] = {"covered_count": len(history), "fingerprint": fingerprint(history),
                                  "last_round": completed_rounds(history) if rounds is None else rounds}
    snapshot.pop("memory_warning", None)


async def before_story(store, tid, settings, session, history, snapshot, request, document, retrieval, run_task):
    for key in ("context_window", "memory_archive"):
        if snapshot.get(key) and not window_valid(snapshot[key], history):
            snapshot.pop(key)
            store.event(tid, "memory_window_reset", {"kind": key, "reason": "维护来源不在当前分支"})
    try:
        snapshot["memory_document"] = retain_memory(read_memory(snapshot), history, settings,
            event=lambda kind, payload: store.event(tid, kind, payload))
    except ValueError as error:
        snapshot["memory_warning"] = str(error)
    edited = False
    while True:
        visible, _ = window_history(history, snapshot, enabled=settings.chunk_mode != "off")
        prepared = await retrieval.prepare(document, visible, request.text, snapshot, settings, session, tid,
                                           turn_number=sum(m["role"] == "user" for m in history) + 1)
        count, _ = plan_window(document, session, history, snapshot, request.text, settings, tid, prepared,
                              replay_records=load_trace(store, session["id"], history))
        old_count = snapshot.get("context_window", {}).get("covered_count", 0) if settings.chunk_mode != "off" else 0
        trimming = request.mode == "send" and count > old_count
        covered = snapshot.get("memory_archive", {}).get("covered_count", 0)
        memory_needed = trimming and covered < count
        edit_needed = request.allow_content_edit and not edited
        changed = False
        if memory_needed or edit_needed:
            # Existing citations may be newer than the proposed exit boundary.
            # Process their complete exchange too before advancing coverage.
            target = count
            if memory_needed:
                positions = {m["id"]: i + 1 for i, m in enumerate(history)}
                target = max(target, max((positions.get(s["message_id"], 0)
                    for r in read_memory(snapshot)["records"] for s in r.get("sources", [])), default=0))
                while target < len(history) and history[target - 1]["role"] != "assistant":
                    target += 1
            task_history = history[:target] if memory_needed else history
            result = await run_task("pre", document, task_history, snapshot,
                                    memory_start=covered if memory_needed else None,
                                    content=edit_needed, user_input=request.text)
            changed = result.document != document
            document = result.document
            snapshot.clear()
            snapshot.update(result.snapshot)
            if memory_needed:
                install_coverage(snapshot, history[:target], rounds=completed_rounds(history))
                store.event(tid, "memory_ready", {"kind": "memory", "covered_count": target})
            edited |= edit_needed
        if trimming:
            snapshot["context_window"] = {"covered_count": count, "fingerprint": fingerprint(history[:count])}
            store.event(tid, "memory_chunk", {"from": old_count, "to": count,
                                              "source_ids": [m["id"] for m in history[old_count:count]]})
        if not trimming and not changed:
            return document, prepared


async def after_story(store, tid, settings, session, history, snapshot, request, document, run_task,
                      *, committed_history):
    rounds = completed_rounds(history)
    archive = snapshot.get("memory_archive", {})
    due = (request.mode == "send" and rounds - archive.get("last_round", 0) >= settings.memory_interval_turns
           and archive.get("covered_count", 0) < len(history))
    if not (due or request.update_variables or request.allow_content_edit):
        return document, []
    try:
        result = await run_task("post", document, history, snapshot,
            memory_start=archive.get("covered_count", 0) if due else None,
            variables=request.update_variables,
            content=request.allow_content_edit or session["policy_effective"] != "directed",
            user_input=request.text, narrative=history[-1]["model"], committed_history=committed_history)
    except Exception as error:
        if request.update_variables or request.allow_content_edit:
            raise
        # An optional task is atomic too: discard its card and memory changes,
        # keeping the prior draft and coverage while the narrative may commit.
        snapshot["memory_warning"] = str(error)
        store.event(tid, "memory_failed", {"purpose": "tracker_post", "message": str(error),
                                           "kept_original": True})
        return document, []
    snapshot.clear()
    snapshot.update(result.snapshot)
    if due:
        install_coverage(snapshot, history)
        store.event(tid, "memory_ready", {"kind": "memory", "covered_count": len(history)})
    return result.document, result.patch or []


def sync_runtime_lore(mirror, document, previous_document):
    """Apply only Tracker-changed entries, retaining prior script-side changes.

    Runtime views add derived fields to entries. Comparing only old/new card
    entries keeps those views intact unless Tracker actually edited that entry.
    """
    old = {e["id"]: e for e in lore_entries(previous_document)}
    new = {e["id"]: e for e in lore_entries(document)}
    current = {e["id"]: e for e in mirror.get("lore", [])}
    for key in old.keys() - new.keys():
        current.pop(key, None)
    for key, value in new.items():
        if key not in old or old[key] != value:
            previous = old.get(key, {})
            merged = copy.deepcopy(current.get(key, previous))
            for field in previous.keys() - value.keys():
                merged.pop(field, None)
            merged.update({field: copy.deepcopy(v) for field, v in value.items() if previous.get(field) != v})
            current[key] = merged
    mirror["lore"] = list(current.values())


def merge_script_lore(document, initial_lore, processed, *, include_existing=False):
    """Fold changed runtime entries into original list/dict containers losslessly."""
    if not processed.get("loreModified"):
        return document
    result = copy.deepcopy(document)
    data = result["data"] if result.get("spec") else result
    book = data.setdefault("character_book", {})
    existing = book.get("entries", [])
    initial = {e["id"]: e for e in initial_lore}
    final = {e["id"]: e for e in processed["lore"]}
    normalized = lore_entries(document)
    keys = list(existing) if isinstance(existing, dict) else list(range(len(existing)))
    replacements = {}
    deleted = set()
    for key, entry in zip(keys, normalized):
        eid = entry["id"]
        if eid not in final and eid in initial:
            deleted.add(key)
        elif eid in final and final[eid] != (entry if include_existing else initial.get(eid, entry)):
            # Copy only actual callback changes rather than replacing every
            # original entry with a lossy normalized runtime representation.
            old = entry if include_existing else initial.get(eid, entry)
            before_callbacks = initial.get(eid, {})
            value = copy.deepcopy(existing[key])
            for field in old.keys() - final[eid].keys():
                value.pop(field, None)
            for field, replacement in final[eid].items():
                if old.get(field) == replacement:
                    continue
                # Runtime expands initialization text and decorates init-only
                # comments. Preserve their authored form unless a callback
                # actually changed that field after the Tracker sync.
                init_only = (entry.get("terrarium", {}).get("audience") == "runtime_init"
                             or "[initvar]" in entry.get("comment", "").lower())
                if init_only and field in ("content", "comment") and before_callbacks.get(field) == replacement:
                    continue
                value[field] = copy.deepcopy(replacement)
            replacements[key] = value
    if isinstance(existing, dict):
        entries = {k: replacements.get(k, copy.deepcopy(v)) for k, v in existing.items() if k not in deleted}
        for eid in final.keys() - initial.keys():
            entries[eid] = copy.deepcopy(final[eid])
    else:
        entries = [replacements.get(i, copy.deepcopy(v)) for i, v in enumerate(existing) if i not in deleted]
        entries.extend(copy.deepcopy(final[eid]) for eid in final.keys() - initial.keys())
    book["entries"] = entries
    return result


def finalize_sources(snapshot, messages, *, document, persona):
    """Bind pending citations only after all callbacks and transformations finish."""
    by_id = {m["id"]: m for m in messages}
    for record in snapshot.get("memory_document", {}).get("records", []):
        for source in record.get("sources", []):
            original = by_id.get(source["message_id"])
            if original is None:
                continue
            # The batch used expanded source text; restore the final macro view
            # before comparing. Never silently cite text no longer present.
            from tavern.conversation.scene import archive_history
            expanded = archive_history([original], document, persona)
            text = expanded[0].get("model", expanded[0]["raw"])
            if source["excerpt"] not in text:
                raise ValueError("Tracker 记忆依据在最终正文处理后发生变化，本轮未提交")
            source["fingerprint"] = source_hash(original)
