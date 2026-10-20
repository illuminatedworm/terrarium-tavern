"""Tavern Helper variable macros for model-bound text, after state projection."""

import ast
import html
import json
import re

import yaml

from tavern.core.model_view import state_for_model

MACRO = re.compile(
    r"\{\{(get|format)_(message|chat|character|preset|global)_variable(_quoted)?::(.*?)\}\}", re.I
)
PATH = re.compile(r"""(?:^|\.)([^.\[\]]+)|\[\s*((?:"(?:\\.|[^"\\])*")|(?:'(?:\\.|[^'\\])*')|\d+)\s*\]""")


def public_value(value):
    if isinstance(value, dict):
        return {key: public_value(item) for key, item in value.items() if not key.startswith("$")}
    if isinstance(value, list):
        return [public_value(item) for item in value]
    return value


def variable_path(value, path):
    path = html.unescape(path)
    cursor = 0
    if not path:
        return None
    for match in PATH.finditer(path):
        if match.start() != cursor:
            return None
        key = match[1] if match[1] is not None else match[2]
        if match[1] is None and key[:1] in {'"', "'"}:
            try:
                key = ast.literal_eval(key)
            except (ValueError, SyntaxError):
                return None
        if key.startswith("$") or key in {"__proto__", "prototype", "constructor"}:
            return None
        if isinstance(value, dict):
            value = value.get(key)
        elif isinstance(value, list) and key.isdigit() and int(key) < len(value):
            value = value[int(key)]
        else:
            return None
        cursor = match.end()
    return public_value(value) if cursor == len(path) else None


class MacroDumper(yaml.SafeDumper):
    quoted = False

    def increase_indent(self, flow=False, indentless=False):
        return super().increase_indent(flow, False)

    def represent_str(self, value):
        return self.represent_scalar(
            "tag:yaml.org,2002:str", value, style='"' if self.quoted else "|" if "\n" in value else None
        )


MacroDumper.add_representer(str, MacroDumper.represent_str)


class QuotedMacroDumper(MacroDumper):
    quoted = True

    def represent_mapping(self, tag, mapping, flow_style=None):
        return super().represent_mapping(
            tag, [(PlainKey(key), value) for key, value in mapping.items()], flow_style
        )


class PlainKey(str):
    pass


QuotedMacroDumper.add_representer(PlainKey, yaml.SafeDumper.represent_str)


def replace_variable_macros(text, state, scopes=None):
    if not MACRO.search(text):
        return text
    state = state_for_model(state)
    scopes = scopes or {}
    output, cursor = "", 0
    for match in MACRO.finditer(text):
        output += text[cursor : match.start()]
        scope = match[2].lower()
        source = state if scope in {"chat", "message"} else state_for_model(scopes.get(scope, {}))
        value = variable_path(source, match[4])
        formatted = match[1].lower() == "format"
        if isinstance(value, str):
            rendered = value
        elif formatted:
            rendered = yaml.dump(
                value,
                Dumper=QuotedMacroDumper if match[3] else MacroDumper,
                allow_unicode=True,
                sort_keys=False,
                width=float("inf"),
            ).rstrip()
            rendered = rendered.removesuffix("\n...")
        else:
            rendered = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        if formatted:
            prefix_width = len(output.rsplit("\n", 1)[-1].encode("utf-16-le")) // 2
            rendered = rendered.replace("\n", "\n" + " " * prefix_width)
        output += rendered
        cursor = match.end()
    return output + text[cursor:]
