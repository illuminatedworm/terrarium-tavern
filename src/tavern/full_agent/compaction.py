"""Full Agent handoff conversion and window-local read permissions."""
import copy

from tavern.conversation.compaction import canonical, checkpoint_items, hashes, replay_batch, replay_groups
from tavern.core.cache_policy import fingerprint
from tavern.core.tokens import request_tokens
from tavern.scripting.request_context import response_origin


def capture_checkpoint(state, compiled, settings, provider, assistant):
    native = getattr(provider, "last_native_response", None)
    items = checkpoint_items((native or {}).get("items", []))
    if items is None:
        return False
    state["compaction"] = {
        "version": 1,
        "origin": response_origin(settings),
        "signature": compiled["_compact_signature"],
        "items": items,
        "covered": hashes([*compiled["_compact_body"], canonical(assistant)]),
    }
    # Complete-read permissions are window-local, even when the server initiated
    # the transition. Notes and story state are independent of this generation.
    import uuid

    state["window"]["id"] = uuid.uuid4().hex
    state["window"]["number"] += 1
    return True


async def rebuild_local(compiled, settings, provider, *, progress=None, save=None):
    """Derive local windows from originals, never from an old cloud checkpoint."""
    header, body = compiled["_replay_header"], compiled["_replay_body"]
    starts = compiled["_replay_starts"]
    groups = replay_groups(body, starts)
    identity = fingerprint({"header": header, "body": body, "starts": starts,
                            "tools": compiled["tools"], "origin": response_origin(settings),
                            "limit": settings.context_tokens, "handoff_tokens": settings.full_agent_config.handoff_tokens})
    state = copy.deepcopy(progress or {})
    if state.get("identity") != identity:
        state = {"identity": identity, "cursor": 0, "retained": 0, "handoff": "", "batches": 0, "usage": []}
    position = state["cursor"]
    while position < len(groups):
        memory = ([{"role": "system", "content": "<tavern_memory>\n" + state["handoff"] + "\n</tavern_memory>"}]
                  if state["handoff"] else [])
        retained = [m for g in groups[state["retained"]:position] for m in g]
        prefix = header + memory + retained
        end, batch = replay_batch(groups, position, prefix, settings, compiled["tools"], settings.context_tokens)
        messages = prefix + batch
        if request_tokens(messages, settings, compiled["tools"]) < settings.context_tokens:
            position = end
            break
        text, usage = await make_handoff({"messages": messages}, settings, provider)
        position = end
        # Keep the last whole exchange, including its tools and anchored lore.
        state.update(cursor=position, retained=max(0, position - 1), handoff=text,
                     batches=state["batches"] + 1,
                     processed=sum(len(g) for g in groups[:position]), total=len(body))
        state["usage"].append(copy.deepcopy(usage))
        if save:
            await save(copy.deepcopy(state))
    state.update(cursor=position, processed=len(body), total=len(body))
    state["history_start"] = starts[state["retained"]]["history_start"] if groups else 0
    return state


async def make_handoff(compiled, settings, provider):
    from tavern.full_agent.tools import NewContext

    instruction = (
        "用户正在将此对话切换为本地换窗。本次任务是生成交接笔记，之前已完成的历史整理所要求的 OK 回复格式不适用于本次。"
        "基于当前完整上下文保留场景、关系、经历、约定、未决事项和继续所需的线索，区分事实与计划。"
        "不要把完整长期笔记或作者未来大纲复制到 handoff。"
        "调用 prepare_handoff 返回完整交接，不续写剧情，不修改 notes、剧情规划或变量。"
        f"handoff 限 {settings.full_agent_config.handoff_tokens} tokens。"
    )
    definition = {
        "type": "function",
        "function": {
            "name": "prepare_handoff",
            "description": "Return a complete portable handoff for this explicit context-mode transition. This does not execute story tools.",
            "parameters": NewContext.model_json_schema(),
        },
    }
    response = await provider.chat_complete(
        [*compiled["messages"], {"role": "developer", "content": instruction}],
        extra_body={
            "tools": [definition],
            "tool_choice": {"type": "function", "function": {"name": "prepare_handoff"}},
        },
        _compact_disabled=True,
    )
    calls = getattr(provider, "last_tool_calls", [])
    if len(calls) != 1 or calls[0].name != "prepare_handoff":
        raise ValueError("模型未返回完整的结构化交接，尚未切换模式")
    text = NewContext.model_validate_json(calls[0].arguments).handoff
    from tavern.core.tokens import count_text

    if not text.strip() or count_text(text, settings) > settings.full_agent_config.handoff_tokens:
        raise ValueError("新 handoff 为空或超出交接笔记上限，尚未切换模式")
    return text, response.usage or {}
