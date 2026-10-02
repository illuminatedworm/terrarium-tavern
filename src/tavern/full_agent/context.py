"""A transparent working window: no legacy recipe, compactor, or memory engine."""
import copy
import json

from tavern.character.cards import lore_entries, normalize
from tavern.conversation.cache_breakpoint import prepare_context
from tavern.conversation.context import (
    character_content,
    macros,
    wrap_card_field,
    wrap_lorebook,
)
from tavern.conversation.variable_rules import collect_variable_rules, variable_rules_message
from tavern.core.cache_policy import fingerprint
from tavern.core.tokens import count_text, counting_info, message_tokens, request_tokens
from tavern.full_agent.context_ledger import ContextLedger
from tavern.full_agent.state import full_state


def load_trace(store, sid, history):
    allowed = {m["id"] for m in history}
    with store.connection() as db:
        rows = db.execute("SELECT * FROM full_agent_trace WHERE session=? ORDER BY message_id,seq", (sid,)).fetchall()
    result = []
    for row in rows:
        if row["message_id"] not in allowed:
            continue
        message = json.loads(row["payload"])
        if "_archive_content" in message:
            message["content"] = message.pop("_archive_content")
        result.append({**dict(row), "message": message})
    return result


def compile_window(document, session, history, snapshot, user_input, settings, tools, *,
                   trace=(), continuation=(), input_id="pending", instruction="", calls=0, force_rollover=False,
                   require_state_patch=False, variable_rules=None):
    state = full_state(snapshot)
    window = state["window"]
    card = normalize(document)
    visible = history[window["start"]:]
    def expand(value):
        return macros(value, card, session["persona"], snapshot.get("state", {}))
    items = []

    def add(role, content, source, **metadata):
        if content:
            items.append({"role": role, "content": content, "source": source, **metadata})

    add("system", settings.full_agent_system_prompt, "rules")
    if session.get("turn_preference"):
        add("system", settings.preference_prompt + "\n" + session["turn_preference"], "session_preference")
    constants = [e for e in lore_entries(document, "writer") if e["enabled"] and e.get("constant")
                 and "[initvar]" not in e.get("comment", "").lower()]
    for entry in constants:
        role = entry.get("role", "system")
        role = {0: "system", 1: "user", 2: "assistant"}.get(role, role)
        add(role, wrap_lorebook(expand(entry["content"])), "lore:" + entry["id"],
            lore_ids=[entry["id"]], lore_region="constant")
    add("system", expand(character_content(card)), "character")
    add("system", "<user_character>\n用户：" + session["persona"] + "\n"
        + session.get("persona_data", {}).get("description", "") + "\n</user_character>", "persona")
    add("system", expand(wrap_card_field("mes_example", card.get("mes_example", ""))), "examples")
    checkpoint = state["checkpoint"]

    by_owner = {}
    for item in trace:
        if item["window_id"] == window["id"]:
            by_owner.setdefault(item["message_id"], []).append(item["message"])

    records = [m for message in visible for m in by_owner.get(message["id"], [])] + list(continuation)
    history_text = {m["id"]: expand(m.get("model", m["raw"])) for m in visible}
    ledger = ContextLedger(settings, items, tools, document, snapshot, records, input_id,
                           {mid: fingerprint(text) for mid, text in history_text.items()})

    def replay(message):
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
                add("system", wrap_lorebook(record["content"]), "lore:" + record["id"], lore_ids=[record["id"]],
                    anchor_id=message_id, title=record["title"])

    for message in visible:
        anchored(message["id"])
        # Native tool calls/results belong before the final narrative that ended that turn.
        records = by_owner.get(message["id"], [])
        for record in records:
            replay(record)
        add(message["role"], history_text[message["id"]], "history", message_id=message["id"])
        final = next((m for m in records if m.get("_full_agent_final")), None)
        if final and items and items[-1].get("message_id") == message["id"]:
            items[-1].update({k: copy.deepcopy(final[k]) for k in (
                "_native_response", "_response_origin", "_response_digest", "reasoning_content",
                "reasoning_details", "reasoning", "_context_epoch", "_native_epoch") if k in final})
            items[-1] = ledger.native_message(items[-1])
    anchored(input_id)
    add("user", user_input, "input", message_id=input_id)
    for message in continuation:
        replay(message)
    update("turn_variable_instruction",
           "本轮须调用 patch_state 更新 MVU 变量一次，无变化提交 []，再回复正文；已调用成功则不要重复。" if require_state_patch else "",
           value={"required": True, "input_id": input_id} if require_state_patch else False)
    if require_state_patch:
        # Actual turns supply a durable, frozen snapshot. Read-only previews
        # collect the same rules without marking them as already injected.
        rules = (collect_variable_rules(document, session["persona"], snapshot.get("state", {}))
                 if variable_rules is None else variable_rules)
        update("mvu_rules", variable_rules_message(rules), value=rules)
    update("post", expand(wrap_card_field("post_history_instructions", card.get("post_history_instructions", ""))))
    update("turn_instruction", instruction)
    warning = ""
    if checkpoint.get("stale"):
        refs = [f"H{i+1}" for i, m in enumerate(history) if m["id"] in state.get("edited_sources", [])]
        warning = "原始剧情已编辑，checkpoint 可能过时。请读取并核对：" + ", ".join(refs)
    update("context_warning", warning)
    update("checkpoint", "<tavern_memory>\n当前有效 checkpoint（取代此前版本；后续以最新记忆更新或成功写入的工具记录为准）：\n"
        + (checkpoint["text"] or "当前 checkpoint 为空；此前版本不再作为当前记忆。") + "\n</tavern_memory>",
        value=checkpoint["text"])
    available = settings.context_tokens
    allowed_fields = {"role", "content", "tool_calls", "tool_call_id", "name",
                      "reasoning_content", "reasoning_details", "reasoning",
                      "_native_response", "_response_origin", "_response_digest"}

    def wire():
        return [{k: v for k, v in m.items() if k in allowed_fields} for m in items]

    reasoning_cost = sum(count_text(json.dumps({k: m[k] for k in
        ("reasoning_content", "reasoning_details", "reasoning") if k in m}, ensure_ascii=False), settings)
        for m in items if not m.get("_native_response")
        and any(k in m for k in ("reasoning_content", "reasoning_details", "reasoning")))
    active_notes = sum(note.get("status", "active") == "active" for note in state["notes"].values())
    archived_notes = len(state["notes"]) - active_notes
    memory_status = f"长期笔记：有效 {active_notes} 条，已归档 {archived_notes} 条。"
    history_status = f"原始历史 H1–H{len(history)}" if history else "尚无原始历史"
    visible_status = f"当前保留 H{window['start']+1} 起的正文" if visible else "当前窗口没有已完成的历史正文"
    status = (f"上下文窗口：当前第 {window['number']} 个，总计 {window['number']} 个。\n"
              f"{history_status}；当前用户输入引用 H{len(history)+1}；{visible_status}。\n"
              f"{memory_status}\n"
              f"本回合还可调用模型 {max(0, settings.full_agent_config.max_steps - calls)} 次。")
    update("context_status", status)
    tokens = request_tokens(wire(), settings, tools) + reasoning_cost
    rollover_required = force_rollover or tokens >= settings.context_tokens
    if rollover_required or tokens >= settings.context_tokens * settings.full_agent_config.pressure_ratio:
        budget_item = None
        # Include the notice itself when checking the boundary and estimating its size.
        for _ in range(6):
            rollover_required |= tokens >= settings.context_tokens
            content = (
                f"当前上下文估算 {tokens}/{settings.context_tokens} tokens（含工具定义）；"
                f"本轮回复额度 {settings.reply_tokens} tokens。\n"
                + ("当前进入强制换窗状态：当前上下文已达到或超过窗口上限。"
                   "必须单独调用 new_context 保存完整交接笔记并换窗；运行时否决其他一切工具调用和正文回复。"
                   "换窗成功后继续当前用户请求。"
                   if rollover_required else "窗口预算提醒，供安排交接与换窗时机；当前仍可正常调用工具和回复。")
            )
            if budget_item is None:
                budget_item = update("context_budget", content)
                if budget_item is None:
                    break
            else:
                budget_item["content"] = content
                budget_item["_runtime_context"]["value"] = content
            updated = request_tokens(wire(), settings, tools) + reasoning_cost
            if updated == tokens:
                break
            tokens = updated
    else:
        update("context_budget", "")
    messages = wire()
    tokens = request_tokens(messages, settings, tools) + reasoning_cost
    result = {"messages": messages, "trace": [{**m, "position": i, "estimated_tokens": message_tokens(m, settings)}
                                            for i, m in enumerate(items)],
            "estimated_tokens": tokens, "tokens": tokens, "available_tokens": available,
            "rollover_required": rollover_required, "context_limit": settings.context_tokens,
            "budget": available, "token_counting": counting_info(settings), "diagnostics": [],
            "tools": tools, "agent_mode": "full_agent", "window": {k: v for k, v in window.items() if k != "id"},
            "daily_context_budget": available, "window_message_count": len(visible) + bool(user_input),
            "budget_sources": {**{source: sum(message_tokens(m, settings) for m in items if m["source"] == source)
                                  for source in dict.fromkeys(m["source"] for m in items)},
                               "tool_definitions": request_tokens([], settings, tools) - 3},
            "next_lore_injections": copy.deepcopy(snapshot.get("lore_injections", []))}
    result["context_ledger"] = ledger.audit()
    result["context_additions"] = copy.deepcopy(ledger.additions)
    return prepare_context(result, {**session, "agent_mode": "full_agent"}, history, snapshot, settings)
