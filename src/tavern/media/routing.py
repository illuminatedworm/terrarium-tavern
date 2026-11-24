"""User-selected media channels, independent story overrides, and scoped speaker bindings."""

import json
from typing import Generic, TypeVar

from pydantic import ConfigDict, Field, model_validator

from tavern.core.channels import Channel, provider_channel
from tavern.media.config import (
    HTTPSpeechProfile,
    ImageProfile,
    MediaConfig,
    MediaSwitch,
    OpenAIImageProfile,
    RealtimeProfile,
    SpeechProfile,
    StrictModel,
    canonical_voice,
    validate_voice,
)

Profile = TypeVar("Profile", bound=SpeechProfile | ImageProfile)


class ChannelSettings(StrictModel, Generic[Profile]):
    active_channel: Channel = "standard"
    standard: Profile
    chatgpt: Profile
    remembered: list[Profile] = Field(default_factory=list)

    @property
    def current(self) -> Profile:
        return getattr(self, self.active_channel)

    @model_validator(mode="after")
    def unique_connections(self):
        ids = [p.provider_id for p in (self.standard, self.chatgpt) if p.provider_id]
        ids.extend(p.provider_id for p in self.remembered)
        if "" in ids or len(ids) != len(set(ids)):
            raise ValueError("媒体提供商的配置重复或缺少连接")
        return self


class SpeechChannels(ChannelSettings[SpeechProfile]):
    standard: SpeechProfile = Field(default_factory=HTTPSpeechProfile)
    chatgpt: SpeechProfile = Field(
        default_factory=lambda: HTTPSpeechProfile(model="gpt-4o-mini-tts", send_instructions=True)
    )
    max_segments: int = Field(default=20, ge=1, le=100, strict=True)
    max_characters: int = Field(default=2000, ge=1, le=100000, strict=True)


class ImageChannels(ChannelSettings[ImageProfile]):
    standard: ImageProfile = Field(
        default_factory=lambda: OpenAIImageProfile(model="", quality="auto", output_format="auto")
    )
    chatgpt: ImageProfile = Field(default_factory=OpenAIImageProfile)
    max_images: int = Field(default=1, ge=1, le=20, strict=True)


class MediaDefaults(StrictModel):
    speech: SpeechChannels = Field(default_factory=SpeechChannels)
    image: ImageChannels = Field(default_factory=ImageChannels)


class ModelKey(StrictModel):
    model_config = ConfigDict(frozen=True)
    provider_id: str
    model: str


def media_key(profile) -> ModelKey:
    return ModelKey(provider_id=profile.provider_id, model=profile.model)


def story_key(session, document) -> ModelKey:
    # A per-story empty model is a real selection. Only an absent selection
    # follows the story's channel profile. No backend IDs enter story prompts.
    value = session["model_selection"] if "model_selection" in session.keys() else None
    if isinstance(value, str):
        value = json.loads(value)
    if value:
        return ModelKey(provider_id=value["provider_id"], model=value["model"])
    official = "channel_origin" in session.keys() and session["channel_origin"] == "chatgpt"
    profile = document.story.official_model if official else document.story.model
    return media_key(profile) if profile else ModelKey(provider_id="", model="")


class VoiceTable(StrictModel):
    story_model: ModelKey
    speech_model: ModelKey
    voices: dict[str, str]


class StorySpeech(MediaSwitch):
    custom: SpeechChannels | None = None


class StoryImage(MediaSwitch):
    custom: ImageChannels | None = None


class StoryMediaSettings(StrictModel):
    speech: StorySpeech = Field(default_factory=StorySpeech)
    image: StoryImage = Field(default_factory=StoryImage)
    view_images: bool = False
    voice_tables: list[VoiceTable] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_tables(self):
        keys = {(t.story_model, t.speech_model) for t in self.voice_tables}
        if len(keys) != len(self.voice_tables):
            raise ValueError("同一剧情模型和配音模型的角色表重复")
        return self

    @property
    def source(self):
        local = (self.speech.custom is not None, self.image.custom is not None)
        return "story" if all(local) else "mixed" if any(local) else "global"

    def table(self, story: ModelKey, speech: ModelKey) -> VoiceTable | None:
        return next(
            (t for t in self.voice_tables if t.story_model == story and t.speech_model == speech), None
        )

    def resolve(self, defaults: MediaDefaults, story: ModelKey) -> MediaConfig:
        speech = self.speech.custom or defaults.speech
        image = self.image.custom or defaults.image
        table = self.table(story, media_key(speech.current))
        return MediaConfig(
            speech={**speech.current.model_dump(), "enabled": self.speech.enabled},
            image={**image.current.model_dump(), "enabled": self.image.enabled},
            max_speech_segments=speech.max_segments,
            max_speech_characters=speech.max_characters,
            max_images=image.max_images,
            view_images=self.view_images,
            voices=table.voices if table else {},
        )


