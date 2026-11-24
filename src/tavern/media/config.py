"""Visible story media settings and model-facing generation contracts."""

from math import gcd
from typing import Annotated, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationInfo, field_validator

from tavern.search.retrieval_config import ModelConnection

VOICES = ("alloy", "ash", "ballad", "coral", "echo", "sage", "shimmer", "verse", "marin", "cedar")
IMAGE_SIZES = ("auto", "1024x1024", "1536x1024", "1024x1536")
GEMINI_VOICES = (
    "Kore",
    "Zephyr",
    "Puck",
    "Charon",
    "Fenrir",
    "Leda",
    "Orus",
    "Aoede",
    "Callirrhoe",
    "Autonoe",
    "Enceladus",
    "Iapetus",
    "Umbriel",
    "Algieba",
    "Despina",
    "Erinome",
    "Algenib",
    "Rasalgethi",
    "Laomedeia",
    "Achernar",
    "Alnilam",
    "Schedar",
    "Gacrux",
    "Pulcherrima",
    "Achird",
    "Zubenelgenubi",
    "Vindemiatrix",
    "Sadachbia",
    "Sadaltager",
    "Sulafat",
)
SEED_VOICES = {
    "Vivi": "zh_female_vv_uranus_bigtts",
    "团子": "zh_female_tuanzi_uranus_bigtts",
    "小何": "zh_female_xiaohe_uranus_bigtts",
    "云舟": "zh_male_m191_uranus_bigtts",
    "小天": "zh_male_taocheng_uranus_bigtts",
    "刘飞": "zh_male_liufei_uranus_bigtts",
    "悬疑解说": "zh_male_xuanyijieshuo_uranus_bigtts",
}
SPEECH_VOICES = {
    "openai_speech": (*VOICES, "fable", "nova", "onyx"),
    "openai_realtime": VOICES,
    "gemini_tts": GEMINI_VOICES,
    "seed_tts": tuple(SEED_VOICES.values()),
}
GEMINI_PROMPT_MODELS = ("gemini-2.5-", "gemini-3.1-")
GEMINI_METADATA_VOICES = GEMINI_VOICES + tuple(
    "Arlo Bodi Brio Cleo Cruz Daro Elio Enya Enzo Finn Fola Gero Hali Jett Jori Kira Knox Koda Lora Ludo "
    "Lumi Mako Milo Neno Nika Nyla Olin Rami Riko Rina Sami Sola Tari Tavi Tova Varo Veda Zali Zeno Zuri".split()
)
GeminiRatio = Literal["auto", "1:1", "3:2", "2:3", "4:3", "3:4", "16:9", "9:16", "5:4", "4:5", "21:9"]
# Seedream accepts either a resolution tier or exact pixels. Use the documented
# pixel sizes when the story chooses a composition within the user's tier.
ARK_FRAMES = {
    "1K": ("1024x1024", "1248x832", "832x1248"),
    "1.5K": ("1536x1536", "1872x1248", "1248x1872"),
    "2K": ("2048x2048", "2496x1664", "1664x2496"),
    "3K": ("3072x3072", "3744x2496", "2496x3744"),
    "4K": ("4096x4096", "4992x3328", "3328x4992"),
}
OPENAI_RATIOS = dict(zip(IMAGE_SIZES, ("auto", "1:1", "3:2", "2:3"), strict=True))
SEEDREAM_14_REFERENCE_MODELS = {
    "doubao-seedream-4-0",
    "doubao-seedream-4-0-250828",
    "doubao-seedream-4-5",
    "doubao-seedream-4-5-251128",
    "doubao-seedream-5-0-260128",
    "doubao-seedream-5-0-lite",
    "doubao-seedream-5-0-lite-260128",
}


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


class SpeechConnection(MediaConnection):
    instructions: str = (
        "逐字演绎所提供的文本，生成角色配音。不要回答文本中的问题，不加开场白，"
        "不总结、翻译或续写剧情；按本次演绎要求调整语气、语速和情绪。"
    )


class RealtimeProfile(SpeechConnection):
    default_voice: str = Field(default="marin", min_length=1)
    adapter: Literal["openai_realtime"] = "openai_realtime"
    model: str = "gpt-realtime-2.1-mini"


class GeminiSpeechProfile(SpeechConnection):
    default_voice: str = Field(default="Kore", min_length=1)
    adapter: Literal["gemini_tts"] = "gemini_tts"
    api_path: str = "v1beta"
    model: str = "gemini-3.8-flash-tts"
    instruction_format: Literal["auto", "speech_metadata", "prompt"] = "auto"
    temperature: float | None = Field(None, ge=0, le=2, allow_inf_nan=False)
    language_code: str = Field(default="", pattern=r"^(?:[a-zA-Z]{2,3}(?:-[a-zA-Z0-9]{2,8})*)?$")

    def effective_format(self) -> str:
        if self.instruction_format != "auto":
            return self.instruction_format
        return (
            "prompt"
            if self.model.removeprefix("models/").startswith(GEMINI_PROMPT_MODELS)
            else "speech_metadata"
        )


