"""Full Agent handoff conversion and window-local read permissions."""

import copy

from pydantic import ValidationError

from tavern.conversation.compaction import canonical, checkpoint_items, hashes, replay_batch, replay_groups
from tavern.core.cache_policy import fingerprint
from tavern.core.history_refs import message_ref
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


async def rebuild_local(compiled, settings, provider, history, *, progress=None, save=None):
    """Derive local windows from originals, never from an old cloud checkpoint.

    Each overflowing batch adds the chapter(s) for the exchanges it retires and
    replaces the current handoff, under the same coverage and limits as new_context.
    """
    from tavern.full_agent.handoff import render_text

    header, body = compiled["_replay_header"], compiled["_replay_body"]
    starts = compiled["_replay_starts"]
    groups = replay_groups(body, starts)
    config = settings.full_agent_config
    identity = fingerprint(
        {
            "header": header,
            "body": body,
            "starts": starts,
            "tools": compiled["tools"],
            "origin": response_origin(settings),
            "limit": settings.context_tokens,
            "handoff": [config.handoff_tokens, config.chapter_tokens, config.chapters_total_tokens],
        }
    )
    state = copy.deepcopy(progress or {})
    if state.get("identity") != identity:
        state = {
            "identity": identity,
            "cursor": 0,
            "retained": 0,
            "handoff": {"chapters": [], "current": [], "revision": 0},
            "batches": 0,
            "usage": [],
        }
    position = state["cursor"]
    while position < len(groups):
        rendered = render_text(state["handoff"], history)
        memory = (
            [{"role": "system", "content": "<tavern_memory>\n" + rendered + "\n</tavern_memory>"}]
            if rendered
            else []
        )
        retained = [m for g in groups[state["retained"] : position] for m in g]
        prefix = header + memory + retained
        end, batch = replay_batch(
            groups, position, prefix, settings, compiled["tools"], settings.context_tokens
        )
        messages = prefix + batch
        if request_tokens(messages, settings, compiled["tools"]) < settings.context_tokens:
            position = end
            break
        # Keep the last whole exchange, including its tools and anchored lore.
        kept = max(0, end - 1)
        handoff, usage = await make_handoff(
            {"messages": messages},
            settings,
            provider,
            state["handoff"],
            history,
            starts[kept]["history_start"],
        )
        position = end
        state.update(
            cursor=position,
            retained=kept,
            handoff=handoff,
            batches=state["batches"] + 1,
            processed=sum(len(g) for g in groups[:position]),
            total=len(body),
        )
        state["usage"].extend(copy.deepcopy(usage))
        if save:
            await save(copy.deepcopy(state))
    state.update(cursor=position, processed=len(body), total=len(body))
    state["history_start"] = starts[state["retained"]]["history_start"] if groups else 0
    return state


async def make_handoff(compiled, settings, provider, handoff, history, through):
    """Prepare a covered handoff, allowing one correction of a rejected model proposal."""
    from tavern.full_agent.handoff import (
        HandoffError,
        HandoffWrite,
        apply_write,
        chapter_spans,
        chapter_token_counts,
        check_limits,
        require_coverage,
    )

    config = settings.full_agent_config
    covered = chapter_spans(handoff, history)[-1][1] if handoff["chapters"] else 0
    used = sum(chapter_token_counts(handoff, history, settings))
    chapters = (
        f"从 {message_ref(covered + 1)} 起用 add 新增章节，覆盖到 {message_ref(through)}（这些原文将退出窗口）；"
        "只需填写标题和要点，省略 through 时程序自动记录退出范围。"
        "章节只保留以后仍有影响的经历、关系转折、约定、秘密与知情者；要点无需逐条引用 H，有精确回查需要时才引用已知编号。"
        if through > covered
        else "本批没有新的原文退出窗口，不需要新增章节。"
    )
    instruction = (
        "用户正在将此对话切换为本地换窗。本次任务是生成交接笔记，之前已完成的历史整理所要求的 OK 回复格式不适用于本次。"
        + chapters
        + "current 写当前场景、关系、约束、未决事项和文风，不复述章节。"
        "不要把完整长期笔记或作者未来大纲复制到 handoff。"
        "调用 prepare_handoff 返回结果，不续写剧情，不修改 notes、剧情规划或变量。"
        f"上限：当前交接 {config.handoff_tokens} tokens，单章 {config.chapter_tokens}，章节总量 {config.chapters_total_tokens}。"
        f"现有章节总量约 {used} tokens，剩余额度约 {max(0, config.chapters_total_tokens - used)} tokens。"
        "若新增章节会接近或超过总量上限，在同一次调用中用 merge 合并最旧的相邻章节，再 add 新章；"
        "C 编号始终按调用前的章节编号填写。合并保留长期有效信息，原文范围仍可回读。"
    )
    definition = {
        "type": "function",
        "function": {
            "name": "prepare_handoff",
            "description": "Return chapter operations and the complete current handoff for this explicit context-mode transition. This does not execute story tools.",
            "parameters": HandoffWrite.model_json_schema(),
        },
    }
    messages = [*compiled["messages"], {"role": "developer", "content": instruction}]
    usage = []
    for attempt in range(2):
        response = await provider.chat_complete(
            messages,
            extra_body={
                "tools": [definition],
                "tool_choice": {"type": "function", "function": {"name": "prepare_handoff"}},
            },
            _compact_disabled=True,
        )
        usage.append(response.usage or {})
        try:
            calls = getattr(provider, "last_tool_calls", [])
            if len(calls) != 1 or calls[0].name != "prepare_handoff":
                raise HandoffError("INVALID_ARGUMENTS", "模型未返回完整的结构化交接，尚未切换模式")
            result = apply_write(
                handoff,
                HandoffWrite.model_validate_json(calls[0].arguments),
                history,
                archive_through=through,
            )
            check_limits(result, history, config, settings)
            require_coverage(result, history, through)
            return result, usage
        except (HandoffError, ValidationError) as error:
            # Retry only rejected model output. Transport failures propagate, and
            # neither failed edits nor arbitrary argument values enter the repair input.
            if attempt:
                raise
            feedback = (
                str(error)
                if isinstance(error, HandoffError) and error.code in ("CONTENT_LIMIT", "HANDOFF_COVERAGE")
                else "结构或引用不符合参数定义；只使用当前已知的 C 章节和 H 原文编号，章节要点的引用须在章内。"
            )
            messages = [
                *messages,
                {
                    "role": "developer",
                    "content": "上次 prepare_handoff 校验失败，提案未应用："
                    + feedback
                    + "请基于本批原文和调用前的章节重新提交完整操作及 current；这是本批唯一一次修正机会。",
                },
            ]
