"""Report the regex fields consumed by Runtime without rewriting imported cards."""

from tavern.character.cards import normalize


def regex_warnings(document):
    rules = normalize(document).get("extensions", {}).get("regex_scripts", [])
    if not isinstance(rules, list):
        return ["extensions.regex_scripts 必须是正则规则数组（references/regex-display.md）"]
    warnings = []
    for index, rule in enumerate(rules):
        location = f"正则第 {index + 1} 条"
        if not isinstance(rule, dict):
            warnings.append(location + "：必须为规则对象")
            continue
        for field in ("findRegex", "replaceString"):
            if not isinstance(rule.get(field), str):
                warnings.append(location + f"：缺少文本字段 {field}；运行时不会读取其他别名")
        if not isinstance(rule.get("placement", []), list) or any(
            type(p) is not int for p in rule.get("placement", [])
        ):
            warnings.append(location + "：placement 必须为数字数组；1 为用户输入，2 为助手输出")
        for field in ("disabled", "markdownOnly", "promptOnly"):
            if field in rule and not isinstance(rule[field], bool):
                warnings.append(location + f"：{field} 必须为布尔值")
        replacement = rule.get("replaceString", "")
        if isinstance(replacement, str) and "<script" in replacement.lower() and not rule.get("disabled"):
            if not rule.get("markdownOnly") or rule.get("promptOnly"):
                warnings.append(
                    location + "：HTML 脚本可能进入模型文本；展示规则应 markdownOnly=true、promptOnly=false"
                )
            if isinstance(rule.get("placement"), list) and rule["placement"] and 2 not in rule["placement"]:
                warnings.append(location + "：展示规则未覆盖助手输出 placement=2")
    return warnings