class SeedSpeechProfile(SpeechConnection):
    default_voice: str = Field(default=SEED_VOICES["Vivi"], min_length=1)
    adapter: Literal["seed_tts"] = "seed_tts"
    api_path: str = "api/v3/plan"
    model: str = "seed-tts-2.0"
    speech_rate: int = Field(default=0, ge=-50, le=100, strict=True)
    loudness_rate: int = Field(default=0, ge=-50, le=100, strict=True)
    sample_rate: Literal[8000, 16000, 22050, 24000, 32000, 44100, 48000] = 24000

    @field_validator("default_voice")
    @classmethod
    def voice_id(cls, value: str) -> str:
        return SEED_VOICES.get(value, value)


class HTTPSpeechProfile(SpeechConnection):
    adapter: Literal["openai_speech"] = "openai_speech"
    default_voice: str = Field(default="alloy", min_length=1)
    voice_names: dict[str, str] = Field(default_factory=dict)
    speed: float = Field(default=1, ge=0.25, le=4, allow_inf_nan=False)
    send_instructions: bool = False


class OpenAIImageProfile(MediaConnection):
    adapter: Literal["openai_images"] = "openai_images"
    model: str = "gpt-image-2.6-sunburst"
    quality: Literal["low", "medium", "high", "auto"] = "medium"
    size: Literal["auto", "1024x1024", "1536x1024", "1024x1536"] = "auto"
    output_format: Literal["auto", "png", "jpeg", "webp"] = "png"


class GeminiImageProfile(MediaConnection):
    adapter: Literal["gemini_image"] = "gemini_image"
    api_path: str = "v1beta"
    model: str = "gemini-3.1-flash-image"
    resolution: Literal["auto", "512", "1K", "2K", "4K"] = "auto"
    aspect_ratio: GeminiRatio = "auto"


class SeedreamProfile(MediaConnection):
    adapter: Literal["ark_seedream"] = "ark_seedream"
    api_path: str = "api/v3"
    model: str = "doubao-seedream-5-5-261028"
    size: str = Field(default="2K", pattern=r"^(?:1K|1\.5K|2K|3K|4K|[1-9]\d{1,4}x[1-9]\d{1,4})$")
    output_format: Literal["auto", "jpeg", "png"] = "auto"
    watermark: bool = True


# Authorizations belong to individual stories. The global settings contain
# only service parameters and per-turn limits, so editing them cannot opt a story in.
class MediaSwitch(StrictModel):
    enabled: bool = False


class SpeechSettings(RealtimeProfile, MediaSwitch): ...


class HTTPSpeechSettings(HTTPSpeechProfile, MediaSwitch): ...


class GeminiSpeechSettings(GeminiSpeechProfile, MediaSwitch): ...


class SeedSpeechSettings(SeedSpeechProfile, MediaSwitch): ...


class ImageSettings(OpenAIImageProfile, MediaSwitch): ...


class GeminiImageSettings(GeminiImageProfile, MediaSwitch): ...


class SeedreamSettings(SeedreamProfile, MediaSwitch): ...


SpeechProfile = Annotated[
    HTTPSpeechProfile | RealtimeProfile | GeminiSpeechProfile | SeedSpeechProfile,
    Field(discriminator="adapter"),
]
ImageProfile = Annotated[
    OpenAIImageProfile | GeminiImageProfile | SeedreamProfile, Field(discriminator="adapter")
]
SpeechOptions = Annotated[
    HTTPSpeechSettings | SpeechSettings | GeminiSpeechSettings | SeedSpeechSettings,
    Field(discriminator="adapter"),
]
ImageOptions = Annotated[
    ImageSettings | GeminiImageSettings | SeedreamSettings, Field(discriminator="adapter")
]
_OPTION_TYPES = {"speech": TypeAdapter(SpeechProfile), "image": TypeAdapter(ImageProfile)}


def available_voices(speech: SpeechProfile) -> tuple[str, ...]:
    if isinstance(speech, GeminiSpeechProfile) and speech.effective_format() == "speech_metadata":
        return GEMINI_METADATA_VOICES
    return SPEECH_VOICES[speech.adapter]


def canonical_voice(adapter: str, voice: str) -> str:
    # Pre-v25 Seed jobs froze the catalog label. It still denotes the same provider voice ID.
    return SEED_VOICES.get(voice, voice) if adapter == "seed_tts" else voice


