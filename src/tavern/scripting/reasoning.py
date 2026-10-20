"""Protocol thinking capabilities shared by settings and request compilation."""

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class AnthropicThinking:
    mode: str = "adaptive"
    xhigh: bool = True
    sampling: bool = False
    can_disable: bool = True
    forced_tools: bool = True


# Only documented models are matched; dated/deployment aliases may add suffixes.
# Claude requests never synthesize manual thinking budgets.
ANTHROPIC_THINKING = {
    "opus-5-5": AnthropicThinking(can_disable=False, forced_tools=False),
    "sonnet-5-5": AnthropicThinking(can_disable=False, forced_tools=False),
    "opus-5": AnthropicThinking(),
    "sonnet-5": AnthropicThinking(),
    "opus-4-8": AnthropicThinking(),
    "opus-4-7": AnthropicThinking(),
    "opus-4-6": AnthropicThinking(xhigh=False, sampling=True),
    "sonnet-4-6": AnthropicThinking(xhigh=False, sampling=True),
    "opus-4-5": AnthropicThinking(mode="effort", xhigh=False, sampling=True),
    "sonnet-4-5": AnthropicThinking(mode="unsupported", xhigh=False, sampling=True),
    "haiku-4-5": AnthropicThinking(mode="unsupported", xhigh=False, sampling=True),
    "opus-4-1": AnthropicThinking(mode="unsupported", xhigh=False, sampling=True),
    "opus-4": AnthropicThinking(mode="unsupported", xhigh=False, sampling=True),
    "sonnet-4": AnthropicThinking(mode="unsupported", xhigh=False, sampling=True),
    "fable-5-1": AnthropicThinking(can_disable=False, forced_tools=False),
    "mythos-5-1": AnthropicThinking(can_disable=False, forced_tools=False),
    "fable-5": AnthropicThinking(can_disable=False),
    "mythos-5": AnthropicThinking(can_disable=False),
    "mythos-preview": AnthropicThinking(xhigh=False, can_disable=False),
}


def anthropic_policy(model):
    name = model.lower().replace(".", "-")
    return next(
        (
            p
            for key, p in ANTHROPIC_THINKING.items()
            if re.search(r"claude-" + re.escape(key) + r"(?=$|:|-(?:[a-z]|\d{8}(?:$|[-:])))", name)
        ),
        AnthropicThinking(mode="effort", sampling=True),
    )


GEMINI_BUDGETS = {"none": 0, "minimal": 128, "low": 1024, "medium": 4096, "high": 8192, "xhigh": 16384}
OPENAI_EFFORTS = ["none", "minimal", "low", "medium", "high", "xhigh", "max"]


def gemini_levels(model, *, native=True):
    name = model.lower()
    if re.search(r"(?:^|/)gemini-(?:[4-9]|[1-9]\d)(?=$|[.-])", name):
        return ["low", "medium", "high"]
    if re.search(r"gemini-3(?=$|[.-])", name):
        if re.search(r"gemini-3\.1-flash-lite-image(?=$|[-:])", name):
            return ["minimal", "high"]
        if re.search(r"gemini-3-pro(?=$|[-:])", name):
            return ["low", "high"]
        if "pro" in name or re.search(r"gemini-3\.[78]-flash(?=$|[-:])", name):
            return ["low", "medium", "high"]
        return ["minimal", "low", "medium", "high"]
    levels = list(GEMINI_BUDGETS) if native else ["none", "low", "medium", "high"]
    if re.search(r"gemini-2\.5-pro(?=$|[-:])", name):
        levels.remove("none")
    if native and re.search(r"gemini-2\.5-flash-lite(?=$|[-:])", name):
        levels.remove("minimal")
    return levels


def reasoning_capabilities(api, model):
    """Local presets, not a claim about arbitrary relay model capabilities."""
    descriptions = {}
    if api == "anthropic":
        policy = anthropic_policy(model)
        levels = ["none"] if policy.can_disable else []
        if policy.mode != "unsupported":
            levels += ["low", "medium", "high"]
            if policy.mode == "adaptive":
                levels += (["xhigh"] if policy.xhigh else []) + ["max"]
        note = (
            "此模型不支持文字思考档位；默认不发送思考参数。"
            if policy.mode == "unsupported"
            else "思考档位按 Anthropic Messages 及当前模型匹配；默认不发送思考参数。"
        )
    elif api == "gemini" or (api == "chat_completions" and "gemini" in model.lower()):
        levels = gemini_levels(model, native=api == "gemini")
        if api == "gemini" and not _gemini_thinking_level(model):
            descriptions = {effort: f"{GEMINI_BUDGETS[effort]} 思考 tokens" for effort in levels}
            note = "此接口使用思考 token 预算，各档位显示对应预算；默认不发送思考参数。"
        else:
            note = "思考档位按 Gemini 模型支持的级别匹配；默认不发送思考参数。"
    else:
        levels = OPENAI_EFFORTS
        note = "思考档位按当前接口提供，具体模型支持范围由服务商决定；默认不发送思考参数。"
    return {
        "id": model,
        "display_name": model,
        "supported_reasoning_levels": [
            {"effort": effort, "description": descriptions.get(effort, "")} for effort in levels
        ],
        "note": note,
    }


def anthropic_thinking(model, effort):
    policy = anthropic_policy(model)
    if effort == "default":
        return policy, {}
    if effort == "none":
        if not policy.can_disable:
            raise ValueError(f"{model} 不支持完全关闭思考；请选择默认或其他思考档位")
        return policy, {"thinking": {"type": "disabled"}}
    if policy.mode == "unsupported":
        raise ValueError(f"{model} 不支持文字思考档位；请选择默认或更换模型，应用不再生成旧式思考预算")
    selected = "low" if effort == "minimal" else effort
    if selected == "xhigh" and not policy.xhigh:
        selected = "max" if policy.mode == "adaptive" else "high"
    if policy.mode == "adaptive":
        return policy, {"thinking": {"type": "adaptive"}, "output_config": {"effort": selected}}
    # Opus 4.5 supports effort without extended thinking. Unknown relay aliases
    # receive the standard effort field; the provider decides their capability.
    return policy, {"output_config": {"effort": selected}}


def gemini_effort(model, effort):
    name = model.lower()
    modern = _gemini_thinking_level(model)
    if effort == "none" and (modern or re.search(r"gemini-2\.5-pro(?=$|-)", name)):
        raise ValueError(f"{model} 不支持关闭思考；请选择默认或其他思考档位")
    if modern:
        if effort == "xhigh":
            return "high"
        if effort == "minimal" and "pro" in name:
            return "low"
        if effort not in ["default", *gemini_levels(model)]:
            raise ValueError(f"{model} 不支持 {effort} 思考档位；请选择默认或其他思考档位")
    return effort


def gemini_thinking(model, effort):
    selected = gemini_effort(model, effort)
    if selected == "default":
        return {}
    if _gemini_thinking_level(model):
        return {"thinkingLevel": selected.upper(), "includeThoughts": True}
    if selected not in gemini_levels(model):
        raise ValueError(f"{model} 不支持 {effort} 思考档位；请选择默认或其他思考档位")
    budget = GEMINI_BUDGETS[selected]
    return {"thinkingBudget": budget, "includeThoughts": True}


def _gemini_thinking_level(model):
    match = re.search(r"(?:^|/)gemini-(\d+)(?=$|[.-])", model.lower())
    return bool(match and int(match[1]) >= 3)
