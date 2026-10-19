"""Visible, opt-in story compatibility settings and deterministic request frames."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from tavern.core.kemini_prompts import (
    CLEAR_PROMPT,
    CONTENT_DESCRIPTION,
    DISCLAIMER_PROMPT,
    IDENTITY_PROMPT,
    INFO_PROMPT,
    THINKING_PROMPT,
    TOOL_DESCRIPTION,
    TRANSPORT_TEMPLATE,
)

TOOL_NAME = "kemini_emit_story"
TRANSPORT_PROMPT = TRANSPORT_TEMPLATE.replace("${toolName}", TOOL_NAME)
PREFIX_SOURCES = ("kemini_filler", "kemini_clear", "kemini_identity", "kemini_info")


class KeminiConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = Field(default=False, strict=True)
    normal_disclaimer_enabled: bool = Field(default=False, strict=True)
    clear_prompt: str = CLEAR_PROMPT
    identity_prompt: str = IDENTITY_PROMPT
    info_prompt: str = INFO_PROMPT
    acknowledgement: str = "明白了。我会依照本轮要求完成角色扮演创作，并遵守实际工具权限和输出协议。"
    thinking_prompt: str = THINKING_PROMPT
    filler_text: str = ""
    disclaimer_text: str = DISCLAIMER_PROMPT
    transport_prompt: str = TRANSPORT_PROMPT
    tool_description: str = TOOL_DESCRIPTION
    content_description: str = CONTENT_DESCRIPTION


class AuxiliaryPart(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["thinking", "disclaimer", "extra"]
    text: str


def prefix_frames(config: KeminiConfig) -> list[dict]:
    """An absolute preamble, outside recipe, worldbook and script depth indexing."""
    if not config.enabled:
        return []
    return [{"role": role, "content": text, "source": source, "message_type": source}
            for role, text, source in (
                ("user", config.filler_text, "kemini_filler"),
                ("system", config.clear_prompt, "kemini_clear"),
                ("user", config.identity_prompt, "kemini_identity"),
                ("system", config.info_prompt, "kemini_info")) if text.strip()]


def control_text(config: KeminiConfig) -> str:
    if not config.enabled:
        return ""
    parts = [config.thinking_prompt]
    if config.normal_disclaimer_enabled:
        parts.append(config.disclaimer_text)
    parts.append(config.transport_prompt)
    return "\n\n".join(part for part in parts if part.strip())


def transport_tools(tools: list[dict] | None, config: KeminiConfig, choice: str | dict = "auto") -> list[dict] | None:
    """The caller's forced maintenance tool always wins; never widen that request."""
    if not config.enabled or choice != "auto":
        return tools
    return [*(tools or []), {"type": "function", "function": {
        "name": TOOL_NAME, "description": config.tool_description,
        "parameters": {"type": "object", "properties": {"content": {"type": "string",
            "description": config.content_description}},
            "required": ["content"]}}}]


def inject_half(messages: list[dict], config: KeminiConfig) -> list[dict]:
    if not config.enabled:
        return messages
    # All recipe/worldbook/script placement is already complete. The four
    # preamble blocks cannot count as dialogue or admit application blocks.
    suffix = [{"role": role, "content": text, "source": source, "message_type": source}
              for role, text, source in (
                  ("assistant", config.acknowledgement, "kemini_acknowledgement"),
                  ("user", control_text(config), "kemini_control")) if text.strip()]
    return [*prefix_frames(config), *messages, *suffix]
