"""Compile complete source context for local windows or official server compaction."""

import copy
import json

from tavern.character.cards import lore_entries, normalize
from tavern.conversation.cache_breakpoint import prepare_context
from tavern.conversation.context import (
    macros,
    wrap_card_field,
    wrap_lorebook,
)
from tavern.conversation.regeneration import regeneration_reference
from tavern.conversation.variable_rules import collect_variable_rules, variable_rules_message
from tavern.core.cache_policy import fingerprint
from tavern.core.message_adapter import adapt_messages, project_context, runtime_messages
from tavern.core.tokens import count_text, counting_info, message_tokens, request_tokens
from tavern.full_agent.config import tool_protocol
from tavern.full_agent.content_history import content_history
from tavern.full_agent.context_ledger import ContextLedger
from tavern.full_agent.plot import plan_message, plan_value
from tavern.full_agent.state import full_state


def load_trace(store, sid, history):
    allowed = {m["id"] for m in history}
    with store.connection() as db:
        rows = db.execute(
            "SELECT * FROM full_agent_trace WHERE session=? ORDER BY message_id,seq", (sid,)
        ).fetchall()
    result = []
    for row in rows:
        if row["message_id"] not in allowed:
            continue
        message = json.loads(row["payload"])
        if "_archive_content" in message:
            message["content"] = message.pop("_archive_content")
        result.append({**dict(row), "message": message})
    return result


