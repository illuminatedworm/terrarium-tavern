"""Compile complete source context for local windows or official server compaction."""

import copy
import json

from tavern.character.cards import is_initvar, lore_entries, normalize
from tavern.conversation.cache_breakpoint import prepare_context
from tavern.conversation.context import (
    lore_text,
    macros,
    wrap_card_field,
    wrap_lorebook,
)
from tavern.conversation.regeneration import regeneration_reference
from tavern.conversation.variable_rules import collect_variable_rules, variable_rules_message
from tavern.core.cache_policy import fingerprint
from tavern.core.context_usage import context_tokens
from tavern.core.history_refs import labeled, message_ref, span_ref
from tavern.core.message_adapter import adapt_messages, project_context, runtime_messages
from tavern.full_agent.config import tool_protocol
from tavern.full_agent.content_history import content_history
from tavern.full_agent.context_ledger import ContextLedger
from tavern.full_agent.handoff import render_chapters, render_current
from tavern.full_agent.plot import plan_message, plan_value
from tavern.full_agent.state import full_state
from tavern.full_agent.story import committed_story, story_history


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
    story=None,
    replay_all=False,
):
    state = full_state(snapshot)
    from tavern.core.kemini import TOOL_NAME, TURN_SOURCES, prefix_frames, transport_tools, turn_frames

    tools = transport_tools(tools, settings.kemini, "required" if force_rollover else "auto")
    compact = settings.context_strategy == "openai_compact"
    window = state["window"]
    compaction = state.get("compaction") or {}
    # Leave the encrypted items out: a usable checkpoint is itself a request
    # block, so describe() already fingerprints them once per compilation.
    settings._context_generation = fingerprint(
        [window["id"], {key: value for key, value in compaction.items() if key != "items"}]
    )
    card = normalize(document)
    complete_source = compact or replay_all
    visible = history if complete_source else history[window["start"] :]
    turn_instructions = {
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

    def lore_block(entry, content):
        return wrap_lorebook(lore_text(entry, content, settings.lore_injection_title, expand))

    # Anchored records keep frozen content; an opted-in title comes from the current entry.
    writer_lore = {e["id"]: e for e in lore_entries(document, "writer")}

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
            f"本次上下文窗口上限为 {settings.context_tokens} tokens，水位依据上次接口实测输入，不需要你自己计算；新增内容尚未计量。"
            f"占用达到 {settings.full_agent_config.pressure_ratio * 100:g}% 时会追加窗口预算提醒，"
            "显示上次实测输入与对应比例；实测达到上限时提醒会改为强制换窗。没有提醒就不必换窗。",
            "context_policy",
        )
    if session.get("turn_preference"):
        add("system", settings.preference_prompt + "\n" + session["turn_preference"], "session_preference")
    constants = [e for e in writer_lore.values() if e["enabled"] and e.get("constant") and not is_initvar(e)]
    for entry in constants:
        role = entry.get("role", "system")
        role = {0: "system", 1: "user", 2: "assistant"}.get(role, role)
        add(
            role,
            lore_block(entry, expand(entry["content"])),
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
    records, _ = story_history(records)
    if story is not None:
        records = [
            committed_story(message, story["content"])
            if message.get("_story_received", {}).get("input_id") == input_id
            else message
            for message in records
        ]
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
            "handoff_chapters",
            "context_budget",
            "context_policy",
            "context_status",
        ):
            return
        # Sent turn requirements stay in place. Their effective value is updated
        # or revoked below; filtering history would break the shared prefix.
        item = ledger.replay(message)
        if item is not None:
            items.append(item)
        return item

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
                    lore_block(writer_lore.get(record["id"], {}), record["content"]),
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
        # Keep every exchange in its original order, including tools after the
        # story. Its accepted response carries the final saved text exactly once.
        records = by_owner.get(message["id"], [])
        story_in_trace = False
        for record in records:
            if record.get("source") == "roleplay_draft" and record["_runtime_context"]["value"]:
                # A delivered-draft frame is current-turn guidance, resent only
                # after a rollover or Kemini cleanup. Once committed it holds a
                # stale instruction and a pre-callback copy; the native response
                # or the reply message carries the committed story instead.
                # Only the cached prefix after that point changes.
                continue
            if record.get("_story_received"):
                record = committed_story(record, history_text[message["id"]])
            if replay(record) is not None and record.get("_story_received"):
                story_in_trace = True
        if not story_in_trace:
            add(
                message["role"],
                # User labels help navigation; assistant prose stays unlabelled.
                labeled(history_index + 1, history_text[message["id"]])
                if message["role"] == "user"
                else history_text[message["id"]],
                "history",
                message_id=message["id"],
            )
        else:
            # A text boundary can carry every provider's cache marker without
            # inventing a field on function_call arguments or repeating prose.
            # Name the finished reply instead of "this turn": the marker sits right
            # before the next input and must not read as an order for that turn.
            add(
                "system",
                f"以上回合已完成，正文为 {message_ref(history_index + 1)}。",
                "turn_commit",
                message_id=message["id"],
            )
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
    # Maintenance also compiles with no pending input. Its fresh directory and
    # other dynamic frames still belong after history, not inside the saved
    # coverage: the next user input will precede those frames.
    completed_offset = len(items) - fixed_count
    if (user_input or continuation or input_id != "pending") and completed_offset:
        replay_starts.append({"offset": completed_offset, "history_start": len(history)})
    anchored(input_id)
    add(
        "user",
        user_input and labeled(len(history) + 1, user_input),
        "input",
        message_id=input_id,
    )
    for message in continuation:
        replay(message)
    story_in_context = any(
        message.get("_story_received", {}).get("input_id") == input_id for message in continuation
    )
    update(
        "roleplay_draft",
        # Tool continuations and resumed turns read this frame; a second story is
        # rejected, so it says how to finish instead of only stating progress.
        f"本轮正文 {message_ref(len(history) + 2)} 已接收，不要再输出正文；"
        "如有未完成的工具调用就继续，否则直接结束本次回复。"
        + ("" if story_in_context else "\n<roleplay_draft>\n" + story["content"] + "\n</roleplay_draft>")
        if story is not None
        else "",
        value={
            "input_id": input_id,
            "content_hash": fingerprint(story["content"]),
            "in_response": story_in_context,
        }
        if story is not None
        else False,
    )
    update(
        "turn_variable_instruction",
        "本轮须至少成功调用一次 patch_state 更新 MVU 变量，无变化提交 []。" if require_state_patch else "",
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
    if complete_source or window["number"] >= 2:
        # The directory is live state, not a fixed rule: adding or archiving a
        # note must preserve the previous request prefix and cloud checkpoint.
        # Reassert it once after compaction, just like the current outline.
        note_keys = "\n".join(
            key for key, note in state["notes"].items() if note.get("status", "active") == "active"
        )
        update(
            "note_list",
            "<tavern_note_list>\n当前有效笔记目录（取代此前目录；正文仍须按需读取）：\n"
            + (note_keys or "当前没有有效笔记。")
            + "\n</tavern_note_list>",
            value={"keys": note_keys, "checkpoint": planning["checkpoint"]},
        )
    if not complete_source:
        # Chapters and the current handoff are separate frames: a mid-window
        # current update does not resend the whole chapter ledger.
        chapters = render_chapters(handoff, history)
        if chapters or "handoff_chapters" in ledger.seen:
            update(
                "handoff_chapters",
                "<tavern_memory>\nhandoff 章节（按时间首尾相接；[H…] 为原文位置，可用 read_history 回读；"
                "C 编号供 write_handoff/new_context 修订或合并）：\n"
                + (chapters or "当前没有章节；此前版本不再有效。")
                + "\n</tavern_memory>",
                value=chapters,
            )
        current = render_current(handoff, history, current_input_id=input_id)
        if current or "handoff" in ledger.seen:
            update(
                "handoff",
                "<tavern_memory>\n当前有效 handoff（取代此前版本；后续以最新记忆更新或成功写入的工具记录为准）：\n"
                + (current or "当前 handoff 为空；此前版本不再作为当前记忆。")
                + "\n</tavern_memory>",
                value=current,
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

    active_notes = sum(note.get("status", "active") == "active" for note in state["notes"].values())
    archived_notes = len(state["notes"]) - active_notes
    memory_status = f"长期笔记：有效 {active_notes} 条，已归档 {archived_notes} 条。"
    history_status = f"原始历史 {span_ref(1, len(history))}" if history else "尚无原始历史"
    visible_status = (
        f"当前保留 {message_ref(window['start'] + 1)} 起的正文" if visible else "当前窗口没有已完成的历史正文"
    )
    status = (
        f"上下文窗口：当前第 {window['number']} 个。\n"
        f"{history_status}；当前用户输入引用 {message_ref(len(history) + 1)}；{visible_status}。\n"
        f"{memory_status}"
    )
    if not complete_source:
        update("context_status", status)
    tokens = context_tokens(wire(), settings, tools)
    rollover_required = not complete_source and (
        force_rollover or tokens is not None and tokens >= settings.context_tokens
    )
    if not complete_source and (
        rollover_required
        or tokens is not None
        and tokens >= settings.context_tokens * settings.full_agent_config.pressure_ratio
    ):
        # A notice cannot change an already measured input. Keep its original
        # observation text stable until the actionable pressure state changes.
        value = {
            "level": "rollover" if rollover_required else "pressure",
            "context_tokens": settings.context_tokens,
            "reply_tokens": settings.reply_tokens,
        }
        measured = (
            f"本次提醒依据上次实测输入 {tokens} / {settings.context_tokens} tokens（{tokens / settings.context_tokens:.1%}）"
            if tokens is not None
            else "此前已要求换窗；当前请求没有可复用的实测输入"
        )
        content = (
            measured
            + f"；新增内容尚未计量，本轮回复额度 {settings.reply_tokens} tokens。\n"
            + (
                "当前进入强制换窗状态。必须单独调用 new_context 换窗；可省略 handoff 沿用现有交接，"
                "仅实际退出的历史须有章节覆盖，保留的最新完整交流不要求归档。"
                "运行时否决其他一切工具调用和正文回复。换窗成功后继续当前用户请求。"
                if rollover_required
                else "窗口水位提醒，供安排交接与换窗时机；当前仍可正常调用工具和回复。"
            )
        )
        update("context_budget", content, value=value)
    elif not complete_source:
        update("context_budget", "")
    if rollover_required:
        # The current compilation itself can discover overflow. Match the actual
        # forced-tool request even before the next compilation sees force_rollover.
        tools = [tool for tool in tools if tool.get("function", tool)["name"] != TOOL_NAME]
    messages = wire()
    rows = [*items, *request_tail]
    result = {
        "messages": messages,
        "trace": [{**m, "position": i} for i, m in enumerate(rows)],
        "available_tokens": available,
        "rollover_required": rollover_required,
        "context_limit": settings.context_tokens,
        "budget": available,
        "diagnostics": [],
        "tools": tools,
        "agent_mode": "full_agent",
        "window": {k: v for k, v in window.items() if k != "id"},
        "daily_context_budget": available,
        "window_message_count": len(visible) + bool(user_input),
        "next_lore_injections": copy.deepcopy(snapshot.get("lore_injections", [])),
    }
    result["context_ledger"] = ledger.audit()
    result["context_additions"] = copy.deepcopy(ledger.additions)
    project_context(result, settings, tools=tools, managed=True)
    if complete_source:
        from tavern.conversation.compaction import canonical
        from tavern.scripting.native_response import portable

        complete = [canonical(portable(message)) for message in result["messages"]]
        result.update(
            _replay_header=complete[:fixed_count],
            _replay_body=complete[fixed_count:],
            _replay_starts=replay_starts,
            _replay_complete=completed_offset,
        )
    if compact:
        from tavern.conversation.compaction import project_compact

        return project_compact(result, state, settings, tools, fixed_count)
    return (
        result
        if replay_all
        else prepare_context(result, {**session, "agent_mode": "full_agent"}, history, snapshot, settings)
    )
