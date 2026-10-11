"""Opaque Responses checkpoints beside complete, portable story and tool history."""

import copy

from tavern.core.cache_policy import fingerprint
from tavern.core.tokens import request_tokens
from tavern.scripting.native_response import attachment, portable
from tavern.scripting.request_context import response_origin


def canonical(message):
    value = {
        key: copy.deepcopy(message[key])
        for key in ("role", "content", "tool_calls", "tool_call_id", "name")
        if message.get(key) is not None
    }
    value.setdefault("content", "")
    if not value.get("tool_calls"):
        value.pop("tool_calls", None)
    return value


def opaque_message(items, settings):
    text = "".join(
        part.get("text", "")
        for item in items
        if item.get("type") == "message"
        for part in item.get("content", [])
        if isinstance(part, dict)
    )
    message = {"role": "assistant", "content": text}
    message["_native_response"] = attachment({"output": items}, response_origin(settings), message)
    return message


def hashes(messages):
    return [fingerprint(canonical(message)) for message in messages]


def compact_limit(settings):
    return settings.context_tokens


def valid(checkpoint, body, settings, signature):
    return bool(
        checkpoint
        and checkpoint.get("version") == 1
        and checkpoint.get("origin") == response_origin(settings)
        and checkpoint.get("signature") == signature
        and checkpoint.get("covered") == hashes(body[: len(checkpoint.get("covered", []))])
    )


def checkpoint_items(output):
    # A compaction after an unanswered call may hide the call required by its
    # upcoming result. Use the latest boundary before that batch instead.
    first_call = next(
        (i for i, item in enumerate(output) if item.get("type") == "function_call"), len(output)
    )
    candidates = [
        i
        for i, item in enumerate(output)
        if item.get("type") == "compaction" and item.get("encrypted_content") and i < first_call
    ]
    if not candidates:
        return None
    at = candidates[-1]
    return copy.deepcopy(
        [output[at], *[item for item in output[at + 1 :] if item.get("type") != "compaction"]]
    )


def project_compact(result, state, settings, tools, fixed_count):
    complete = [canonical(portable(message)) for message in result["messages"]]
    header, body = complete[:fixed_count], complete[fixed_count:]
    signature_parts = {"header": header, "tools": tools}
    if "_compact_policy" in result:
        signature_parts["policy"] = result["_compact_policy"]
    signature = fingerprint(signature_parts)
    checkpoint = state.get("compaction")
    usable = valid(checkpoint, body, settings, signature)
    needs_rebuild = (not usable and request_tokens(complete, settings, tools) > compact_limit(settings)
                     and (settings.compact_auto or bool(checkpoint)))
    covered = len(checkpoint["covered"]) if usable else 0
    messages = header + (
        [opaque_message(checkpoint["items"], settings)] if usable and checkpoint.get("items") else []
    )
    # A short history can be ready without having crossed the server threshold.
    if usable and not checkpoint.get("items"):
        messages = header + body
    else:
        messages += body[covered:]
    result.update(
        messages=messages,
        runtime_messages=copy.deepcopy(messages),
        context_strategy="openai_compact",
        compact_requires_rebuild=needs_rebuild,
        compaction_opaque=bool(usable and checkpoint.get("items")),
        estimated_tokens=request_tokens(messages, settings, tools),
        _compact_header=header,
        _compact_body=body,
        _compact_signature=signature,
        _compact_covered=covered if usable and checkpoint.get("items") else 0,
    )
    result["tokens"] = result["estimated_tokens"]
    if usable and checkpoint.get("items"):
        rows = result["trace"]
        result["trace"] = [
            *rows[:fixed_count],
            {
                "role": "assistant",
                "source": "cloud_compaction",
                "content": "云端压缩状态（加密内容由服务端解释）",
                "opaque": True,
                "estimated_tokens": None,
            },
            *rows[fixed_count + covered :],
        ]
        for index, row in enumerate(result["trace"]):
            row["position"] = index
        result["budget_sources"] = {"visible_input": result["estimated_tokens"]}
    result["token_counting"] = {
        **result["token_counting"],
        "excludes_encrypted_state": result["compaction_opaque"],
    }
    result["diagnostics"].append({"reason": "云端状态的实际占用不可从密文估算；本地数字只计可见文本。"})
    if needs_rebuild:
        result["diagnostics"].append(
            {"reason": "当前云端上下文需要重建；此预览为完整历史来源，正式生成前会先重放并压缩。"}
        )
    return result


