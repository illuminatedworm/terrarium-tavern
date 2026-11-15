"""Compact persisted turn records; large payloads are loaded separately."""

import json

LABELS = {
    "provider_response": "模型原生响应",
    "story_response": "模型响应",
    "story_assistant": "Agent 中间回复",
    "story_tool_started": "工具调用开始",
    "story_tool": "剧情工具调用",
    "context_rollover": "工作窗口切换",
    "context_warning": "上下文超长提示",
    "context_baseline": "上下文基线重建",
    "context_replay": "上下文分批整理",
    "context_compacted": "云端上下文已压缩",
    "variable_rules": "MVU 规则注入",
    "started": "回合开始",
    "state_proposal": "状态修改提案",
    "content_proposal": "设定修改提案",
    "script_log": "角色脚本日志",
    "awaiting_review": "等待审核",
    "committed": "变更已提交",
    "cancelled": "任务已取消",
    "error": "执行失败",
    "provider_request": "模型请求",
    "usage": "模型用量",
    "activity": "执行活动",
}


def work_records(events):
    for event in events:
        if event["kind"] in ("text", "text_reset"):
            continue
        payload = event["payload"]
        purpose = payload.get("purpose", "runtime") if isinstance(payload, dict) else "runtime"
        yield {**event, "purpose": purpose}


def compact_record(event):
    payload = event["payload"] if isinstance(event["payload"], dict) else {"message": str(event["payload"])}
    kind = event["kind"]
    label = LABELS.get(kind, kind)
    description = payload.get("message") or payload.get("reason") or ""
    if kind == "provider_request":
        description = f"{payload.get('model', '')} · {len(payload.get('messages', []))} 条输入 · {len(payload.get('tools', []))} 个工具"
    if kind == "usage" and payload.get("status") == "failed":
        label = "失败请求用量"
        description = "接口返回的计费用量；正文未提交，不作为有效窗口实测"
    if kind == "activity":
        label = str(payload.get("kind") or label)
    if kind == "context_baseline":
        description = {
            "initial_context": "建立当前窗口的首个基线",
            "legacy_context": "旧记录首次接入追加式续答",
            "context_version_changed": "上下文记录版本变化",
            "provider_or_model_changed": "提供商、认证或模型变化，使用可迁移历史重建",
            "context_sources_changed": "固定资料、工具或请求策略变化",
            "history_content_changed": "历史正文或宏展开结果变化",
            "context_prefix_changed": "原有内容或注入位置改变，按原编排重建",
            "context_window_changed": "可见历史窗口或分支改变",
        }.get(payload.get("rebuild_reason"), payload.get("rebuild_reason", ""))
    return {
        "seq": event["seq"],
        "kind": kind,
        "label": label,
        "purpose": event["purpose"],
        "created": event["created"],
        "description": str(description) if kind == "error" else str(description)[:180],
        "bytes": len(json.dumps(payload, ensure_ascii=False).encode()),
    }
