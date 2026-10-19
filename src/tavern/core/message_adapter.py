"""Application message identities and the final, reversible SDK projection."""
import copy
from dataclasses import dataclass, replace
from typing import Literal, TypedDict

from pydantic import BaseModel, ConfigDict, Field, model_validator

APIS = ("chat_completions", "responses", "anthropic", "gemini")
ROLES = ("system", "developer", "user", "assistant")


class RuntimeMessage(TypedDict, total=False):
    message_type: str
    runtime_content: str | list[dict]
    runtime_phase: Literal["header", "body"]
    runtime_order: int
    role: str  # Legacy framework bridge; SDK role is owned by the adapter.
    content: str | list[dict]
    source: str


@dataclass(frozen=True)
class MessageType:
    id: str
    label: str
    tag: str = ""
    configurable: bool = False
    appendable: bool = False
    role: str = "system"
    scopes: tuple = ("foreground", "full_agent", "background", "card_author", "advisor")


TYPES = {item.id: item for item in (
    MessageType("root_system", "主系统规则"),
    MessageType("kemini_filler", "Kemini 填充文本", role="user", scopes=("foreground", "full_agent")),
    MessageType("kemini_clear", "Kemini CLEAR", scopes=("foreground", "full_agent")),
    MessageType("kemini_identity", "Kemini 创作身份", role="user", scopes=("foreground", "full_agent")),
    MessageType("kemini_info", "Kemini 资料起始", scopes=("foreground", "full_agent")),
    MessageType("kemini_roleplay", "Kemini 角色扮演追加要求", scopes=("foreground", "full_agent")),
    MessageType("kemini_acknowledgement", "Kemini 助手确认", role="assistant", scopes=("foreground", "full_agent")),
    MessageType("kemini_control", "Kemini 输出要求", appendable=True, role="user", scopes=("foreground", "full_agent")),
    MessageType("editor_environment", "对话时间与系统环境", scopes=("card_author",)),
    MessageType("tool_protocol", "Full Agent 工具协议", scopes=("full_agent",)),
    MessageType("context_policy", "上下文窗口机制", scopes=("full_agent",)),
    MessageType("user", "用户输入", role="user"),
    MessageType("assistant", "模型回复", role="assistant"),
    MessageType("tool", "工具结果", role="tool"),
    MessageType("character", "合并角色资料", configurable=True),
    *[MessageType("card_" + field, label, "card_" + field, True) for field, label in (
        ("system_prompt", "角色系统要求"), ("description", "角色描述"),
        ("personality", "角色性格"), ("scenario", "角色场景"), ("mes_example", "对话示例"))],
    MessageType("persona", "用户角色资料", "user_character", True),
    MessageType("lore_constant", "常驻世界书", "lorebook", True),
    MessageType("lore_injected", "中途／可变世界书", "lorebook", True, True),
    MessageType("session_preference", "对话创作偏好", "tavern_preference", True),
    MessageType("retrieval_protocol", "检索输出协议", "tavern_output_protocol", True),
    MessageType("author_note", "作者注", "author_note", True, True),
    MessageType("post", "角色后置要求", "card_post_history_instructions", True, True),
    MessageType("memory", "分类记忆", "tavern_memory", True, True),
    MessageType("handoff", "交接笔记", "tavern_memory", True, True),
    MessageType("note_list", "长期笔记目录", "tavern_note_list", True, scopes=("full_agent",)),
    MessageType("plot", "剧情规划", "tavern_plot", True, True, scopes=("full_agent",)),
    MessageType("state", "剧情状态", "tavern_state", True, True),
    MessageType("mvu_rules", "MVU 规则快照", "mvu_rules", True, True),
    MessageType("tracker_mvu_rules", "Tracker MVU 规则", "mvu_rules", scopes=("background",)),
    MessageType("variable_mvu_rules", "独立变量分析规则", "mvu_rules", scopes=("background",)),
    MessageType("tracker_contract", "Tracker 操作契约", "tavern_tracker_contract", scopes=("background",)),
    MessageType("tracker_input", "Tracker 任务资料", "tavern_tracker_input", True, role="user", scopes=("background",)),
    MessageType("custom", "自定义注入", "tavern_custom_context", True, True),
    *[MessageType(name, label, "tavern_" + name, True, True) for name, label in (
        ("turn_instruction", "本轮额外要求"), ("regenerate_instruction", "重生成要求"),
        ("message_edit_notice", "原文编辑提醒"),
        ("turn_variable_instruction", "本轮变量要求"), ("context_warning", "资料过时提醒"),
        ("context_status", "上下文状态"), ("context_budget", "窗口预算提醒"),
        ("reply_correction", "回复纠正要求"), ("required_tool_notice", "必要工具提醒"),
        ("tracker_task_update", "Tracker 任务更新"))],
    *[MessageType("editor_" + name, label, "tavern_editor_" + name, True, True,
                  scopes=("card_author",)) for name, label in (
        ("overview", "草稿概览"), ("briefs", "确认资料"), ("handoff", "交接笔记"),
        ("persona_attachments", "引用用户角色"),
        ("files", "引用本地文件"),
        ("persona_changes", "待保存用户角色"), ("adapt", "适配要求"), ("pressure", "窗口压力"),
        ("final_ref", "已保存回复引用"))],
    *[MessageType("advisor_" + name, label, "tavern_advisor_" + name, True, True,
                  scopes=("advisor",)) for name, label in (
        ("reference", "参谋 agent 基础资料"), ("story", "参谋 agent 当前剧情与偏好"),
        ("summary", "参谋 agent 咨询摘要"), ("notice", "参谋 agent 请求与资料提醒"))],
)}
SCOPES = {
    **{name: ("foreground", "full_agent") for name in ("character", "persona", "lore_constant", "lore_injected",
       "session_preference", "post", "custom", "turn_instruction", "card_system_prompt", "card_description",
       "card_personality", "card_scenario", "card_mes_example")},
    **{name: ("foreground",) for name in ("retrieval_protocol", "author_note", "memory", "state")},
    **{name: ("full_agent",) for name in ("handoff", "mvu_rules", "regenerate_instruction",
       "turn_variable_instruction", "context_warning", "context_status", "context_budget")},
    "reply_correction": ("full_agent", "card_author"), "required_tool_notice": ("background",),
    "tracker_task_update": ("background",),
    "message_edit_notice": ("foreground", "full_agent", "background"),
}
TYPES = {name: replace(item, scopes=SCOPES.get(name, item.scopes)) for name, item in TYPES.items()}
SOURCES = {"rules": "root_system", "system": "root_system", "input": "user", "history": None,
           "system_prompt": "card_system_prompt", "description": "card_description",
           "personality": "card_personality", "scenario": "card_scenario", "examples": "card_mes_example",
           "char_description": "card_description", "char_personality": "card_personality",
           "lore_before": "lore_injected", "lore_after": "lore_injected", "inject": "custom"}