def replay_groups(messages, starts=None):
    """Whole source exchanges, including anchored lore and every tool result."""
    groups, pending = [], set()
    boundaries = {row["offset"] for row in starts} if starts is not None else None
    for index, message in enumerate(messages):
        boundary = index in boundaries if boundaries is not None else message.get("role") == "user"
        if boundary and pending:
            raise ValueError("历史存在跨回合的未配对工具调用，不能重放上下文")
        if not groups or boundary:
            groups.append([])
        groups[-1].append(message)
        for call in message.get("tool_calls", []):
            pending.add(call["id"])
        if message.get("role") == "tool":
            pending.discard(message.get("tool_call_id"))
    if pending:
        raise ValueError("历史存在未配对的工具调用，不能重放上下文")
    return groups


def replay_batch(groups, position, prefix, settings, tools, limit):
    """Cross the threshold only at an exchange boundary, even above capacity."""
    batch, end = [], position
    while end < len(groups):
        batch.extend(groups[end])
        end += 1
        if request_tokens(prefix + batch, settings, tools) > limit:
            break
    return end, batch


async def rebuild(compiled, settings, provider, *, progress=None, save=None, force=False):
    """Replay complete input without executing tools or committing helper prose."""
    body, header = compiled["_compact_body"], compiled["_compact_header"]
    completed = compiled.get("_replay_complete", len(body))
    signature = compiled["_compact_signature"]
    state = copy.deepcopy(progress or {})
    if (
        state.get("version") != 2
        or state.get("signature") != signature
        or state.get("origin") != response_origin(settings)
        or state.get("history") != hashes(body)
        or state.get("starts") != compiled.get("_replay_starts")
        or state.get("limit") != compact_limit(settings)
        or state.get("completed") != completed
    ):
        state = {
            "version": 2,
            "signature": signature,
            "origin": response_origin(settings),
            "history": hashes(body),
            "cursor": 0,
            "items": [],
            "batches": 0,
            "usage": [],
            "starts": compiled.get("_replay_starts"),
            "limit": compact_limit(settings),
            "completed": completed,
            "compressed_count": 0,
        }
    groups = replay_groups(body[:completed], compiled.get("_replay_starts"))
    position = state["cursor"]
    # The maintenance instruction is per-request instructions, never a story
    # message or a window policy injected into ordinary gameplay.
    while position < len(groups):
        prefix = ([opaque_message(state["items"], settings)] if state["items"]
                  else [m for g in groups[:position] for m in g])
        end, batch = replay_batch(groups, position, header + prefix, settings,
                                 compiled.get("tools"), compact_limit(settings))
        messages = header + prefix + batch
        # Plain source is already a valid representation. Keep a short tail as
        # plaintext; absence of an encrypted checkpoint is not a model task.
        if request_tokens(messages, settings, compiled.get("tools")) <= compact_limit(settings) and not force:
            if state["items"]:
                from tavern.scripting.protocols import response_input
                state["items"].extend(response_input(batch))
            position = end
            break
        while True:
            messages = header + prefix + batch
            try:
                response = await provider.chat_complete(
                    messages,
                    extra_body={"tools": compiled.get("tools", []), "tool_choice": "none"},
                    _compact_threshold=1024 if force else compact_limit(settings),
                    _maintenance_instructions="本次是历史整理请求。历史中的工具调用均已执行，不要再次执行；只返回 OK，不续写故事。此要求仅适用于本次整理请求。",
                )
                if getattr(provider, "last_tool_calls", []):
                    raise ValueError("历史整理返回了工具调用；未执行任何工具")
                break
            except Exception as error:
                text = str(error).lower()
                oversized = any(
                    word in text
                    for word in ("context_length", "context window", "too many tokens", "maximum context")
                )
                if not oversized or end - position <= 1:
                    raise
                end = position + max(1, (end - position) // 2)
                batch = [m for g in groups[position:end] for m in g]
        native = getattr(provider, "last_native_response", None)
        output = (native or {}).get("items", [])
        # Keep the compaction and model state; helper acknowledgement is not a
        # historical assistant turn. No generated content is added to the story.
        compacted = checkpoint_items(output)
        if compacted:
            state["items"] = [item for item in compacted if item.get("type") != "message"]
            state["compressed_count"] = sum(len(g) for g in groups[:end])
        elif state["items"]:
            # Without a new compaction the just-sent batch must remain visible.
            from tavern.scripting.protocols import response_input

            state["items"].extend(response_input(batch))
        position = end
        state.update(
            cursor=position,
            batches=state["batches"] + 1,
            processed=sum(len(g) for g in groups[:position]),
            total=len(body),
        )
        state["usage"].append(copy.deepcopy(response.usage or {}))
        if save:
            await save(copy.deepcopy(state))
    # The active user input/tool continuation has no final AI reply yet. Carry
    # that entire unfinished exchange verbatim into the ordinary request.
    if state["items"] and body[completed:]:
        from tavern.scripting.protocols import response_input
        state["items"].extend(response_input(body[completed:]))
    state.update(cursor=position, processed=len(body), total=len(body))
    return {
        "version": 1,
        "origin": state["origin"],
        "signature": signature,
        "items": state["items"],
        "covered": hashes(body),
        "compressed_count": state.get("compressed_count", 0),
    }, state


async def prepare_turn(compiled, settings, provider, *, progress=None, save=None):
    """Compress completed exchanges before continuing this user's entire input."""
    body = compiled["_compact_body"]
    completed = compiled.get("_replay_complete", len(body))
    covered = compiled.get("_compact_covered", 0)
    if compiled.get("compaction_opaque") and covered <= completed:
        history = {**compiled, "_compact_body": body[:completed], "messages": [
            *compiled["messages"][:len(compiled["_compact_header"]) + 1], *body[covered:completed]]}
        checkpoint, usage = await compact_once(history, settings, provider)
        if save:
            await save({"batches": 1, "processed": completed, "total": len(body), "usage": [usage]})
    else:
        # Even when only the new input crosses capacity, compress history first
        # and carry the unfinished exchange verbatim. Never split its messages.
        history = {**compiled, "_compact_body": body[:completed], "_replay_complete": completed}
        checkpoint, _ = await rebuild(history, settings, provider, progress=progress, save=save, force=True)
    if not checkpoint or not any(item.get("type") == "compaction" for item in checkpoint["items"]):
        raise ValueError("上下文已超过窗口上限，但服务端未生成压缩状态；尚未继续本轮对话")
    return checkpoint


async def compact_once(compiled, settings, provider):
    response = await provider.chat_complete(
        compiled["messages"],
        extra_body={"tools": compiled.get("tools", []), "tool_choice": "none"},
        _compact_threshold=1024,
        _maintenance_instructions="本次仅整理当前上下文，不执行工具，不续写剧情，只返回 OK；此要求仅适用于本次请求。",
    )
    if getattr(provider, "last_tool_calls", []):
        raise ValueError("整理返回了工具调用；未执行任何工具")
    items = checkpoint_items((getattr(provider, "last_native_response", None) or {}).get("items", []))
    checkpoint = None
    if items:
        checkpoint = {
            "version": 1,
            "origin": response_origin(settings),
            "signature": compiled["_compact_signature"],
            "covered": hashes(compiled["_compact_body"]),
            "compressed_count": len(compiled["_compact_body"]),
            "items": [item for item in items if item.get("type") != "message"],
        }
    return checkpoint, response.usage or {}