def compile_window(
    document,
    session,
    history,
    snapshot,
    user_input,
    settings,
    tools,
    *,
    trace=(),
    continuation=(),
    input_id="pending",
    instruction="",
    force_rollover=False,
    require_state_patch=False,
    variable_rules=None,
    allow_content_edit=False,
    regenerate_reply="",
    replay_all=False,
):
    state = full_state(snapshot)
    from tavern.core.kemini import TOOL_NAME, TURN_SOURCES, prefix_frames, transport_tools, turn_frames

    tools = transport_tools(tools, settings.kemini, "required" if force_rollover else "auto")
    compact = settings.context_strategy == "openai_compact"
    window = state["window"]
    card = normalize(document)
    complete_source = compact or replay_all
    visible = history if complete_source else history[window["start"] :]
    turn_instructions = {
        "message_edit_notice": "",  # Filled after canonical history and input are present.
        "turn_instruction": (
            f"允许修改设定：{allow_content_edit}。" if require_state_patch or allow_content_edit else ""
        ),
        "regenerate_instruction": instruction,
        "regenerate_reference": regeneration_reference(
            macros(
                regenerate_reply, card, session["persona"], snapshot.get("state", {}), snapshot.get("scopes")
            )
        ),
    }

    def expand(value):
        return macros(value, card, session["persona"], snapshot.get("state", {}), snapshot.get("scopes"))

    # This absolute prefix is outside all application ordering and history
    # offsets; no runtime rules or worldbook entries can split its four blocks.
    items = prefix_frames(settings.kemini)
    request_tail = turn_frames(settings.kemini)

    def add(role, content, source, **metadata):
        if content:
            items.append({"role": role, "content": content, "source": source, **metadata})

    add("system", settings.full_agent_system_prompt, "rules")
    add(
        "system",
        tool_protocol(
            settings.full_agent_config, compact=compact, plot_enabled=session.get("plot_enabled", False)
        ),
        "tool_protocol",
    )
    # Keep the window policy in the fixed prefix, once per compiled window;
    # live token counts belong in append-only pressure notices below.
    if not compact:
        add(
            "system",
            f"本次上下文窗口上限为 {settings.context_tokens} tokens；"
            f"占用达到 {settings.full_agent_config.pressure_ratio * 100:g}% 时预警，提示占用百分比和当前估算 tokens / 窗口上限；"
            "达到上限须单独调用 new_context 保存 handoff 并换窗，随后继续本轮。",
            "context_policy",
        )
    if session.get("turn_preference"):
        add("system", settings.preference_prompt + "\n" + session["turn_preference"], "session_preference")
    constants = [
        e
        for e in lore_entries(document, "writer")
        if e["enabled"] and e.get("constant") and "[initvar]" not in e.get("comment", "").lower()
    ]
    for entry in constants:
        role = entry.get("role", "system")
        role = {0: "system", 1: "user", 2: "assistant"}.get(role, role)
        add(
            role,
            wrap_lorebook(expand(entry["content"])),
            "lore:" + entry["id"],
            lore_ids=[entry["id"]],
            lore_region="constant",
        )
    for field in ("system_prompt", "description", "personality", "scenario"):
        add("system", expand(wrap_card_field(field, card.get(field, ""))), field)
    add(
        "system",
        "<user_character>\n用户："
        + session["persona"]
        + "\n"
        + session.get("persona_data", {}).get("description", "")
        + "\n</user_character>",
        "persona",
    )
    add("system", expand(wrap_card_field("mes_example", card.get("mes_example", ""))), "examples")
    if complete_source or window["number"] >= 2:
        # Keep the current directory after fixed settings so key changes preserve
        # their cache prefix. Note bodies and metadata remain tool-only.
        note_keys = "\n".join(
            key for key, note in state["notes"].items() if note.get("status", "active") == "active"
        )
        add("system", "<tavern_note_list>\n" + note_keys + "\n</tavern_note_list>", "note_list")
    handoff = state["handoff"]
    fixed_count = len(items)

    by_owner = {}
    for item in trace:
        if complete_source or window.get("replay_history") or item["window_id"] == window["id"]:
            message = item["message"]
            if window.get("replay_history") and item["window_id"] != window["id"]:
                from tavern.scripting.native_response import portable

                # A local window rebuilt from originals must not resurrect
                # an old cloud checkpoint through a final-response attachment.
                message = portable(message)
            by_owner.setdefault(item["message_id"], []).append(message)

    # Filter a single chronological sequence so native state after a retired
    # exchange cannot resurrect broad source reads through another turn.
    records = [
        {**message, "_content_owner": owner["id"]}
        for owner in visible
        for message in by_owner.get(owner["id"], [])
    ]
    records.extend({**message, "_content_owner": None} for message in continuation)
    if complete_source:
        from tavern.scripting.native_response import portable

        records = [portable(message) for message in records]
    else:
        records, _ = content_history(records)
    by_owner, continuation = {}, []
    for message in records:
        owner = message.pop("_content_owner")
        if owner is None:
            continuation.append(message)
        else:
            by_owner.setdefault(owner, []).append(message)

    records = [m for message in visible for m in by_owner.get(message["id"], [])] + list(continuation)
    history_text = {m["id"]: expand(m.get("model", m["raw"])) for m in visible}
    from tavern.conversation.message_editing import edit_notification

    turn_instructions["message_edit_notice"] = edit_notification(
        snapshot,
        session.get("edit_notification_history", session.get("messages", history)),
        {**history_text, input_id: user_input},
        expand,
    )
    ledger = ContextLedger(
        settings,
        items,
        tools,
        document,
        snapshot,
        records,
        input_id,
        {mid: fingerprint(text) for mid, text in history_text.items()},
    )

    def replay(message):
        source = message.get("source")
        # Older versions persisted Kemini request requirements as append frames.
        # Keep those audit rows, but never replay them into a later request.
        if source in TURN_SOURCES:
            return
        if complete_source and source in (
            "handoff",
            "context_budget",
            "context_policy",
            "context_status",
            "context_warning",
        ):
            return
        # Sent turn requirements stay in place. Their effective value is updated
        # or revoked below; filtering history would break the shared prefix.
        item = ledger.replay(message)
        if item is not None:
            items.append(item)

    def update(source, content, **kwargs):
        frame = ledger.update(source, content, **kwargs)
        if frame is not None:
            items.append(frame)
        return frame

    def anchored(message_id):
        for record in snapshot.get("lore_injections", []):
            if record["anchor_id"] == message_id and not record.get("manually_removed"):
                add(
                    "system",
                    wrap_lorebook(record["content"]),
                    "lore:" + record["id"],
                    lore_ids=[record["id"]],
                    anchor_id=message_id,
                    title=record["title"],
                )

    # Boundaries belong to source exchanges, not SDK roles (lore may be mapped
    # to user). Include anchored lore before its user and all tool continuations.
    replay_starts = [{"offset": 0, "history_start": 0 if complete_source else window["start"]}]
    for history_index, message in enumerate(visible, 0 if complete_source else window["start"]):
        if message["role"] == "user" and len(items) > fixed_count:
            replay_starts.append({"offset": len(items) - fixed_count, "history_start": history_index})
        anchored(message["id"])
        # Native tool calls/results belong before the final narrative that ended that turn.
        records = by_owner.get(message["id"], [])
        for record in records:
            replay(record)
        add(message["role"], history_text[message["id"]], "history", message_id=message["id"])
        final = next((m for m in records if m.get("_full_agent_final")), None)
        if not complete_source and final and items and items[-1].get("message_id") == message["id"]:
            items[-1].update(
                {
                    k: copy.deepcopy(final[k])
                    for k in (
                        "_native_response",
                        "_response_origin",
                        "_response_digest",
                        "reasoning_content",
                        "reasoning_details",
                        "reasoning",
                        "_context_epoch",
                        "_native_epoch",
                    )
                    if k in final
                }
            )
            items[-1] = ledger.native_message(items[-1])
    pending_offset = len(items) - fixed_count if user_input or continuation or input_id != "pending" else None
    if pending_offset is not None and len(items) > fixed_count:
        replay_starts.append({"offset": len(items) - fixed_count, "history_start": len(history)})
    anchored(input_id)
    add("user", user_input, "input", message_id=input_id)
    for message in continuation:
        replay(message)
    update(
        "turn_variable_instruction",
        "本轮须调用 patch_state 更新 MVU 变量一次，无变化提交 []，再回复正文；已调用成功则不要重复。"
        if require_state_patch
        else "",
        value={"required": True, "input_id": input_id} if require_state_patch else False,
    )
    if require_state_patch:
        # Actual turns supply a durable, frozen snapshot. Read-only previews
        # collect the same rules without marking them as already injected.
        rules = (
            collect_variable_rules(
                document, session["persona"], snapshot.get("state", {}), snapshot.get("scopes")
            )
            if variable_rules is None
            else variable_rules
        )
        update("mvu_rules", variable_rules_message(rules), value=rules)
    update(
        "post",
        expand(wrap_card_field("post_history_instructions", card.get("post_history_instructions", ""))),
    )
    for source, content in turn_instructions.items():
        update(source, content)
    planning = plan_value(state, session.get("plot_enabled", False), compact=compact)
    # Initial empty defaults need no frame; retained content and observed
    # versions still need explicit updates when cleared or disabled.
    if planning["enabled"] or state["plot"]["text"] or "plot" in ledger.seen:
        update("plot", plan_message(planning), value=planning)
    # Staleness remains truthful in the user's notes and source checks. Proactive
    # edit instructions now come only from the user's explicit notification queue.
    if not complete_source:
        update("context_warning", "")
        if handoff["text"] or "handoff" in ledger.seen:
            update(
                "handoff",
                "<tavern_memory>\n当前有效 handoff（取代此前版本；后续以最新记忆更新或成功写入的工具记录为准）：\n"
                + (handoff["text"] or "当前 handoff 为空；此前版本不再作为当前记忆。")
                + "\n</tavern_memory>",
                value=handoff["text"],
            )
    available = settings.context_tokens
    allowed_fields = {
        "role",
        "content",
        "tool_calls",
        "tool_call_id",
        "name",
        "reasoning_content",
        "reasoning_details",
        "reasoning",
        "_native_response",
        "_response_origin",
        "_response_digest",
        "_tool_error",
    }

    def wire():
        # Role remapping must not let anchored lore join/reorder the fixed
        # prefix: replay offsets are defined by the original exchange order.
        for item in [*items[fixed_count:], *request_tail]:
            item["runtime_phase"] = "body"
        typed = runtime_messages([*items, *request_tail], getattr(settings, "_adapter_lane", "full_agent"))
        adapted, _, _ = adapt_messages(typed, settings, managed=True)
        return [{k: v for k, v in m.items() if k in allowed_fields} for m in adapted]

    reasoning_cost = sum(
        count_text(
            json.dumps(
                {k: m[k] for k in ("reasoning_content", "reasoning_details", "reasoning") if k in m},
                ensure_ascii=False,
            ),
            settings,
        )
        for m in items
        if not m.get("_native_response")
        and any(k in m for k in ("reasoning_content", "reasoning_details", "reasoning"))
    )
    active_notes = sum(note.get("status", "active") == "active" for note in state["notes"].values())
    archived_notes = len(state["notes"]) - active_notes
    memory_status = f"长期笔记：有效 {active_notes} 条，已归档 {archived_notes} 条。"
    history_status = f"原始历史 H1–H{len(history)}" if history else "尚无原始历史"
    visible_status = (
        f"当前保留 H{window['start'] + 1} 起的正文" if visible else "当前窗口没有已完成的历史正文"
    )
    status = (
        f"上下文窗口：当前第 {window['number']} 个。\n"
        f"{history_status}；当前用户输入引用 H{len(history) + 1}；{visible_status}。\n"
        f"{memory_status}"
    )
    if not complete_source:
        update("context_status", status)
    tokens = request_tokens(wire(), settings, tools) + reasoning_cost
    rollover_required = not complete_source and (force_rollover or tokens >= settings.context_tokens)
    if not complete_source and (
        rollover_required or tokens >= settings.context_tokens * settings.full_agent_config.pressure_ratio
    ):
        budget_item = None
        # Compare actionable pressure states, not the changing token estimate.
        # Include a new notice itself when checking whether it forces rollover.
        for _ in range(6):
            rollover_required |= tokens >= settings.context_tokens
            value = {
                "level": "rollover" if rollover_required else "pressure",
                "context_tokens": settings.context_tokens,
                "reply_tokens": settings.reply_tokens,
            }
            content = (
                f"本次提醒时上下文估算 {tokens} / {settings.context_tokens} tokens"
                f"（{tokens / settings.context_tokens:.1%}，含工具定义）；"
                f"本轮回复额度 {settings.reply_tokens} tokens。\n"
                + (
                    "当前进入强制换窗状态：当前上下文已达到或超过窗口上限。"
                    "必须单独调用 new_context 保存完整交接笔记并换窗；运行时否决其他一切工具调用和正文回复。"
                    "换窗成功后继续当前用户请求。"
                    if rollover_required
                    else "窗口预算提醒，供安排交接与换窗时机；当前仍可正常调用工具和回复。"
                )
            )
            if budget_item is None:
                budget_item = update("context_budget", content, value=value)
                if budget_item is None:
                    break
            else:
                budget_item["content"] = content
                budget_item["_runtime_context"]["value"] = value
            updated = request_tokens(wire(), settings, tools) + reasoning_cost
            if updated == tokens:
                break
            tokens = updated
    elif not complete_source:
        update("context_budget", "")
    if rollover_required:
        # The current compilation itself can discover overflow. Match the actual
        # forced-tool request even before the next compilation sees force_rollover.
        tools = [tool for tool in tools if tool.get("function", tool)["name"] != TOOL_NAME]
    messages = wire()
    tokens = request_tokens(messages, settings, tools) + reasoning_cost
    rows = [*items, *request_tail]
    result = {
        "messages": messages,
        "trace": [
            {**m, "position": i, "estimated_tokens": message_tokens(m, settings)} for i, m in enumerate(rows)
        ],
        "estimated_tokens": tokens,
        "tokens": tokens,
        "available_tokens": available,
        "rollover_required": rollover_required,
        "context_limit": settings.context_tokens,
        "budget": available,
        "token_counting": counting_info(settings),
        "diagnostics": [],
        "tools": tools,
        "agent_mode": "full_agent",
        "window": {k: v for k, v in window.items() if k != "id"},
        "daily_context_budget": available,
        "window_message_count": len(visible) + bool(user_input),
        "budget_sources": {
            **{
                source: sum(message_tokens(m, settings) for m in rows if m["source"] == source)
                for source in dict.fromkeys(m["source"] for m in rows)
            },
            "tool_definitions": request_tokens([], settings, tools) - 3,
        },
        "next_lore_injections": copy.deepcopy(snapshot.get("lore_injections", [])),
    }
    result["context_ledger"] = ledger.audit()
    result["context_additions"] = copy.deepcopy(ledger.additions)
    project_context(result, settings, tools=tools, reasoning_cost=reasoning_cost, managed=True)
    if complete_source:
        from tavern.conversation.compaction import canonical
        from tavern.scripting.native_response import portable

        complete = [canonical(portable(message)) for message in result["messages"]]
        result.update(
            _replay_header=complete[:fixed_count],
            _replay_body=complete[fixed_count:],
            _replay_starts=replay_starts,
            _replay_complete=pending_offset if pending_offset is not None else len(complete) - fixed_count,
        )
    if compact:
        from tavern.conversation.compaction import project_compact

        return project_compact(result, state, settings, tools, fixed_count)
    return (
        result
        if replay_all
        else prepare_context(result, {**session, "agent_mode": "full_agent"}, history, snapshot, settings)
    )
