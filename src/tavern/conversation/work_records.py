"""Compact persisted background records; large payloads are loaded separately."""

import json

LABELS = {
    "full_agent_response": "模型响应", "full_agent_assistant": "Agent 中间回复",
    "full_agent_tool_started": "工具调用开始",
    "full_agent_tool": "Full Agent 工具调用", "context_rollover": "工作窗口切换",
    "memory_history_recall": "Tracker 历史补查", "memory_history_limit": "历史补查达到3次上限",
    "memory_history_budget": "历史补查预算不足",
    "memory_batch": "记忆整理批次与来源", "memory_batch_completed": "记忆批次返回",
    "memory_review_started": "疑难复核开始", "memory_review_result": "疑难复核结果",
    "memory_rebuild_estimate": "重建批次估算",
    "memory_records_discarded": "可选原话记录已丢弃",
    "memory_tombstone_suppressed": "手工删除内容已屏蔽",
    "memory_retention": "记忆条目准出",
    "variable_rules": "MVU 规则注入",
    "variable_update_started": "变量分析开始", "variable_update_completed": "变量更新完成",
    "variable_update_failed": "变量更新失败",
    "started": "回合开始", "maintenance_started": "设定维护开始", "scene_refresh_started": "手动更新开始",
    "memory_chunk": "滚动窗口推进",
    "memory_window_reset": "滚动窗口重置", "state_proposal": "状态修改提案", "content_proposal": "设定修改提案",
    "script_log": "角色脚本日志", "awaiting_review": "等待审核", "committed": "变更已提交",
    "cancelled": "任务已取消", "error": "执行失败", "provider_request": "模型请求", "usage": "模型用量",
    "activity": "执行活动",
    "scene_refresh_ready": "手动更新结果", "maintenance_completed": "设定维护记录", "memory_proposal": "记忆写入提案",
    "archive_started": "自动档案整理开始", "memory_ready": "记忆整理完成", "memory_failed": "记忆整理失败",
}


def background_records(events):
    phase = "narrative"
    for event in events:
        payload = event["payload"] if isinstance(event["payload"], dict) else {"message": str(event["payload"])}
        kind = event["kind"]
        if kind == "maintenance_started":
            phase = "maintenance"
        if kind == "provider_request" and payload.get("purpose") in ("narrative", "maintenance"):
            phase = payload["purpose"]
        if kind == "text":
            phase = "narrative"
            continue
        purpose = payload.get("purpose", phase if kind == "activity" else "runtime")
        if kind in ("memory_chunk", "memory_ready", "memory_failed", "memory_window_reset", "memory_retention",
                    "memory_tombstone_suppressed"):
            purpose = "memory"
        elif kind in ("state_proposal", "content_proposal", "maintenance_started", "maintenance_completed"):
            purpose = "maintenance"
        elif kind == "memory_proposal":
            purpose = "memory"
        elif kind in ("memory_ready", "memory_failed"):
            purpose = "memory"
        elif kind == "archive_started":
            purpose = payload.get("kind", "memory")
        elif kind == "scene_refresh_started":
            purpose = payload.get("kind", "runtime")
        if kind in ("provider_request", "usage", "activity") and purpose == "narrative":
            continue
        yield {**event, "purpose": purpose}


def compact_record(event):
    payload = event["payload"] if isinstance(event["payload"], dict) else {"message": str(event["payload"])}
    kind = event["kind"]
    label = LABELS.get(kind, kind)
    description = payload.get("message") or payload.get("reason") or ""
    if kind == "provider_request":
        description = f"{payload.get('model', '')} · {len(payload.get('messages', []))} 条输入 · {len(payload.get('tools', []))} 个工具"
    if kind == "activity":
        label = str(payload.get("kind") or label)
    return {"seq": event["seq"], "kind": kind, "label": label, "purpose": event["purpose"],
            "created": event["created"], "description": str(description)[:180],
            "bytes": len(json.dumps(payload, ensure_ascii=False).encode())}
