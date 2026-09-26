"""A transparent working window: no legacy recipe, compactor, or memory engine."""
import copy
import json

from tavern.character.cards import lore_entries, normalize
from tavern.conversation.context import macros
from tavern.core.tokens import count_text, counting_info, message_tokens, request_tokens
from tavern.full_agent.state import full_state


def load_trace(store, sid, history):
    allowed = {m["id"] for m in history}
    with store.connection() as db:
        rows = db.execute("SELECT * FROM full_agent_trace WHERE session=? ORDER BY message_id,seq", (sid,)).fetchall()
    return [{**dict(row), "message": json.loads(row["payload"])} for row in rows if row["message_id"] in allowed]


def compile_window(document, session, history, snapshot, user_input, settings, tools, *,
                   trace=(), continuation=(), input_id="pending", instruction="", calls=0):
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
        add("system", session["turn_preference"], "session_preference")
    constants = [e for e in lore_entries(document, "writer") if e["enabled"] and e.get("constant")
                 and "[initvar]" not in e.get("comment", "").lower()]
    for entry in constants:
        role = entry.get("role", "system")
        role = {0: "system", 1: "user", 2: "assistant"}.get(role, role)
        add(role, expand(entry["content"]), "lore:" + entry["id"], lore_ids=[entry["id"]], lore_region="constant")
    add("system", expand("\n\n".join(str(card.get(k, "")) for k in
                                      ("system_prompt", "description", "personality", "scenario"))), "character")
    add("system", "玩家：" + session["persona"] + "\n" + session.get("persona_data", {}).get("description", ""), "persona")
    add("system", expand(card.get("mes_example", "")), "examples")
    checkpoint = state["checkpoint"]
    add("system", "Agent checkpoint（模型维护的交接笔记）：\n" + (checkpoint["text"] or "尚无 checkpoint。"), "checkpoint")
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
                add("system", record["content"], "lore:" + record["id"], lore_ids=[record["id"]],
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
    available = settings.context_tokens - settings.reply_tokens - 2048
    allowed_fields = {"role", "content", "tool_calls", "tool_call_id", "name",
                      "reasoning_content", "reasoning_details", "reasoning"}

    def wire():
        return [{k: v for k, v in m.items() if k in allowed_fields} for m in items]

    estimated = request_tokens(wire(), settings, tools)
    reasoning_cost = sum(count_text(json.dumps({k: m[k] for k in
        ("reasoning_content", "reasoning_details", "reasoning") if k in m}, ensure_ascii=False), settings)
        for m in items if any(k in m for k in ("reasoning_content", "reasoning_details", "reasoning")))
    estimated += reasoning_cost
    status = (f"Full Agent 窗口 {window['number']}；原始历史 H1–H{len(history)}；"
              f"当前保留 H{window['start']+1} 起的正文；可搜索笔记 {len(state['notes'])} 条。\n"
              f"本次估算输入约 {estimated + 180}/{available} tokens（含工具定义）；"
              f"本回合还可调用模型 {max(0, settings.full_agent_config.max_steps - calls)} 次。\n"
              "需要时主动写 checkpoint 并调用 new_context。工具调用与结果在换窗后只能搜索召回。")
    if estimated > available * settings.full_agent_config.pressure_ratio:
        status += "\n窗口接近预算，请现在调用 new_context，避免工具结果挤满下一次请求。"
    add("system", status, "context_status")
    messages = wire()
    tokens = request_tokens(messages, settings, tools) + reasoning_cost
    return {"messages": messages, "trace": [{**m, "estimated_tokens": message_tokens(m, settings)} for m in items],
            "estimated_tokens": tokens, "tokens": tokens, "available_tokens": available,
            "budget": available, "token_counting": counting_info(settings), "diagnostics": [],
            "tools": tools, "agent_mode": "full_agent", "window": {k: v for k, v in window.items() if k != "id"},
            "daily_context_budget": available, "window_message_count": len(visible) + bool(user_input),
            "budget_sources": {**{source: sum(message_tokens(m, settings) for m in items if m["source"] == source)
                                  for source in dict.fromkeys(m["source"] for m in items)},
                               "tool_definitions": request_tokens([], settings, tools) - 3},
            "next_lore_injections": copy.deepcopy(snapshot.get("lore_injections", []))}
