"""Half Agent's durable request frames; the existing compiler owns admission/positions."""

import copy
import json

from tavern.conversation.half_compaction import enabled, local_window_enabled
from tavern.core.cache_policy import fingerprint
from tavern.core.kemini import TURN_SOURCES as KEMINI_TRANSIENT
from tavern.core.kemini import control_text, prefix_frames
from tavern.core.message_adapter import project_context, type_definition, unwrapped
from tavern.core.tokens import count_text, counting_info, message_tokens, request_tokens
from tavern.scripting.native_response import message_digest, native_items
from tavern.scripting.request_context import response_origin
from tavern.search.retrieval_text import split_retrieval

# v4 adds per-exchange frozen lore; v3 append frames remain compatible.
VERSION = 4
DYNAMIC = {
    "author_note",
    "post",
    "memory",
    "state",
    "turn_instruction",
    "message_edit_notice",
    "regenerate_reference",
    *KEMINI_TRANSIENT,
}
TRANSIENT = {"message_edit_notice", "regenerate_reference", *KEMINI_TRANSIENT}
LABELS = {
    "author_note": "作者注",
    "post": "角色后置要求",
    "memory": "记忆",
    "state": "剧情状态",
    "turn_instruction": "本轮额外要求",
    "message_edit_notice": "原文编辑提醒",
    "regenerate_reference": "待重写回复参考",
    "kemini_control": "Kemini 输出要求",
    "kemini_roleplay": "Kemini 角色扮演追加要求",
    "kemini_acknowledgement": "Kemini 助手确认",
}
FRAME_TAGS = {"author_note": "author_note", "post": "card_post_history_instructions", "state": "tavern_state"}
REASONS = {
    "initial_context": "首次建立上下文基线",
    "legacy_context": "为旧剧情建立上下文基线",
    "context_version_changed": "上下文记录格式变化",
    "provider_or_model_changed": "模型或提供商身份变化",
    "context_window_changed": "可见历史窗口或分支变化",
    "context_sources_changed": "请求策略变化",
    "context_prefix_changed": "已有内容或注入位置变化",
}
WIRE_FIELDS = {
    "role",
    "content",
    "tool_calls",
    "tool_call_id",
    "name",
    "_native_response",
    "reasoning",
    "reasoning_details",
    "reasoning_content",
    "_tool_error",
}


def frame_content(source, notice, content=None):
    tag = FRAME_TAGS.get(source)
    if tag is None:
        return notice if content is None else notice + "\n" + content
    opening, closing = f"<{tag}>\n", f"\n</{tag}>"
    if content and content.startswith(opening) and content.endswith(closing):
        content = content[len(opening) : -len(closing)]
    return opening + notice + ("\n" + content if content else "") + closing


def load_trace(store, sid, history):
    allowed = {m["id"] for m in history}
    with store.connection() as db:
        rows = db.execute(
            "SELECT message_id,payload FROM half_agent_trace WHERE session=? ORDER BY seq", (sid,)
        ).fetchall()
    return {row["message_id"]: json.loads(row["payload"]) for row in rows if row["message_id"] in allowed}


def descriptor(message):
    mid = message.get("message_id")
    return {
        "message_id": mid,
        "anchor_id": message.get("anchor_id"),
        "sha256": fingerprint(
            [
                message["role"],
                message.get("content", ""),
                "dialogue" if mid else message.get("source"),
                message.get("insertion_depth"),
            ]
        ),
    }


def native_text(native):
    if native.get("origin", {}).get("api") == "anthropic":
        return "".join(p.get("text", "") for p in native["items"] if p.get("type") == "text")
    return "".join(
        p.get("text", "")
        for item in native["items"]
        if item.get("type") == "message"
        for p in item.get("content", [])
    )