def type_definition(name):
    if name.startswith("compat:"):
        parts = name.split(":")
        if len(parts) != 3 or parts[1] not in TYPES or parts[2] not in ROLES:
            raise ValueError("未知兼容消息类型")
        base = TYPES[parts[1]]
        override = base.id in ("user", "assistant") and parts[2] != base.role
        return MessageType(name, base.label + " · 旧 " + parts[2], base.tag or ("tavern_custom_context" if override else ""), base.configurable or override,
                           base.appendable, parts[2], base.scopes)
    if name not in TYPES:
        raise ValueError("未知运行时消息类型")
    return TYPES[name]


class AdapterRule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["system", "developer", "user", "assistant"]
    wrap: bool


class MessageAdapterConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal[1] = 1
    mappings: dict[str, dict[str, AdapterRule]] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def remove_call_notifications(cls, value):
        if isinstance(value, dict) and isinstance(value.get("mappings"), dict):
            value = copy.deepcopy(value)
            for api, rules in value["mappings"].items():
                if isinstance(rules, dict):
                    value["mappings"][api] = {
                        name: rule for name, rule in rules.items()
                        if not isinstance(name, str) or (name not in ("call_budget", "last_call_notice")
                        and not name.startswith(("compat:call_budget:", "compat:last_call_notice:")))}
        return value

    @model_validator(mode="after")
    def editable_types(self):
        for api, rules in self.mappings.items():
            if api not in APIS:
                raise ValueError("未知消息适配协议")
            for name in rules:
                if not type_definition(name).configurable:
                    raise ValueError("该消息类型不允许自定义映射或标签")
        return self


