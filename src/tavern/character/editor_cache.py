"""Choose a cache boundary after a fully committed authoring exchange."""

from tavern.core.cache_policy import retention_policy

FINAL_REF_PREFIX = "上条最终回复的历史引用："
DEFAULT_FOLLOW = {"mode": "auto", "interval": 1, "offset": 1, "baseline": 0, "reply_ref": None}


def follows_replies(cfg):
    return (
        cfg.auth_mode == "openai_api_key"
        and cfg.api_mode == "responses"
        and cfg.cache.mode == "runtime"
        and retention_policy(cfg)[0] == "openai_ttl"
    )


def reply_breakpoint(cfg, policy, replies, messages):
    if not follows_replies(cfg):
        reason = "使用支持显式断点的 OpenAI 官方 API 模型，并在制卡模型设置中选择本地运行时控制后可调整跟进。"
        return {"enabled": False, "reason": reason}

    ref, remaining = None, None
    if policy["mode"] == "manual":
        ref = policy["reply_ref"]
    elif policy["mode"] == "auto":
        progress = (len(replies) - policy["baseline"]) % policy["interval"]
        remaining = policy["interval"] - progress
        index = len(replies) - progress - policy["offset"]
        if index >= 0:
            ref = replies[index]

    # The developer reference follows the entire committed turn, even when
    # the final prose was delivered alongside a closing batch of native tools.
    target = None
    if ref:
        text = FINAL_REF_PREFIX + ref
        target = next(
            (
                i
                for i, message in enumerate(messages)
                if message.get("message_type") == "editor_final_ref" and message["content"] == text
            ),
            None,
        )
    return {
        "enabled": True,
        "reply_ref": ref if target is not None else None,
        "index": target,
        "remaining": remaining,
        "reason": "目标回复的断点不在当前上下文窗口，暂用固定前缀。" if ref and target is None else None,
    }
