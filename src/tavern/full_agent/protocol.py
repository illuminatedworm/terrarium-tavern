"""Full Agent result envelopes and explicit, actionable failure categories."""

RECOVERY = {
    "UNKNOWN_TOOL": (True, "使用本轮工具目录中的名称。"),
    "PERMISSION_DENIED": (False, "遵守本轮授权；需要时由用户调整工具开关或授权。"),
    "INVALID_ARGUMENTS": (True, "按参数定义修正后重试。"),
    "INVALID_REFERENCE": (True, "重新搜索或读取当前分支中的有效引用。"),
    "READ_REQUIRED": (True, "在当前窗口完整读取最新目标后重试。"),
    "INVALID_PATH": (
        True,
        "使用 JSON Pointer；变量路径相对于 stat_data，如 /hp，不是 stat_data.hp 或 /stat_data/hp。不要自动删除真实同名键。",
    ),
    "PATCH_VALIDATION_FAILED": (True, "核对最新变量与 MVU 规则，修正补丁后重试。"),
    "CONTENT_LIMIT": (True, "精简本次写入内容后重试；不能截断读取的原文。"),
    "PROTECTED_TARGET": (False, "仅修改工具公开的业务字段；程序内部字段与执行配置不可修改。"),
    "MIXED_CONTEXT_CALLS": (True, "本批次未执行任何工具；单独调用 new_context。"),
    "PARALLEL_TOOL_CALLS_DISABLED": (True, "每次回复只提出一个工具调用，等待结果后再继续。"),
    "CONTEXT_ROLLOVER_REQUIRED": (True, "单独调用 new_context 后继续本轮。"),
    "HANDOFF_COVERAGE": (
        True,
        "补齐实际退出范围的章节，或增大 keep_recent 保留未归档的完整交流，再单独调用 new_context。",
    ),
}


class ToolError(ValueError):
    def __init__(self, code, message, *, next_action=None, **details):
        retryable, action = RECOVERY[code]
        self.result = {
            "ok": False,
            "error": {
                "code": code,
                "message": message,
                "retryable": retryable,
                "next_action": next_action or action,
                **details,
            },
        }
        super().__init__(message)


class TurnError(ValueError):
    """Terminal failures are recorded for the user, never returned as tool retries."""

    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def success(data):
    return {"ok": True, "data": data}


def succeeded(result):
    return isinstance(result, dict) and result.get("ok") is True


def failed(result):
    return isinstance(result, dict) and result.get("ok") is False


def saved(result):
    return succeeded(result) and result["data"].get("saved_to_draft") is True