def default_rule(name, api, lane="foreground"):
    definition = type_definition(name)
    role = definition.role
    if name == "card_mes_example" and lane == "foreground":
        role = "assistant"
    # Explicit conversation/task identities must survive configurable defaults.
    # Otherwise Tracker's sole user input becomes system-only, leaving relays
    # with no Responses input / Gemini contents after extracting instructions.
    task_role = definition.role in ("user", "assistant") and not name.startswith("compat:")
    if definition.configurable and not task_role:
        role = "user" if api == "gemini" else "system"
    elif role == "developer" and api in ("anthropic", "gemini"):
        role = "system"
    base = name.split(":")[1] if name.startswith("compat:") else name
    wrapped = {"author_note", "post", "memory", "handoff", "note_list", "plot", "state", "mvu_rules", "lore_injected",
               "persona", "lore_constant", "session_preference", "retrieval_protocol"}
    wrap = bool(definition.tag) and (not definition.configurable or base in wrapped or base.startswith("card_")
                                    or api in ("anthropic", "gemini") and definition.role in ("system", "developer"))
    return {"role": role, "wrap": wrap}


def rule_for(name, config, api, lane="foreground"):
    raw = config.model_dump() if isinstance(config, MessageAdapterConfig) else config or {}
    rule = raw.get("mappings", {}).get(api, {}).get(name)
    return copy.deepcopy(rule) if rule is not None else default_rule(name, api, lane)


def catalog(lane="foreground", extra=()):
    names = [name for name, item in TYPES.items() if lane in item.scopes]
    # Runtime sources (worldbook entries and script injections included) can
    # retain any supported source role. Publish the same finite compatibility
    # vocabulary accepted by type_definition, rather than only recipe sightings.
    compatible = [f"compat:{name}:{role}" for name in names for role in ROLES
                  if type_definition(f"compat:{name}:{role}").configurable]
    names = list(dict.fromkeys([*names, *compatible, *extra]))
    return [{"id": name, "label": type_definition(name).label, "tag": type_definition(name).tag,
             "configurable": type_definition(name).configurable, "appendable": type_definition(name).appendable,
             "defaults": {api: default_rule(name, api, lane) for api in APIS}} for name in names]


def infer_type(message, lane="foreground"):
    if message.get("message_type"):
        type_definition(message["message_type"])
        return message["message_type"]
    source = message.get("context_source", message.get("source"))
    if not source and message.get("role") in ("user", "assistant", "tool"):
        return message["role"]
    if source in ("agent_tools", "maintenance_tools") and message.get("role") in ("system", "developer"):
        return "reply_correction"
    if source == "input":
        return "user" if message.get("role") == "user" else f"compat:user:{message['role']}"
    if source in ("history", "agent_tools", "maintenance_tools") or message.get("role") == "tool":
        return message["role"] if message["role"] in ("user", "assistant", "tool") else "custom"
    if source and (source.startswith("lore:") or source.startswith("outlet:")):
        base = "lore_constant" if message.get("lore_region") == "constant" else "lore_injected"
    else:
        base = SOURCES.get(source, source if source in TYPES else "custom")
    expected = "assistant" if base == "card_mes_example" and lane == "foreground" else TYPES[base].role
    role = message.get("role", expected)
    return base if role == expected else f"compat:{base}:{role}"


def unwrapped(content, tag):
    opening, closing = f"<{tag}>\n", f"\n</{tag}>"
    if tag and isinstance(content, str) and content.startswith(opening) and content.endswith(closing):
        return content[len(opening):-len(closing)]
    return content


def canonical_content(message, definition):
    if "runtime_content" in message:
        return message["runtime_content"]
    base = definition.id.split(":")[1] if definition.id.startswith("compat:") else definition.id
    compiled = {"persona", "lore_constant", "lore_injected", "author_note", "post", "memory", "handoff", "note_list",
                "plot", "state", "mvu_rules", "tracker_mvu_rules", "variable_mvu_rules"}
    return (unwrapped(message.get("content", ""), definition.tag)
            if base.startswith("card_") or base in compiled else message.get("content", ""))