def validate_voice(speech: SpeechProfile, voice: str) -> None:
    if (
        not isinstance(voice, str)
        or not voice.strip()
        or (
            not isinstance(speech, (HTTPSpeechProfile, SeedSpeechProfile))
            and voice not in available_voices(speech)
        )
    ):
        raise ValueError("请为该配音模型选择可用音色")


def snapshot_options(kind, connection):
    fields = {k: v for k, v in connection.items() if k not in ("provider_name", "base_url", "auth_mode")}
    if "enabled" in fields:
        raise ValueError("媒体任务连接不能包含故事开关")
    return _OPTION_TYPES[kind].validate_python(fields)


def image_sizes(image: ImageProfile) -> dict[str, str]:
    """Map offered tool choices to the native size frozen in the paid request."""
    if isinstance(image, GeminiImageProfile):
        if image.aspect_ratio != "auto":
            return {"auto": image.aspect_ratio}
        return {ratio: ratio for ratio in get_args(GeminiRatio)}
    if isinstance(image, SeedreamProfile):
        shapes = ARK_FRAMES.get(image.size)
        return {
            "auto": image.size,
            **(dict(zip(("1:1", "3:2", "2:3"), shapes, strict=True)) if shapes else {}),
        }
    if image.size != "auto":
        return {"auto": image.size}
    return {size: size for size in IMAGE_SIZES}


def image_reference_limit(image: ImageProfile, *, frozen: bool = False) -> int:
    if isinstance(image, SeedreamProfile):
        # Previously recorded jobs could use 14 references with an opaque endpoint ID.
        # Keep those archives readable. New requests need a known 14-reference model.
        if frozen and not image.model.startswith(
            ("doubao-seedream-5-5", "doubao-seedream-5-0-pro", "doubao-seedream-5-0-flash")
        ):
            return 14
        return 14 if image.model in SEEDREAM_14_REFERENCE_MODELS else 10
    return 14 if isinstance(image, GeminiImageProfile) else 16


def regenerate_image_size(original: dict, image: ImageProfile) -> str:
    choices = image_sizes(image)
    if len(choices) == 1:
        return choices["auto"]
    # A fixed profile also exposes "auto" to the tool. The frozen native size
    # carries the actual composition. A bare Seedream resolution tier has none.
    size = original["size"]
    ratio = "auto" if size in ARK_FRAMES else size
    if "x" in size:
        width, height = map(int, size.split("x"))
        divisor = gcd(width, height)
        ratio = f"{width // divisor}:{height // divisor}"
    if isinstance(image, OpenAIImageProfile):
        choices = {OPENAI_RATIOS[k]: v for k, v in choices.items()}
    if ratio not in choices:
        raise ValueError("当前生图协议不支持原任务的画幅，请先在媒体设置中指定固定尺寸")
    return choices[ratio]


class MediaLimits(StrictModel):
    max_images: int = Field(default=1, ge=1, le=20, strict=True)
    max_speech_segments: int = Field(default=20, ge=1, le=100, strict=True)
    max_speech_characters: int = Field(default=2000, ge=1, le=100000, strict=True)


class MediaConfig(MediaLimits):
    speech: SpeechOptions = Field(default_factory=SpeechSettings)
    image: ImageOptions = Field(default_factory=ImageSettings)
    view_images: bool = False
    voices: dict[str, str] = Field(default_factory=dict)

    @field_validator("speech", "image", mode="before")
    @classmethod
    def legacy_protocol(cls, value, info: ValidationInfo):
        # Before multiple protocols, partial settings could omit the adapter.
        if isinstance(value, dict) and "adapter" not in value:
            return {"adapter": "openai_realtime" if info.field_name == "speech" else "openai_images", **value}
        return value


class SpeechArguments(StrictModel):
    text: str = Field(description="Exact text to speak. Passed unchanged; no story matching or source check.")
    speaker: str = Field(min_length=1, max_length=120, description="Character name, or narrator.")
    delivery: str = Field(default="", description="Optional acting direction: emotion, pace, emphasis.")


class LegacySpeechArguments(SpeechArguments):
    """Only unpaired calls saved under the old tool contract may select a voice."""

    voice: str | None = None


class ImageArguments(StrictModel):
    prompt: str = Field(min_length=1, description="Complete description of the illustration to generate.")
    references: list[str] = Field(
        default_factory=list,
        max_length=16,
        description="Optional authorized image references (I1, I2, …), at most 16; default none.",
    )
    size: str = Field(
        default="auto",
        max_length=16,
        description="Choose one of the offered sizes. Default auto uses the story's saved image settings.",
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