def finalize_record(record, native, clean, output):
    """Called only after MVU/model transforms, before the atomic story commit."""
    result = copy.deepcopy(record)
    result["base"].append(
        descriptor({"role": "assistant", "content": output["model"], "message_id": output["id"]})
    )
    if native:
        result["response"] = {
            "native": copy.deepcopy(native),
            "model_sha256": fingerprint(clean),
            "eligible": clean == output["raw"] == output["model"],
            "native_epoch": result["native_epoch"],
        }
    return result


def remap_record(record, ids):
    result = copy.deepcopy(record)
    for item in result.get("base", []):
        for key in ("message_id", "anchor_id"):
            item[key] = ids.get(item.get(key), item.get(key))
    result["input_id"] = ids.get(result.get("input_id"), result.get("input_id"))
    result["retired_native"] = [ids.get(mid, mid) for mid in result.get("retired_native", [])]
    for frame in result.get("frames", []):
        frame["anchor_id"] = ids.get(frame.get("anchor_id"), frame.get("anchor_id"))
    for row in result.get("lore", []):
        row["anchor_id"] = ids.get(row["anchor_id"], row["anchor_id"])
        row["source_ids"] = [ids.get(mid, mid) for mid in row.get("source_ids", [])]
    # The new story starts a new baseline; opaque protocol IDs remain untouched.
    result["window"] = "forked"
    return result


