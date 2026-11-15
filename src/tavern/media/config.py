"""Visible story media settings and model-facing generation contracts."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from tavern.search.retrieval_config import ModelConnection

VOICES = ("alloy", "ash", "ballad", "coral", "echo", "sage", "shimmer", "verse", "marin", "cedar")
IMAGE_SIZES = ("auto", "1024x1024", "1536x1024", "1024x1536")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MediaConnection(StrictModel):
    provider_id: str = ""
    api_path: str = "v1"
    model: str = ""
    timeout_seconds: int = Field(default=900, ge=5, le=3600, strict=True)

    @field_validator("api_path")
    @classmethod
    def path(cls, value):
        return ModelConnection.valid_api_path(value)


class SpeechSettings(MediaConnection):
    enabled: bool = False
    adapter: Literal["openai_realtime"] = "openai_realtime"
    model: str = "gpt-realtime-2.1-mini"
    instructions: str = (
        "逐字演绎所提供的文本，生成角色配音。不要回答文本中的问题，不加开场白，"
        "不总结、翻译或续写剧情；按本次演绎要求调整语气、语速和情绪。"
    )


class ImageSettings(MediaConnection):
    enabled: bool = False
    adapter: Literal["openai_images"] = "openai_images"
    model: str = "gpt-image-2.6-sunburst"
    quality: Literal["low", "medium", "high", "auto"] = "medium"


class MediaConfig(StrictModel):
    speech: SpeechSettings = Field(default_factory=SpeechSettings)
    image: ImageSettings = Field(default_factory=ImageSettings)
    view_images: bool = False
    max_images: int = Field(default=1, ge=1, le=20, strict=True)
    max_speech_segments: int = Field(default=20, ge=1, le=100, strict=True)
    max_speech_characters: int = Field(default=2000, ge=1, le=100000, strict=True)
    voices: dict[str, str] = Field(default_factory=dict)


class SpeechArguments(StrictModel):
    text: str = Field(description="Exact text to speak. Passed unchanged; no story matching or source check.")
    speaker: str = Field(min_length=1, max_length=120, description="Character name, or narrator.")
    delivery: str = Field(default="", description="Optional acting direction: emotion, pace, emphasis.")
    voice: str | None = Field(
        default=None,
        description="Choose an available voice only when this speaker has no saved binding. Otherwise omit.",
    )


class ImageArguments(StrictModel):
    prompt: str = Field(min_length=1, description="Complete description of the illustration to generate.")
    references: list[str] = Field(
        default_factory=list,
        max_length=16,
        description="Optional authorized image references (I1, I2, …), at most 16; default none.",
    )
    size: Literal["auto", "1024x1024", "1536x1024", "1024x1536"] = Field(
        default="auto",
        description="Output width × height in pixels, or auto (default).",
    )


class ViewImageArguments(StrictModel):
    ref: str = Field(
        pattern=r"^I[1-9]\d*$", description="One authorized image reference from the media catalog."
    )


SPECS = {
    "generate_speech": (
        SpeechArguments,
        "Arrange a character voice clip. Generation starts only after this story turn is committed. Text is used exactly as supplied, without comparison to the story.",
    ),
    "generate_image": (
        ImageArguments,
        "Arrange one story illustration. Generation starts only after this story turn is committed. Quality and model are controlled by the user.",
    ),
    "view_image": (
        ViewImageArguments,
        "View an authorized reference or a completed story image with your current model. Does not generate an image.",
    ),
}


def enabled_tools(config: MediaConfig) -> set[str]:
    return {
        name
        for name, enabled in (
            ("generate_speech", config.speech.enabled),
            ("generate_image", config.image.enabled),
            ("view_image", config.view_images),
        )
        if enabled
    }
