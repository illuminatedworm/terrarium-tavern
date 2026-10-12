"""Recognize complete current versions in compiled sources and tool exchanges.

Only runtime source identities and typed tool payloads locate targets. Prose,
search previews, note directories and opaque compaction do not grant evidence.
Model-authored tool arguments count just like tool results, including complete
replacement texts. Nothing from this bookkeeping is added to model messages.
"""
import json

import jsonpatch
import jsonpointer

from tavern.core.cache_policy import fingerprint
from tavern.core.message_adapter import unwrapped
from tavern.core.model_view import state_for_model
from tavern.full_agent.content import CHARACTER_FIELDS, card_data, editable_entry, entry_rows, lore_references
from tavern.full_agent.protocol import succeeded
from tavern.full_agent.read_guards import state_scope
from tavern.scripting.native_response import native_items


def parsed_json(text):
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return None


def object_json(text):
    value = parsed_json(text)
    return value if isinstance(value, dict) else None


def objects(value):
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def context_messages(compiled):
    """Include explicit plaintext tails carried beside a cloud checkpoint.

    The UI represents a checkpoint as one opaque row, but replay maintenance
    can attach later, uncompressed messages and tool calls to that same native
    item. Those originals remain model-visible; encrypted summaries do not.
    """
    from tavern.conversation.compaction import canonical

    originals = {}
    complete = compiled.get("_compact_header", []) + compiled.get("_compact_body", [])
    for wire, row in zip(complete, compiled.get("runtime_trace", [])):
        originals.setdefault(fingerprint(canonical(wire)), []).append(row)
    for row, wire in zip(compiled["trace"], compiled["messages"], strict=True):
        if not row.get("opaque"):
            yield row
            continue
        items = native_items(wire, "responses") or []
        start = max((index + 1 for index, item in enumerate(items) if item.get("type") == "compaction"), default=0)
        for item in items[start:]:
            kind = item.get("type")
            if kind == "function_call":
                yield {"role": "assistant", "tool_calls": [{"id": item["call_id"],
                    "function": {"name": item["name"], "arguments": item["arguments"]}}]}
            elif kind == "function_call_output":
                yield {"role": "tool", "tool_call_id": item["call_id"], "content": item["output"]}
            elif kind == "message" or "role" in item:
                parts = item.get("content", "")
                content = parts if isinstance(parts, str) else "".join(
                    part.get("text", "") for part in parts if part.get("type") in ("input_text", "output_text", "text"))
                message = {"role": item["role"], "content": content}
                # Recover source identity only for an exact original message
                # still present in the actual native plaintext input.
                yield from originals.get(fingerprint(canonical(message)), [message])


def observe_context(reads, messages):
    owner = reads.owner
    data = card_data(owner.document)
    expose = owner.model_view.expose
    refs = lore_references(owner.document, owner.state)
    entries = {entry["id"]: (raw, entry) for _, raw, entry in entry_rows(owner.document)
               if editable_entry(raw, entry)}
    reverse = {identity: ref for ref, identity in refs.items()}
    projected = state_for_model(owner.snapshot.get("state", {}))
    variables = expose(projected.get("stat_data", {}))
    variables_fingerprint = fingerprint(variables)
    calls, known_variables = {}, None

    def character(field, text, *, expanded=False):
        if field not in CHARACTER_FIELDS:
            return
        expected = data.get(field)
        if expanded:
            expected = owner.expand(expected or "")
        if text == expose(expected):
            reads.read_character(field)

    def note(key, text):
        if not isinstance(key, str):
            return
        current = owner.state["notes"].get(key)
        if current is not None and text == expose(current["content"]):
            reads.read_note(key)

    def entry(ref, text, *, expanded=False):
        if not isinstance(ref, str):
            return
        row = entries.get(refs.get(ref))
        if row is None:
            return
        raw, normalized = row
        expected = normalized.get("content", "")
        if expanded:
            expected = owner.expand(expected)
        if text == expose(expected):
            reads.read_entry(ref, raw)

    def state(value):
        if fingerprint(value) == variables_fingerprint:
            reads.read_state()

    for message in messages:
        if message.get("opaque"):
            continue
        source = message.get("source", "")
        text = message.get("runtime_content", message.get("content", ""))
        field = {"examples": "mes_example", "post": "post_history_instructions"}.get(source, source)
        if field in CHARACTER_FIELDS:
            character(field, text, expanded=True)
        elif source.startswith("lore:") and source[5:] in reverse:
            entry(reverse[source[5:]], text, expanded=True)

        for call in message.get("tool_calls", []):
            function = call.get("function", {})
            args = object_json(function.get("arguments"))
            if args is None:
                continue
            name = function.get("name")
            calls[call["id"]] = (name, args)
            # These are complete texts the model itself supplied. They qualify
            # only when equal to the version currently being edited; a proposal
            # for a different version cannot authorize overwriting unseen text.
            if name == "write_note":
                note(args.get("key"), args.get("content"))
            elif name == "revise_content":
                for operation in objects(args.get("operations")):
                    if operation.get("op") == "set_character" and "value" in operation:
                        character(operation.get("field"), operation.get("value"))
                    elif (operation.get("op") == "update_lore" and isinstance(operation.get("changes"), dict)
                          and "content" in operation["changes"]):
                        entry(operation.get("ref"), operation["changes"]["content"])
                    elif operation.get("op") == "add_lore" and isinstance(operation.get("entry"), dict):
                        for ref in refs:
                            entry(ref, operation["entry"].get("content"))
            elif name == "patch_state":
                for operation in objects(args.get("operations")):
                    if operation.get("path") == "" and operation.get("op") in ("add", "replace", "test") and "value" in operation:
                        state(operation.get("value"))

        call = calls.get(message.get("tool_call_id")) if message.get("role") == "tool" else None
        result = object_json(message.get("content")) if call else None
        if not succeeded(result) or not isinstance(result.get("data"), dict):
            continue
        name, args = call
        result = result["data"]
        if name == "read_note":
            note(args.get("key"), result.get("text"))
        elif name == "read_character":
            character(args.get("ref"), result.get("text"))
        elif name == "read_lore_entry":
            entry(args.get("ref"), result.get("content"))
        elif name == "revise_content":
            for row in objects(result.get("characters")):
                character(row.get("ref"), row.get("text"))
            for row in objects(result.get("entries")):
                entry(row.get("ref"), row.get("content"))
        elif name == "recall_lore":
            for match in objects(result.get("matches")):
                # Default recall results deliberately omit navigation handles.
                # A complete matching body still supplies the editable text.
                body = unwrapped(match.get("text", ""), "lorebook")
                for ref in refs:
                    entry(ref, body, expanded=True)
        elif name == "read_state":
            value = parsed_json(result.get("text"))
            path, scope = args.get("path", ""), state_scope(args.get("scope", "mvu"))
            if scope == "full" and path == "" and isinstance(value, dict):
                value = value.get("stat_data", {})
            elif not (scope == "mvu" and path == "" or scope == "full" and path == "/stat_data"):
                continue
            known_variables = value
            state(value)
        elif name == "patch_state" and result.get("saved_to_draft") and known_variables is not None:
            # A complete read followed by a successful visible patch also
            # expresses the new version. Script side effects are not guessed:
            # the reconstructed value must exactly match current variables.
            try:
                known_variables = jsonpatch.apply_patch(known_variables, args.get("operations", []))
            except (jsonpatch.JsonPatchException, jsonpointer.JsonPointerException, KeyError, TypeError, ValueError):
                known_variables = None
            state(known_variables)
