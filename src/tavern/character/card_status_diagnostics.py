"""Conservative hints for model-written status displays; never rewrite card content."""

import re

from tavern.character.cards import lore_entries, normalize, variable_rule_entries

MACRO = re.compile(r"\{\{\s*get(?:global)?var::([^{}]+)}}", re.I)
HTML = re.compile(r"<(?:details|div|span|table|section|style|script|html|status)\b", re.I)
STATUS = re.compile(r"状态栏|状态面板|生命体征|status[ _-]*(?:bar|panel|block)|<status\b", re.I)
METRICS = re.compile(r"\b(?:HP|TIME|ENV|MONEY|EDDIES)\b|生命值|健康值|余额|收入|当前时间|环境状态", re.I)
REPEAT = re.compile(
    r"每(?:次|轮|个).{0,12}(?:回复|回合|回答)|(?:回复|回答).{0,8}(?:末尾|结尾)"
    r"|(?:each|every)\s+(?:reply|response|turn)|(?:end of|after)\s+(?:each\s+)?(?:reply|response)",
    re.I,
)
OUTPUT = re.compile(
    r"生成|输出|附加|附上|添加|展示|显示|更新|填写|保持|使用|include|append|output|generate|display|render|update",
    re.I,
)
NEGATION = re.compile(
    r"不要|不得|禁止|无需|不必|不能|不再|避免|移除|删除|do\s+not|don't|never|without|no\s+need", re.I
)
TABLE = re.compile(r"^\s*\|?.*\|.*\n\s*\|?\s*:?-{3,}", re.M)
BINDING = re.compile(r"\b(?:getMvuData|getAllVariables)\s*\(|StatusPlaceHolder", re.I)
INIT = re.compile(r"<initvar\b", re.I)


def repeats_display(text):
    # Negation is local to an instruction, not the entire field. This avoids
    # treating a migration prohibition as a request, without masking a later demand.
    return any(
        REPEAT.search(clause) and OUTPUT.search(clause) and not NEGATION.search(clause)
        for clause in re.split(r"[。！？!?\n;；]", text)
    )


def status_display_warnings(document):
    data = normalize(document)
    entries = lore_entries(document)
    openings = [data.get("first_mes", ""), *data.get("alternate_greetings", [])]
    fields = [
        (key, data.get(key, ""), None)
        for key in (
            "system_prompt",
            "description",
            "personality",
            "scenario",
            "mes_example",
            "post_history_instructions",
        )
    ]
    fields.extend(
        ("first_mes" if i == 0 else f"alternate_greetings[{i - 1}]", text, i)
        for i, text in enumerate(openings)
    )
    # Ordinal locations do not copy opaque worldbook IDs or source excerpts into
    # model-facing diagnostics. The editor locates the full entry with read tools.
    fields.extend(
        (f"世界书第 {i + 1} 条", e.get("content", ""), None)
        for i, e in enumerate(entries)
        if e.get("enabled", True) and "[initvar]" not in e.get("comment", "").lower()
    )
    base_init = any(
        "[initvar]" in str(e.get("comment") or "").lower() for e in entries if e.get("content", "").strip()
    )
    initialized = [base_init or bool(INIT.search(text)) for text in openings]
    rules = any(e.get("content", "").strip() for e in variable_rule_entries(document))
    warnings, candidates = [], []
    for location, text, opening in fields:
        macros = MACRO.findall(text)
        markup = bool(HTML.search(text) or TABLE.search(text))
        formatted = markup or bool(STATUS.search(text))
        repeated = formatted and repeats_display(text)
        # Ignore styling and script source when looking for visible sample values.
        visible = re.sub(r"<(style|script)\b[^>]*>[\s\S]*?</\1\s*>", " ", text, flags=re.I)
        visible = re.sub(r"<[^>]*>", " ", visible)
        static_panel = (
            (opening is not None or location == "mes_example")
            and markup
            and (
                STATUS.search(visible)
                or re.search(r"<status\b", text, re.I)
                or len(set(m.upper() for m in METRICS.findall(visible))) >= 2
            )
            and not BINDING.search(text)
            and not any(p.strip().startswith("stat_data.") for p in macros)
        )
        if repeated:
            warnings.append(
                f"{location}：可能要求模型逐轮生成格式化状态栏或界面；请读取全文，分离叙事、变量更新与展示（references/status-display.md）"
            )
        elif static_panel:
            warnings.append(
                f"{location}：可能含静态状态栏示范；迁移正文状态栏时需保留其事实并处理开场/示例，普通 HTML 排版无需迁移（references/status-display.md）"
            )
        if macros and any(not p.strip().startswith("stat_data.") for p in macros):
            warnings.append(
                f"{location}：getvar/getglobalvar 宏按状态根路径读取，不会自动绑定 stat_data；请核对来源与路径，宏本身不初始化或更新变量"
            )
        if repeated or static_panel or (macros and formatted):
            candidates.append((location, opening))
    if candidates:
        missing = [
            location
            for location, opening in candidates
            if not (all(initialized) if opening is None else initialized[opening])
        ]
        if missing:
            warnings.append(
                "、".join(missing)
                + "：未发现覆盖相关开场的声明式初始化来源；如依赖脚本需完整读取并运行验证，不能凭展示值认定变量已初始化"
            )
        if not rules:
            warnings.append(
                "状态展示未发现有效且非空的 tracker_mvu 更新规则；请确认是否需要持续追踪，静态说明不必创建变量"
            )
    return warnings
