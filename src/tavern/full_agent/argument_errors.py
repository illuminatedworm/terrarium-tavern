"""Actionable tool argument feedback without validator internals or input echoes."""
import copy

from tavern.full_agent.protocol import ToolError


def parameter_schema(schema):
    result = copy.deepcopy(schema)
    result.pop("title", None)
    for group in ("properties", "$defs"):
        if group in result:
            result[group] = {key: parameter_schema(value) for key, value in result[group].items()}
    for key in ("items", "additionalProperties"):
        if isinstance(result.get(key), dict):
            result[key] = parameter_schema(result[key])
    for key in ("anyOf", "oneOf", "allOf"):
        if key in result:
            result[key] = [parameter_schema(value) for value in result[key]]
    return result


def issue_text(issue):
    path = ""
    for part in issue["loc"]:
        path += f"[{part}]" if isinstance(part, int) else ("." if path else "") + str(part)
    field = f"参数 {path} " if path else "参数"
    kind, context = issue["type"], issue.get("ctx", {})
    if kind == "missing":
        return f"缺少必填参数 {path}"
    if kind == "extra_forbidden":
        return f"不接受参数 {path}，请删除"
    types = {"string_type": "字符串", "int_type": "整数", "int_parsing": "整数",
             "float_type": "数字", "float_parsing": "数字", "bool_type": "布尔值（true/false）",
             "bool_parsing": "布尔值（true/false）", "list_type": "数组", "dict_type": "对象",
             "model_type": "对象", "model_attributes_type": "对象"}
    if kind in types:
        return f"{field}必须是{types[kind]}"
    bounds = {"greater_than": ("gt", "大于"), "greater_than_equal": ("ge", "大于或等于"),
              "less_than": ("lt", "小于"), "less_than_equal": ("le", "小于或等于")}
    if kind in bounds:
        key, relation = bounds[kind]
        return f"{field}必须{relation} {context[key]}"
    if kind in ("string_too_short", "string_too_long", "too_short", "too_long"):
        short = kind.endswith("short")
        bound = context["min_length" if short else "max_length"]
        unit = "个字符" if kind.startswith("string_") else "项"
        return f"{field}{'至少需要' if short else '最多允许'} {bound} {unit}"
    if kind == "literal_error":
        return f"{field}必须取允许值：{context['expected']}"
    if kind == "string_pattern_mismatch":
        return f"{field}格式不符，请遵循参数定义中的 pattern"
    if kind == "json_invalid":
        return "参数必须是合法的 JSON 对象"
    return f"{field}不符合要求，请按参数定义修正"


class ToolArgumentError(ToolError):
    def __init__(self, name, model, error):
        issues = error.errors(include_url=False, include_input=False)
        message = f"{name} 参数错误，工具未执行：" + "；".join(issue_text(issue) for issue in issues)
        message += "。请按 expected_parameters 修正后重新调用。"
        super().__init__("INVALID_ARGUMENTS", message)
        self.result["expected_parameters"] = parameter_schema(model.model_json_schema())