def wrap_content(content, tag):
    if not tag:
        return content
    if isinstance(content, list):
        # Tags belong to text, never to a stringified image payload. Keep every
        # original part in order and leave the canonical source untouched.
        return [{"type": "text", "text": f"<{tag}>\n"}, *copy.deepcopy(content),
                {"type": "text", "text": f"\n</{tag}>"}]
    return f"<{tag}>\n{content}\n</{tag}>"


def runtime_messages(messages, lane="foreground"):
    result, header = [], True
    for index, message in enumerate(messages):
        item = copy.deepcopy(message)
        name = infer_type(item, lane)
        definition = type_definition(name)
        if name in ("user", "assistant", "tool") or item.get("context_source", item.get("source")) in ("history", "input") or item.get("_cache_static") is False or item.get("runtime_phase") == "body":
            header = False
        item.setdefault("message_type", name)
        item.setdefault("runtime_phase", "header" if header else "body")
        item.setdefault("runtime_content", canonical_content(item, definition))
        item.setdefault("runtime_order", index)
        result.append(item)
    return result


def adapter_snapshot(settings):
    loader = getattr(settings, "_adapter_loader", None)
    if loader:
        return loader(getattr(settings, "_adapter_lane", "foreground"))
    config = getattr(settings, "message_adapter", MessageAdapterConfig())
    return {"config": config.model_dump() if hasattr(config, "model_dump") else config, "revision": 0}


def bind_adapter(settings, store, owner_kind, owner_id, lane=None):
    settings._adapter_loader = lambda current: store.message_adapter(owner_kind, owner_id, current)
    if lane:
        settings._adapter_lane = lane
    return settings


def adapt_messages(messages, settings, snapshot=None, *, managed=None):
    """A pure projection: canonical bodies and native attachments are never rewritten."""
    snapshot = snapshot or adapter_snapshot(settings)
    lane = getattr(settings, "_adapter_lane", "foreground")
    if settings.runtime_mode == "full_agent" and lane in ("foreground", "background"):
        lane = "full_agent"
    api = settings.api_mode
    config = snapshot["config"]
    managed = lane != "foreground" or settings.runtime_mode == "full_agent" if managed is None else managed
    pairs, diagnostics = [], []
    for original in messages:
        item = copy.deepcopy(original)
        item.pop("_kemini_compat", None)
        name = item.get("message_type")
        if not name:  # Standalone protocol probes and imported SDK messages retain their identity.
            pairs.append((original, item))
            continue
        definition = type_definition(name)
        rule = rule_for(name, config, api, lane)
        body = canonical_content(item, definition)
        native = item.get("_native_response")
        if name == "assistant" and native:
            body = ("".join(p.get("text", "") for p in native["items"] if p.get("type") == "text")
                    if native.get("origin", {}).get("api") == "anthropic" else
                    "".join(p.get("text", "") for entry in native["items"] if entry.get("type") == "message"
                            for p in entry.get("content", [])))
        item["role"] = rule["role"]
        item["content"] = wrap_content(body, definition.tag) if rule["wrap"] else body
        if item.get("runtime_parts"):
            item["content"] = "\n".join(f"<{definition.tag}>\n{part}\n</{definition.tag}>" if rule["wrap"] else part
                                        for part in item["runtime_parts"])
        item["_adapter_typed"] = True
        item["sdk_role"], item["label_enabled"] = rule["role"], rule["wrap"]
        pairs.append((original, item))
    boundary = 0
    while boundary < len(pairs) and pairs[boundary][0].get("runtime_phase") == "header":
        boundary += 1
    def rank(pair):
        item = pair[1]
        if item["role"] not in ("system", "developer"):
            return 2
        return int(type_definition(item["message_type"]).appendable)
    kemini = (settings.kemini.enabled and lane in ("foreground", "full_agent")
              and any(item.get("message_type", "").startswith("kemini_") for _, item in pairs))
    if kemini:
        # Match strict_tools' chronological projection without merging distinct
        # source blocks or disrupting native tool-call/result adjacency.
        leading, converted = True, 0
        for _, item in pairs:
            if item["role"] not in ("system", "developer"):
                leading = False
            elif item["role"] == "system" and not leading:
                item["role"] = item["sdk_role"] = "user"
                converted += 1
            item["_kemini_compat"] = True
        diagnostics.append(f"Kemini 式实验功能：保留消息顺序，本次 {converted} 条中途 system 消息按 user 发送；"
                           "开头连续的 system 区域与工具交换保留。此规则来自全局设置 → 杂项。")
    elif managed:
        pairs[:boundary] = sorted(pairs[:boundary], key=lambda pair: (rank(pair), pair[0].get("runtime_order", 0)))
    else:
        # Stable partition is the minimum crossing removal needed for a single
        # system prefix. Do not re-sort by original runtime_order: the canonical
        # result is replayed, so projecting it again must leave the prefix intact.
        header = pairs[:boundary]
        ordered = sorted(header, key=lambda pair: pair[1]["role"] not in ("system", "developer"))
        if ordered != header:
            pairs[:boundary] = ordered
            diagnostics.append("已按最小位移保持头部 system 连续：非系统资料后移，各组相对顺序与正文位置保持不变。"
                               "包括 Google GenAI 在内的部分 API 明确不支持中途 system 消息。")
    return [item for _, item in pairs], [copy.deepcopy(raw) for raw, _ in pairs], list(dict.fromkeys(diagnostics))


