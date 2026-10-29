"""Safe, actionable validation feedback shared by model-facing tool routes."""

import copy
from typing import Any

from pydantic import BaseModel, ValidationError


def _resolve(schema, definitions, *, unwrap_nullable=False):
    if "$ref" in schema:
        schema = {
            **definitions[schema["$ref"].removeprefix("#/$defs/")],
            **{key: value for key, value in schema.items() if key != "$ref"},
        }
    variants = schema.get("anyOf", [])
    concrete = [variant for variant in variants if variant.get("type") != "null"]
    if unwrap_nullable and len(variants) == 2 and len(concrete) == 1:
        return _resolve(concrete[0], definitions, unwrap_nullable=True)
    return schema


def _parameter_schema(schema, definitions):
    # These tool schemas are acyclic. Inline only the selected structure so
    # feedback needs neither unrelated definitions nor dangling local refs.
    result = copy.deepcopy(_resolve(schema, definitions))
    result.pop("title", None)
    result.pop("$defs", None)
    if "discriminator" in result:
        result["discriminator"].pop("mapping", None)
    if "properties" in result:
        result["properties"] = {
            key: _parameter_schema(value, definitions) for key, value in result["properties"].items()
        }
    for key in ("items", "additionalProperties"):
        if isinstance(result.get(key), dict):
            result[key] = _parameter_schema(result[key], definitions)
    for key in ("anyOf", "oneOf", "allOf"):
        if key in result:
            result[key] = [_parameter_schema(value, definitions) for value in result[key]]
    return result


def _location(schema, location, definitions):
    path = ""
    for part in location:
        schema = _resolve(schema, definitions, unwrap_nullable=True)
        # Pydantic includes a discriminated union's tag in loc. It selects a
        # schema branch, but is not a property in the submitted JSON object.
        branches = schema.get("discriminator", {}).get("mapping", {})
        if part in branches:
            schema = {"$ref": branches[part]}
            continue
        if isinstance(part, int):
            path += f"[{part}]"
            schema = schema.get("items", {})
        else:
            path += ("." if path else "") + str(part)
            schema = schema.get("properties", {}).get(part, {})
    return path, _resolve(schema, definitions)


def _issue_text(issue, path):
    field = f"参数 {path} " if path else "参数"
    kind, context = issue["type"], issue.get("ctx", {})
    if kind == "missing":
        return f"缺少必填参数 {path}"
    if kind == "extra_forbidden":
        return f"不接受参数 {path}，请删除"
    types = {
        "string_type": "字符串",
        "int_type": "整数",
        "int_parsing": "整数",
        "float_type": "数字",
        "float_parsing": "数字",
        "bool_type": "布尔值（true/false）",
        "bool_parsing": "布尔值（true/false）",
        "list_type": "数组",
        "dict_type": "对象",
        "model_type": "对象",
        "model_attributes_type": "对象",
    }
    if kind in types:
        return f"{field}必须是{types[kind]}"
    bounds = {
        "greater_than": ("gt", "大于"),
        "greater_than_equal": ("ge", "大于或等于"),
        "less_than": ("lt", "小于"),
        "less_than_equal": ("le", "小于或等于"),
    }
    if kind in bounds:
        key, relation = bounds[kind]
        return f"{field}必须{relation} {context[key]}"
    if kind in ("string_too_short", "string_too_long", "too_short", "too_long"):
        short = kind.endswith("short")
        bound = context["min_length" if short else "max_length"]
        unit = "个字符" if kind.startswith("string_") else "项"
        actual = context.get("actual_length")
        count = f"，本次 {actual} {unit}" if actual is not None else ""
        return f"{field}{'至少需要' if short else '最多允许'} {bound} {unit}{count}"
    if kind == "literal_error":
        return f"{field}必须取允许值：{context['expected']}"
    if kind == "string_pattern_mismatch":
        return f"{field}须符合格式 {context['pattern']}"
    if kind == "union_tag_not_found":
        return f"{field}缺少操作判别字段 {context['discriminator']}，请按相关结构补充"
    if kind == "union_tag_invalid":
        return f"{field}的 {context['discriminator']} 必须取允许值：{context['expected_tags']}"
    if kind == "json_invalid":
        return "参数必须是合法的 JSON 对象"
    # Only explicitly controlled business errors may supply their own message;
    # arbitrary ValueError contexts can contain submitted text or storage IDs.
    if kind in ("blank_lore_entry", "empty_lore_changes", "invalid_memory_identity"):
        return f"{field}{issue['msg']}"
    return f"{field}不符合要求，请按参数定义修正"


def argument_feedback(model: type[BaseModel], error: ValidationError) -> dict[str, Any]:
    schema = model.model_json_schema()
    definitions = schema.get("$defs", {})
    messages, expected = [], {}
    for issue in error.errors(include_url=False, include_input=False):
        path, target = _location(schema, issue["loc"], definitions)
        messages.append(_issue_text(issue, path))
        kind = issue["type"]
        if kind in ("model_type", "model_attributes_type", "union_tag_not_found", "union_tag_invalid") or (
            kind == "missing" and target.get("type") == "object"
        ):
            expected[path or "$"] = _parameter_schema(target, definitions)
    result = {"message": "；".join(dict.fromkeys(messages))}
    if expected:
        result["expected_parameters"] = expected
    return result
