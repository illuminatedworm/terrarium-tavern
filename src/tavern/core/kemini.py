"""Visible, opt-in story compatibility settings and deterministic request frames."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from tavern.core.kemini_prompts import (
    CLEAR_PROMPT,
    CONTENT_DESCRIPTION,
    CONTENT_PROMPT,
    DISCLAIMER_PROMPT,
    IDENTITY_PROMPT,
    INFO_PROMPT,
    OUTPUT_PROMPT,
    ROLEPLAY_PROMPT,
    THINKING_PROMPT,
    TOOL_DESCRIPTION,
    TRANSPORT_TEMPLATE,
)
from tavern.core.story_segments import MAX_SEGMENTS

TOOL_NAME = "kemini_emit_story"
TRANSPORT_PROMPT = TRANSPORT_TEMPLATE.replace("${toolName}", TOOL_NAME)
FULL_AGENT_TRANSPORT_HEADING = "## 本轮正文交付与结束"
FULL_AGENT_TRANSPORT_PROMPT = FULL_AGENT_TRANSPORT_HEADING + (
    "\n\n"
    "- 根据回应本轮用户输入的需要，自行决定交付次数。"
    "只有本轮回应仍需补充正文时，才继续交付。写到可以把下一步交还给用户的自然停顿，就结束正文交付。"
    f"每轮最多成功调用 {MAX_SEGMENTS} 次 `submit_story`，每次只在 `content` 中按下述格式追加新正文。"
    "`Interleaving` 的分段与调用次数无关。\n"
    f"- 正文已回应本轮输入并写到自然停顿、已接收 {MAX_SEGMENTS} 段、收到 `STORY_LIMIT_REACHED`，"
    "满足任一条件即停止交付。\n"
    "- 完成剩余必要工具后，直接结束回复，不再调用工具。下述格式只用于交付新正文。"
)
PREFIX_SOURCES = ("kemini_filler", "kemini_clear", "kemini_identity", "kemini_info")
TURN_SOURCES = ("kemini_roleplay", "kemini_acknowledgement", "kemini_control")


class KeminiConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = Field(default=False, strict=True)
    normal_disclaimer_enabled: bool = Field(default=False, strict=True)
    clear_prompt: str = CLEAR_PROMPT
    identity_prompt: str = IDENTITY_PROMPT
    info_prompt: str = INFO_PROMPT
    roleplay_prompt: str = ROLEPLAY_PROMPT
    acknowledgement: str = "明白了。我会依照本轮要求完成角色扮演创作，并遵守实际工具权限和输出协议。"
    thinking_prompt: str = THINKING_PROMPT
    content_prompt: str = CONTENT_PROMPT
    output_prompt: str = OUTPUT_PROMPT
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
    return [
        {"role": role, "content": text, "source": source, "message_type": source}
        for role, text, source in (
            ("user", config.filler_text, "kemini_filler"),
            ("system", config.clear_prompt, "kemini_clear"),
            ("user", config.identity_prompt, "kemini_identity"),
            ("system", config.info_prompt, "kemini_info"),
        )
        if text.strip()
    ]


def control_text(config: KeminiConfig, *, full_agent=False, story_limit_reached: bool = False) -> str:
    if not config.enabled:
        return ""
    parts = [config.thinking_prompt, config.content_prompt]
    if config.normal_disclaimer_enabled:
        parts.append(config.disclaimer_text)
    transport_prompt = FULL_AGENT_TRANSPORT_PROMPT if full_agent else config.transport_prompt
    if full_agent and story_limit_reached:
        transport_prompt = transport_prompt.replace(
            FULL_AGENT_TRANSPORT_HEADING,
            f"{FULL_AGENT_TRANSPORT_HEADING}\n\n本轮已成功调用 {MAX_SEGMENTS} 次，剩余 0 次。",
            1,
        )
    # The ST script inserts transport instructions immediately before its
    # final format block. Keep that order without depending on an XML anchor.
    parts.extend((transport_prompt, config.output_prompt))
    return "\n\n".join(part for part in parts if part.strip())


def transport_tools(
    tools: list[dict] | None, config: KeminiConfig, choice: str | dict = "auto"
) -> list[dict] | None:
    """The caller's forced maintenance tool always wins; never widen that request."""
    if not config.enabled or choice != "auto":
        return tools
    return [
        *(tools or []),
        {
            "type": "function",
            "function": {
                "name": TOOL_NAME,
                "description": config.tool_description,
                "parameters": {
                    "type": "object",
                    "properties": {"content": {"type": "string", "description": config.content_description}},
                    "required": ["content"],
                },
            },
        },
    ]


def turn_frames(config: KeminiConfig, *, full_agent=False, story_limit_reached: bool = False) -> list[dict]:
    """Request-local requirements, never historical dialogue or durable updates."""
    if not config.enabled:
        return []
    return [
        {"role": role, "content": text, "source": source, "message_type": source}
        for role, text, source in (
            ("system", config.roleplay_prompt, "kemini_roleplay"),
            ("assistant", config.acknowledgement, "kemini_acknowledgement"),
            (
                "user",
                control_text(config, full_agent=full_agent, story_limit_reached=story_limit_reached),
                "kemini_control",
            ),
        )
        if text.strip()
    ]


def inject_half(messages: list[dict], config: KeminiConfig) -> list[dict]:
    if not config.enabled:
        return messages
    # All recipe/worldbook/script placement is already complete. The four
    # preamble blocks cannot count as dialogue or admit application blocks.
    return [*prefix_frames(config), *messages, *turn_frames(config)]