WIRE_FIELDS = {"role", "content", "tool_calls", "tool_call_id", "name", "reasoning_content",
               "reasoning_details", "reasoning", "_native_response", "_response_origin", "_response_digest", "_tool_error"}


def project_context(context, settings, *, tools=None, reasoning_cost=0, managed=None):
    from tavern.core.tokens import count_text, message_tokens, request_tokens
    if tools is None:
        tools = context.get("tools")
    rows = context.get("runtime_trace", context["trace"])
    canonical = runtime_messages(rows, getattr(settings, "_adapter_lane", "foreground"))
    snapshot = adapter_snapshot(settings)
    adapted, ordered, warnings = adapt_messages(canonical, settings, snapshot, managed=managed)
    context["runtime_trace"] = ordered
    context["runtime_messages"] = [{k: v for k, v in row.items() if k in WIRE_FIELDS or k in
        {"message_type", "runtime_content", "runtime_phase", "runtime_order", "runtime_parts", "title"}} for row in ordered]
    context["messages"] = [{k: v for k, v in row.items() if k in WIRE_FIELDS} for row in adapted]
    context["trace"] = [{**row, "position": i, "estimated_tokens": message_tokens(row, settings),
                         "content_tokens": count_text(row.get("content", ""), settings)} for i, row in enumerate(adapted)]
    for row, raw in zip(context["trace"], ordered, strict=True):
        if raw.get("native_replay"):
            row["content"] = raw["content"]
    context["estimated_tokens"] = request_tokens(context["messages"], settings, tools) + reasoning_cost
    if "tokens" in context:
        context["tokens"] = context["estimated_tokens"]
    context["budget_sources"] = {source: sum(row["estimated_tokens"] for row in context["trace"]
        if row.get("source") == source) for source in dict.fromkeys(row.get("source") for row in context["trace"])}
    tool_cost = request_tokens([], settings, tools) - 3 if tools else 0
    if tools:
        context["budget_sources"]["tool_definitions"] = tool_cost
    context["budget_sources"]["chat_framing"] = context["estimated_tokens"] - tool_cost - sum(
        row["estimated_tokens"] for row in context["trace"])
    context["adapter_revision"] = snapshot["revision"]
    context.setdefault("diagnostics", []).extend({"reason": warning} for warning in warnings)
    return context


def provider_messages(provider, messages, settings):
    # ModelTransport owns the real SDK boundary. Test/injected providers are that boundary themselves.
    from tavern.scripting.model_transport import ModelTransport
    if isinstance(provider, ModelTransport):
        return messages
    projected, _, _ = adapt_messages(messages, settings)
    return [{k: v for k, v in item.items() if k in WIRE_FIELDS or k.startswith("_cache_")}
            for item in projected]
