"""Independent background analysis; the turn orchestrator owns state application."""

import asyncio
import hashlib
import json

from tavern.character.cards import card_view, variable_rule_entries
from tavern.conversation.context import macros
from tavern.conversation.variable_rules import wrap_mvu_rules
from tavern.core.message_adapter import provider_messages, runtime_messages
from tavern.core.model_view import state_for_model
from tavern.core.tokens import count_text
from tavern.scripting.agent import make_provider

RECENT_MESSAGES = 6
SYSTEM_PROMPT = """你是后台 MVU 变量分析员。根据当前变量、近期历史和本轮对话，判断本轮造成的变量变化。
历史只用于理解上下文，不重复应用历史事件；只更新有本轮事实依据的变量，不推进剧情。
仅返回 JSON Patch 数组，路径相对于 stat_data，使用 add、replace、remove、move、copy、test 操作。
无变化返回 []。不要输出正文、分析或 Markdown。"""


async def analyze_variables(
    store,
    tid,
    settings,
    history,
    state,
    user_text,
    narrative,
    provider_factory=None,
    *,
    document=None,
    persona="用户",
    request_context=None,
    scopes=None,
):
    cfg = settings.for_role("background") if hasattr(settings, "for_role") else settings
    rules = []
    for entry in variable_rule_entries(document or {"name": ""}):
        content = macros(entry.get("content", ""), card_view(document), persona, state, scopes)
        rules.append(
            {
                "id": entry["id"],
                "content": content,
                "version": hashlib.sha256(
                    json.dumps(entry, sort_keys=True, ensure_ascii=False).encode()
                ).hexdigest(),
                "tokens": count_text(content, cfg),
            }
        )
    messages = [
        {"role": "system", "message_type": "root_system", "content": SYSTEM_PROMPT},
        *(
            [
                {
                    "role": "system",
                    "message_type": "variable_mvu_rules",
                    "content": wrap_mvu_rules(
                        "本轮 MVU 规则（输出仍须遵循 JSON Patch 数组协议）：\n"
                        + "\n\n".join(r["content"] for r in rules)
                    ),
                }
            ]
            if rules
            else []
        ),
        {
            "role": "user",
            "content": json.dumps(
                {
                    "current_variables": state_for_model(state).get("stat_data", {}),
                    "recent_history": [
                        {"role": m["role"], "content": m.get("model", m["raw"])}
                        for m in history[-RECENT_MESSAGES:]
                    ],
                    "current_turn": {"user": user_text, "assistant": narrative},
                },
                ensure_ascii=False,
            ),
        },
    ]
    messages = runtime_messages(messages, "background")
    # No window warning here: MVU sends one request per turn and its recent
    # history slides each turn, so no earlier observation can cover this input.
    store.event(tid, "variable_rules", {"purpose": "variables", "entries": rules})
    store.event(tid, "variable_update_started", {"purpose": "variables"})
    provider = (
        provider_factory()
        if provider_factory
        else await make_provider(
            cfg,
            lambda payload: store.event(tid, "provider_request", {**payload, "purpose": "variables"}),
        )
    )
    provider.operation_purpose = "variables"
    if request_context:
        from tavern.scripting.request_context import configure_operation

        configure_operation(
            provider, request_context, lambda kind, payload: store.event(tid, kind, payload), "variables"
        )
    try:
        outgoing = provider_messages(provider, messages, cfg) if hasattr(cfg, "api_mode") else messages
        result = await asyncio.wait_for(provider.chat_complete(outgoing), settings.memory_timeout)
        store.event(tid, "usage", {"purpose": "variables", "calls": 1, "usage": result.usage or {}})
        operations = json.loads(result.content)
        if not isinstance(operations, list) or any(not isinstance(op, dict) for op in operations):
            raise ValueError("后台变量结果必须是 JSON Patch 数组")
        return operations
    finally:
        await provider.close()
