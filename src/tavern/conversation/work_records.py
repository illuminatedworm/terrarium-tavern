"""Compact persisted background records; large payloads are loaded separately."""

import json

LABELS = {
    "tracker_started": "Tracker 任务开始",
    "tracker_completed": "Tracker 任务完成",
    "tracker_tool": "Tracker 工具",
    "tracker_failed": "Tracker 任务失败",
    "provider_response": "模型原生响应",
    "full_agent_response": "模型响应",
    "full_agent_assistant": "Agent 中间回复",
    "full_agent_tool_started": "工具调用开始",
    "full_agent_tool": "Full Agent 工具调用",
    "context_rollover": "工作窗口切换",
    "context_warning": "上下文超长提示",
    "context_baseline": "上下文基线重建",
    "context_replay": "上下文分批整理",
    "context_compacted": "云端上下文已压缩",
    "memory_history_recall": "Tracker 历史补查",
    "memory_history_limit": "历史补查达到3次上限",
    "memory_history_budget": "历史补查预算不足",
    "memory_batch": "记忆整理批次与来源",
    "memory_batch_completed": "记忆批次返回",
    "memory_review_started": "疑难复核开始",
    "memory_review_result": "疑难复核结果",
    "memory_rebuild_estimate": "重建批次估算",
    "memory_records_discarded": "可选原话记录已丢弃",
    "memory_tombstone_suppressed": "手工删除内容已屏蔽",
    "memory_retention": "记忆条目准出",
    "memory_recall_filter": "归档召回筛选",
    "variable_rules": "MVU 规则注入",
    "variable_update_started": "变量分析开始",
    "variable_update_completed": "变量更新完成",
    "variable_update_failed": "变量更新失败",
    "started": "回合开始",
    "maintenance_started": "设定维护开始",
    "scene_refresh_started": "手动更新开始",
    "memory_chunk": "滚动窗口推进",
    "memory_window_reset": "滚动窗口重置",
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
    "scene_refresh_ready": "手动更新结果",
    "maintenance_completed": "设定维护记录",
    "memory_proposal": "记忆写入提案",
    "archive_started": "自动档案整理开始",
    "memory_ready": "记忆整理完成",
    "memory_failed": "记忆整理失败",
}


def background_records(events):
    phase = "narrative"
    for event in events:
        payload = (
            event["payload"] if isinstance(event["payload"], dict) else {"message": str(event["payload"])}
        )
        kind = event["kind"]
        if kind == "maintenance_started":
            phase = "maintenance"
        if kind == "provider_request" and payload.get("purpose") in ("narrative", "maintenance"):
            phase = payload["purpose"]
        if kind in ("text", "text_reset"):
            phase = "narrative"
            continue
        purpose = payload.get("purpose", phase if kind == "activity" else "runtime")
        if purpose.startswith("tracker_"):
            pass  # Keep task phase alongside the tool/event's semantic category.
        elif kind in (
            "memory_chunk",
            "memory_ready",
            "memory_failed",
            "memory_window_reset",
            "memory_retention",
            "memory_tombstone_suppressed",
        ):
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
        if kind in ("provider_request", "provider_response", "usage", "activity") and purpose == "narrative":
            continue
        yield {**event, "purpose": purpose}


def compact_record(event):
    payload = event["payload"] if isinstance(event["payload"], dict) else {"message": str(event["payload"])}
    kind = event["kind"]
    label = LABELS.get(kind, kind)
    description = payload.get("message") or payload.get("reason") or ""
    if kind == "tracker_tool":
        label = {
            "write_memory": "文本记忆写入",
            "revise_content": "对话级角色卡修改",
            "patch_state": "MVU 补丁暂存",
        }.get(payload.get("name"), payload.get("name", label))
        result = payload.get("result")
        if isinstance(result, dict):
            description = result.get("error") or result.get("notice", "")
    if kind == "provider_request":
        description = f"{payload.get('model', '')} · {len(payload.get('messages', []))} 条输入 · {len(payload.get('tools', []))} 个工具"
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