def validate_channels(routes, providers, *, allow_missing=False):
    by_id = {p.id: p for p in providers}
    for channel in ("standard", "chatgpt"):
        profile = getattr(routes, channel)
        if isinstance(routes, SpeechChannels):
            validate_voice(profile, profile.default_voice)
        if not profile.provider_id:
            continue
        p = by_id.get(profile.provider_id)
        if p is None:
            if allow_missing:
                continue
            raise ValueError("全局媒体设置引用了已移除的提供商，请在配音与生图中更换连接")
        if p.auth_mode == "chatgpt_subscription" or provider_channel(p) != channel:
            raise ValueError("媒体连接与所选渠道不匹配，请选择该渠道的 API 提供商")


def upgrade_defaults(raw: dict, providers: list) -> MediaDefaults:
    """Place v24's single live profile into its actual credential family."""
    if "active_channel" in raw.get("speech", {}):
        return MediaDefaults.model_validate(raw)
    old = MediaConfig.model_validate(raw)
    result = MediaDefaults()
    for kind in ("speech", "image"):
        profile = getattr(old, kind)
        fields = profile.model_dump(exclude={"enabled"})
        route = getattr(result, kind)
        # Virgin, unconfigured defaults can start on the new generic HTTP route.
        if (
            not profile.provider_id
            and fields == (RealtimeProfile() if kind == "speech" else OpenAIImageProfile()).model_dump()
        ):
            continue
        p = next(
            (p for p in providers if (p["id"] if isinstance(p, dict) else p.id) == profile.provider_id), None
        )
        channel = provider_channel(p) if p else "standard"
        setattr(result, kind, type(route).model_validate({"active_channel": channel, channel: fields}))
    result.speech.max_segments = old.max_speech_segments
    result.speech.max_characters = old.max_speech_characters
    result.image.max_images = old.max_images
    return result


def upgrade_selection(
    raw, session, document, previous: StoryMediaSettings | None = None
) -> StoryMediaSettings:
    if "voice_tables" in raw or "custom" in raw.get("speech", {}) or "custom" in raw.get("image", {}):
        return StoryMediaSettings.model_validate(raw)
    if "source" in raw and raw["source"] != "global":
        raise ValueError("未知的故事媒体设置来源")
    follows = raw.get("source") == "global"
    values = {k: v for k, v in raw.items() if k != "source"}
    if follows:
        if values.keys() - {"speech", "image", "voices", "view_images"}:
            raise ValueError("跟随全局的故事仅保存授权和角色音色")
        for kind in ("speech", "image"):
            MediaSwitch.model_validate(values.get(kind, {}))
    old = MediaConfig.model_validate(values)
    routes = document.story.media if follows else upgrade_defaults(values, document.shared.providers)
    selection = StoryMediaSettings(
        speech=StorySpeech(enabled=old.speech.enabled, custom=None if follows else routes.speech),
        image=StoryImage(enabled=old.image.enabled, custom=None if follows else routes.image),
        view_images=old.view_images,
    )
    text = story_key(session, document)
    speech = media_key(routes.speech.current)
    if previous:
        # An older client can edit only the active choices and this one role table.
        selection.voice_tables = [
            t for t in previous.voice_tables if (t.story_model, t.speech_model) != (text, speech)
        ]
        if not follows:
            for kind in ("speech", "image"):
                active = getattr(routes, kind)
                before = getattr(previous, kind).custom or getattr(document.story.media, kind)
                channel = active.active_channel
                other = "chatgpt" if channel == "standard" else "standard"
                setattr(active, other, getattr(before, other))
                retained = [*before.remembered, getattr(before, channel)]
                active.remembered = [
                    p for p in retained if p.provider_id and p.provider_id != active.current.provider_id
                ]
    if old.voices:
        voices = {
            name: canonical_voice(routes.speech.current.adapter, voice) for name, voice in old.voices.items()
        }
        selection.voice_tables.append(VoiceTable(story_model=text, speech_model=speech, voices=voices))
    return selection
