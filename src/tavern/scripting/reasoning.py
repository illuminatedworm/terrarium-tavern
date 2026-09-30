"""Model capabilities for native thinking, separate from the shared UI effort."""
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


def anthropic_thinking(model, effort):
    name = model.lower().replace(".", "-")
    policy = next((p for key, p in ANTHROPIC_THINKING.items()
                   if re.search(r"claude-" + re.escape(key) + r"(?=$|:|-(?:[a-z]|\d{8}(?:$|[-:])))", name)),
                  AnthropicThinking(mode="effort", sampling=True))
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
    modern = bool(re.search(r"gemini-3(?=$|[.-])", name))
    if effort == "none" and (modern or re.search(r"gemini-2\.5-pro(?=$|-)", name)):
        raise ValueError(f"{model} 不支持关闭思考；请选择默认或其他思考档位")
    if modern:
        if effort == "xhigh":
            return "high"
        if effort == "minimal" and "pro" in name:
            return "low"
    return effort


def gemini_thinking(model, effort):
    selected = gemini_effort(model, effort)
    if selected == "default":
        return {}
    if re.search(r"gemini-3(?=$|[.-])", model.lower()):
        return {"thinkingLevel": selected.upper(), "includeThoughts": True}
    budget = {"none": 0, "minimal": 128, "low": 1024, "medium": 4096, "high": 8192, "xhigh": 16384}[selected]
    return {"thinkingBudget": budget, "includeThoughts": True}