def apply_context(
    context,
    records,
    session,
    history,
    snapshot,
    settings,
    *,
    input_id="",
    purpose="narrative",
    previous_record=None,
    continuation=(),
    sent=(),
):
    """Reconcile a fresh canonical compilation with the last committed request.

    Only ordinary trailing runtime sources are eligible for chronological updates.
    Comparing the entire remaining sequence catches moved depth/position blocks,
    worldbook removal, macro edits and arbitrary compatibility overrides alike.
    """
    rows = copy.deepcopy(context.get("runtime_trace", context["trace"]))
    sources = {b["id"]: b["source"] for b in session.get("recipe", {}).get("blocks", [])}
    for row in rows:
        row.setdefault("context_source", sources.get(row["source"], row["source"]))
    input_id = input_id or next(
        (m.get("message_id") for m in reversed(rows) if m.get("context_source", m.get("source")) == "input"),
        "pending",
    )
    boundary = len(rows)
    while boundary and not context.get("position_sensitive"):
        row = rows[boundary - 1]
        source = row.get("context_source", row.get("source"))
        if source not in DYNAMIC or row.get("insertion_depth", 0) > 0:
            break
        boundary -= 1
    base, dynamic = rows[:boundary], rows[boundary:]
    visible_ids = {m.get("message_id") for m in base if m.get("source") == "history"}
    active = [(m["id"], records[m["id"]]) for m in history if m["id"] in records and m["id"] in visible_ids]
    previous = previous_record or (active[-1][1] if active else None)
    generation = (
        snapshot.get("half_context", {}).get("compaction", {}).get("generation", "")
        if enabled(settings)
        else ""
    )
    reassert = bool(generation and generation != (previous or {}).get("compaction_generation"))
    origin = response_origin(settings)
    window = fingerprint(
        [
            purpose,
            next((m["id"] for m in history if m["id"] in visible_ids), None),
            snapshot.get("context_window", {}) if local_window_enabled(settings) else {},
        ]
    )
    layout = [
        (b["id"], b["source"], b.get("depth"), b.get("message_type", ""))
        for b in session.get("recipe", {}).get("blocks", [])
    ]
    # Disabled Kemini adds nothing, so pre-feature policies, epochs and cloud
    # checkpoints stay valid without a compatibility branch.
    kemini = (
        [prefix_frames(settings.kemini), control_text(settings.kemini)] if settings.kemini.enabled else []
    )
    policy = fingerprint([settings.reasoning_effort, settings.reasoning_format, purpose, layout, *kemini])
    current_base = [descriptor(m) for m in base]
    reason = None
    if previous is None:
        reason = "legacy_context" if history else "initial_context"
    elif previous.get("version") not in (3, VERSION):
        reason = "context_version_changed"
    elif previous.get("origin") != origin:
        reason = "provider_or_model_changed"
    elif previous.get("window") != window:
        reason = "context_window_changed"
    elif previous.get("policy") != policy:
        reason = "context_sources_changed"
    elif current_base[: len(previous["base"])] != previous["base"]:
        reason = "context_prefix_changed"
    epoch = fingerprint([origin, policy, window, current_base, input_id]) if reason else previous["epoch"]
    native_epoch = previous["native_epoch"] if previous and previous.get("origin") == origin else epoch
    retired = set(previous.get("retired_native", [])) if previous else set()
    updates, invalidations, seen = [], [], {}
    replay = {}
    if not reason:
        for mid, record in active:
            if record["epoch"] == epoch:
                replay[mid] = [copy.deepcopy(f) for f in record["frames"] if f["source"] not in TRANSIENT]
                for frame in replay[mid]:
                    seen[frame["_half_frame"]["key"]] = frame["_half_frame"]
        if previous_record:
            for frame in sent:
                if "_half_frame" in frame:
                    seen[frame["_half_frame"]["key"]] = frame["_half_frame"]

    def restore_native(row):
        mid = row.get("message_id")
        saved = records.get(mid, {}).get("response")
        if not saved:
            return
        native = saved["native"]
        failure = None
        if mid in retired or saved.get("native_epoch") != native_epoch or native.get("origin") != origin:
            failure = "provider_or_model_changed"
        elif not saved["eligible"] or fingerprint(row["content"]) != saved["model_sha256"]:
            failure = "message_changed"
        else:
            original = native_text(native)
            clean, _, _ = split_retrieval(original, settings.retrieval.query_chars)
            if (
                fingerprint(clean) != saved["model_sha256"]
                or native.get("version") != 1
                or native.get("message_sha256") != message_digest({"content": original})
            ):
                failure = "native_response_changed"
        if failure:
            retired.add(mid)
            invalidations.append({"message_id": mid, "reason": failure})
        else:
            # Canonical/story text stays clean. Only the transport projection is raw.
            row["_native_response"] = copy.deepcopy(native)
            row["native_replay"] = True

    items = []
    for row in base:
        items.extend(replay.pop(row.get("message_id"), []))
        if (
            purpose == "narrative"
            and not enabled(settings)
            and row.get("source") == "history"
            and row["role"] == "assistant"
        ):
            restore_native(row)
        items.append(row)
    current = {}
    occurrences = {}
    for row in dynamic:
        source = row.get("context_source", row["source"])
        stem = (row["source"], row["role"])
        occurrences[stem] = occurrences.get(stem, 0) + 1
        key = fingerprint([*stem, occurrences[stem]])
        label = f"{LABELS[source]}（{row['source']}/{row['role']}/{occurrences[stem]}）"
        value = [row["content"], input_id if source == "turn_instruction" or source in TRANSIENT else None]
        current[key] = {
            "key": key,
            "source": source,
            "value": value,
            "role": row["role"],
            "label": label,
            "message_type": row.get("message_type"),
        }
        if key in seen and seen[key]["value"] == value and not reassert:
            continue
        notice = (
            f"[当前{label}；取代此前同来源版本。"
            + ("仅适用于本轮。" if source == "turn_instruction" or source in TRANSIENT else "")
            + "]"
        )
        frame = {
            **row,
            "anchor_id": input_id,
            "_half_frame": current[key],
            "epoch": epoch,
            "content": row["content"]
            if source in KEMINI_TRANSIENT
            else frame_content(source, notice, row["content"]),
        }
        if row.get("message_type"):
            tag = type_definition(row["message_type"]).tag
            raw = row.get("runtime_content", unwrapped(row["content"], tag))
            frame["runtime_content"] = raw if source in KEMINI_TRANSIENT else notice + "\n" + raw
            frame["runtime_phase"] = "body"
        updates.append(frame)
    for key, previous_value in seen.items():
        if key not in current and (previous_value["value"] is not None or reassert):
            source = previous_value["source"]
            updates.append(
                {
                    "role": previous_value["role"],
                    "source": source,
                    "anchor_id": input_id,
                    "content": frame_content(
                        source, f"[此前{previous_value['label']}已撤销；旧版本不再作为当前要求或状态。]"
                    ),
                    "epoch": epoch,
                    "_half_frame": {**previous_value, "value": None},
                }
            )
            if previous_value.get("message_type"):
                updates[-1]["message_type"] = previous_value["message_type"]
                updates[-1]["runtime_phase"] = "body"
    if previous_record and not reason:
        items = copy.deepcopy(list(sent))
    old_count = previous.get("continuation_count", 0) if previous_record and not reason else 0
    items.extend({**copy.deepcopy(m), "source": "maintenance_tools"} for m in continuation[old_count:])
    items.extend(updates)
    messages = []
    for item in items:
        wire = {k: copy.deepcopy(v) for k, v in item.items() if k in WIRE_FIELDS}
        if wire.get("_native_response"):
            wire["content"] = native_text(wire["_native_response"])
        messages.append(wire)
    total = request_tokens(messages, settings)
    for index, (item, message) in enumerate(zip(items, messages, strict=True)):
        item.update(
            position=index,
            estimated_tokens=message_tokens(message, settings),
            content_tokens=count_text(item["content"], settings),
            unit="tokens",
        )
    context.update(
        messages=messages,
        trace=items,
        estimated_tokens=total,
        token_counting=counting_info(settings),
        budget_sources={
            source: sum(m["estimated_tokens"] for m in items if m["source"] == source)
            for source in dict.fromkeys(m["source"] for m in items)
        },
    )
    context["budget_sources"]["chat_framing"] = total - sum(m["estimated_tokens"] for m in items)
    if reason:
        context.setdefault("diagnostics", []).append({"reason": "上下文重建：" + REASONS[reason]})
    for invalid in invalidations:
        context.setdefault("diagnostics", []).append(
            {"message_id": invalid["message_id"], "reason": "原生回复已改用有效正文：" + invalid["reason"]}
        )
    replay_count = sum(bool(m.get("native_replay")) for m in items)
    if replay_count:
        context.setdefault("diagnostics", []).append(
            {"reason": f"原生回复回放：{replay_count} 条；剧情预览显示清理后的正文"}
        )
    context["context_ledger"] = {
        "version": VERSION,
        "epoch": epoch,
        "rebuild_reason": reason,
        "native_replay_invalidations": invalidations,
        "updates": [{"source": f["source"], "cleared": f["_half_frame"]["value"] is None} for f in updates],
    }
    context["half_agent_record"] = {
        "version": VERSION,
        "origin": origin,
        "window": window,
        "policy": policy,
        "epoch": epoch,
        "native_epoch": native_epoch,
        "base": current_base,
        "input_id": input_id,
        "compaction_generation": generation,
        "frames": updates,
        "retired_native": sorted(retired & visible_ids),
        "continuation_count": len(continuation),
    }
    # Save only newly introduced lore. Older frozen originals belong to their
    # own exchange and remain recoverable after active snapshots retire them.
    previous_keys = {(row["anchor_id"], row["id"]) for row in snapshot.get("lore_injections", [])}
    context["half_agent_record"]["lore"] = [
        copy.deepcopy(row)
        for row in context.get("next_lore_injections", [])
        if (row["anchor_id"], row["id"]) not in previous_keys
    ]
    context["_half_record_ids"] = list(records)
    context["runtime_trace"] = items
    return project_context(context, settings)


def attach_continuation(messages, responses):
    """Preserve native metadata even if a framework drops unknown extra fields."""
    result = copy.deepcopy(messages)
    for row in result:
        if row.get("role") != "assistant":
            continue
        native = responses.get(message_digest(row))
        if native and native_items({**row, "_native_response": native}, native["origin"]["api"]) is not None:
            row["_native_response"] = copy.deepcopy(native)
    return result
