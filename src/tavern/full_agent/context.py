"""A transparent working window: no legacy recipe, compactor, or memory engine."""
import copy
import json

from tavern.character.cards import lore_entries, normalize
from tavern.conversation.context import character_content, macros, wrap_lorebook
from tavern.core.tokens import count_text, counting_info, message_tokens, request_tokens
from tavern.full_agent.state import full_state


def load_trace(store, sid, history):
    allowed = {m["id"] for m in history}
    with store.connection() as db:
        rows = db.execute("SELECT * FROM full_agent_trace WHERE session=? ORDER BY message_id,seq", (sid,)).fetchall()
    return [{**dict(row), "message": json.loads(row["payload"])} for row in rows if row["message_id"] in allowed]


def compile_window(document, session, history, snapshot, user_input, settings, tools, *,
                   trace=(), continuation=(), input_id="pending", instruction="", calls=0, force_rollover=False):
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
    add("system", "<user_character>\n玩家：" + session["persona"] + "\n"
        + session.get("persona_data", {}).get("description", "") + "\n</user_character>", "persona")
    add("system", expand(card.get("mes_example", "")), "examples")
    checkpoint = state["checkpoint"]
    add("system", "<tavern_memory>\nAgent checkpoint（模型维护的交接笔记）：\n"
        + (checkpoint["text"] or "尚无 checkpoint。") + "\n</tavern_memory>", "checkpoint")
    if checkpoint.get("stale"):
        refs = [f"H{i+1}" for i, m in enumerate(history) if m["id"] in state.get("edited_sources", [])]
        add("system", "原始剧情已编辑，checkpoint 可能过时。请读取并核对：" + ", ".join(refs), "context_warning")

    by_owner = {}
    for item in trace:
        if item["window_id"] == window["id"]:
            by_owner.setdefault(item["message_id"], []).append(item["message"])

    def anchored(message_id):
        for record in snapshot.get("lore_injections", []):
            if record["anchor_id"] == message_id and not record.get("manually_removed"):
                add("system", wrap_lorebook(record["content"]), "lore:" + record["id"], lore_ids=[record["id"]],
                    anchor_id=message_id, title=record["title"])

    for message in visible:
        anchored(message["id"])
        # Native tool calls/results belong before the final narrative that ended that turn.
        items.extend({**copy.deepcopy(m), "source": "agent_tools"} for m in by_owner.get(message["id"], []))
        add(message["role"], expand(message.get("model", message["raw"])), "history", message_id=message["id"])
    anchored(input_id)
    add("user", user_input, "input", message_id=input_id)
    items.extend({**copy.deepcopy(m), "source": "agent_tools"} for m in continuation)
    add("system", expand(card.get("post_history_instructions", "")), "post")
    add("system", instruction, "turn_instruction")
    available = settings.context_tokens
    allowed_fields = {"role", "content", "tool_calls", "tool_call_id", "name",
                      "reasoning_content", "reasoning_details", "reasoning"}

    def wire():
        return [{k: v for k, v in m.items() if k in allowed_fields} for m in items]

    reasoning_cost = sum(count_text(json.dumps({k: m[k] for k in
        ("reasoning_content", "reasoning_details", "reasoning") if k in m}, ensure_ascii=False), settings)
        for m in items if any(k in m for k in ("reasoning_content", "reasoning_details", "reasoning")))
    active_notes = sum(note.get("status", "active") == "active" for note in state["notes"].values())
    archived_notes = len(state["notes"]) - active_notes
    memory_status = f"长期笔记：有效 {active_notes} 条，已归档 {archived_notes} 条。"
    history_status = f"原始历史 H1–H{len(history)}" if history else "尚无原始历史"
    visible_status = f"当前保留 H{window['start']+1} 起的正文" if visible else "当前窗口没有已完成的历史正文"
    status = (f"上下文窗口：当前第 {window['number']} 个，总计 {window['number']} 个。\n"
              f"{history_status}；当前玩家输入引用 H{len(history)+1}；{visible_status}。\n"
              f"{memory_status}\n"
              f"本回合还可调用模型 {max(0, settings.full_agent_config.max_steps - calls)} 次。")
    memory_at = next(i for i, item in enumerate(items) if item["source"] == "checkpoint") + 1
    items.insert(memory_at, {"role": "system", "content": status, "source": "context_status"})
    tokens = request_tokens(wire(), settings, tools) + reasoning_cost
    rollover_required = force_rollover or tokens >= settings.context_tokens
    if rollover_required or tokens >= settings.context_tokens * settings.full_agent_config.pressure_ratio:
        budget_item = {"role": "system", "content": "", "source": "context_budget"}
        items.insert(memory_at + 1, budget_item)
        # Include the notice itself when checking the boundary and estimating its size.
        for _ in range(6):
            rollover_required |= tokens >= settings.context_tokens
            budget_item["content"] = (
                f"当前上下文估算 {tokens}/{settings.context_tokens} tokens（含工具定义）；"
                f"本轮回复额度 {settings.reply_tokens} tokens。\n"
                + ("当前进入强制换窗状态：当前上下文已达到或超过窗口上限。"
                   "必须单独调用 new_context 保存完整交接笔记并换窗；运行时否决其他一切工具调用和正文回复。"
                   "换窗成功后继续当前玩家请求。"
                   if rollover_required else "窗口预算提醒，供安排交接与换窗时机；当前仍可正常调用工具和回复。")
            )
            updated = request_tokens(wire(), settings, tools) + reasoning_cost
            if updated == tokens:
                break
            tokens = updated
    messages = wire()
    tokens = request_tokens(messages, settings, tools) + reasoning_cost
    return {"messages": messages, "trace": [{**m, "position": i, "estimated_tokens": message_tokens(m, settings)}
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
