"""Persisted settings; runtime projections are disposable, never the source of truth."""

from copy import deepcopy
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

from tavern.advisor.config import SYSTEM as ADVISOR_PROMPT
from tavern.advisor.config import AdvisorConfig, AdvisorOptions
from tavern.character.editor_config import SYSTEM as CARD_AUTHOR_PROMPT
from tavern.character.editor_config import CardAuthorOptions
from tavern.core.cache_policy import endpoint, fingerprint, runtime_cache
from tavern.core.channels import Channel, provider_channel
from tavern.core.kemini import KeminiConfig
from tavern.core.kemini_prompts import (
    LEGACY_CLEAR_PROMPT,
    LEGACY_INFO_PROMPT,
    LEGACY_THINKING_PROMPT,
    migrate_interleaving_prompts,
)
from tavern.core.models import PreferencePreset, Settings
from tavern.core.prompt_choices import PromptChoices
from tavern.core.prompts import (
    DEFAULT_LOCAL_INSTRUCTION,
    DEFAULT_PREFERENCE_PROMPT,
    DEFAULT_PROBE_MARKER_PROMPT,
    DEFAULT_PROBE_PROMPT,
    DEFAULT_REGENERATE_PROMPT,
    DEFAULT_RERANKER_PROMPT,
)
from tavern.core.update_policy import Policy
from tavern.media.routing import MediaDefaults, upgrade_defaults, validate_channels
from tavern.novel.config import NovelConfig
from tavern.search.retrieval_config import LocalModel, ModelProfile, Network, Provider, RetrievalSettings
from tavern.storage.legacy_story import (
    discard_retired_settings,
    upgrade_card_author_guide_references,
    upgrade_settings_keys,
)
from tavern.story.config import STORY_PROMPT, StoryConfig
from tavern.web.config import WebConfig, migrate_web_settings

LEGACY_STORY_PROMPT_HASHES = {
    "2ab331d1d01ab9ffeeb9436e27af00b7425f7aed7b1a685e5d38081a84dc7727",
    "74cfaf344bc123d16a6604b6dcecd8ebbfa335ef0e5c62d54ca27fe641111566",
    "d72b35768688226ad359f3fdc8dcb04286633b4da808a8ee6aa70f7f47e8e00b",
    "4bc871cd5021ed1be8b86b36fd4f072ecd6cbd8e017cd074eaa1107f47af257e",
    "7ee23c619af1a6cb406489c4c9316a8457e2161d68d1ef5feba332c8fdb267ea",
    "95173fea564656ff5b21f15e6a6bcc5b76f3f9d45255346913edf11de955b462",
    "d3a665909b62c5a6a3d739d1f899a1b9782694a9c899ae3ea66d9c00aee9e2e3",
    "af511dd7f085b1028fe8d610f83014843b3ec24099463db8649f3dd794a6a1ca",
    "33c4c671fe89cada3300edc6a3551307c6890864bee0dfc380c00a477b6498ba",
    "ca82508b4903dfa3411d76b12e6b553ef19e18433dda34bb3232ef106b18f832",
    "75f0ff256f82a0afad50077a04a6c26544867d59ca411dce4bb309c8386f1fd9",
    "e7124336caed2c3e63eb011f78672601d4dcd2ff2d4a45b81fbdfb2769780579",
}

LEGACY_ADVISOR_PROMPT_HASHES = {
    "62e33b9d5bc78cdbbe3badf7d94cb4bd8150fc48f7cca1875f00d6decfd303ad",
    "0693d1b8e704e83a913b7f9e477cc10c27290b9db207d2882ef04193eeb65cab",
    "f7071ba980f49b6cb9889c4652bcf9b9d2392a130e208bed162324ed185c8e44",
    "86617163b364451f012f2e8810d1580433a3db6ab2381c9c6df7a7e4e95e7659",
    "b1224d17c30b83d2b0c8fc9951e38f19b56a8ce3b45c2b9f93a8ea96ed8803ad",
    "7fcffe8be2d2a71e5a631f6a4a7bd2387321e95519d1cc7de455a5c28ec61e05",
    "28a812d6b0a58f07fe62828a35f0ac1f582d86502ad5a179bbaa35d556213b67",
}

# Previous built-in Card Author prompts replaced by the current rules.
LEGACY_CARD_AUTHOR_PROMPT_HASHES = {
    "f924e6726dea8984b252e4b38eb92c810415da240b16cce2fd44ad729ccdc863",
    "04ffb4bf795237bc697f7dbf46a4173818a770248f934bc652ba4f837eabcf06",
    "aa8251e3d7628c8348a285dbc517e5247a83166bc08ccb18c924b1ce18bfc869",
    "8448879a9facf4ca7f9134e539dc2815021b7c512b65b842c99da161a47b5426",
    "dd958297acd37514917b3f5e0eca8790db78fa874ff7930b372e11d0e80c3291",
    "2884032491d685dcaa5b9d51a5249cb242f3d60293bb657b34da9a3b20fe3a90",
    "13812c7e5d4714e18cb3a7475100203252d1f92be472cbe138f5d58cacc5f4fd",
    "677e11901c5cb0c5a4e0320eae8ae7aa8ba4eb3bdda4d6d8ce0a193da051fdf3",
    "3271754a32525f77b7e56175d8cc1be197e79143f352c95776d1e044140cd039",
    "16efa497a58295743b8c8f1f3ff528d4eaa8cd8b8d4fda189e7288c0a5b26b5b",
    "b2b062ce5f9af5d8aaeb09fde38c986cf825b8638a7e35ed13af7b925dd15453",
    "ddc4de9d44a5b4301c62f516cd050786132eed1a3e2a8db0d4df77f684d7c5b3",
    "7f1b33b5b7c60ac39b73e363af2f972d950501c98593ff5a76adc20f61e55ce8",
    "2bbdacc371814cff642baf200ded4d645e57aea23ecc35d76ce7899ed59cb221",
    "f90f0c751b6b5e52de17ade3bdee95ef91d543dfa68ac3b1afaeafd20d910f1a",
    "cb9f2c79f916a115d5ab85e53ca66cdf70d048dd9326939f3d3671ab93529204",
    "7be81331ec72819352cd1d7e7d33d4789b5805fdb1537b3671c19948a3c0c850",
    "c7f41a64378a9f615bea6c38d07ef53dc1e92437e45af6fcbc7b82ecb2988e84",
}


class StrictSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RetrievalRuntime(StrictSettings):
    instruction_prompt: str = DEFAULT_LOCAL_INSTRUCTION
    reranker_prompt: str = DEFAULT_RERANKER_PROMPT
    reranker_enabled: bool = False
    rerank_fragments: int = Field(default=3, ge=1, le=8)


class AgentSettings(StrictSettings):
    default_channel: Channel = "standard"
    retrieval: RetrievalSettings = Field(default_factory=RetrievalSettings)
    local_runtime: RetrievalRuntime = Field(default_factory=RetrievalRuntime)
    lore_recursive: bool = Field(default=True, strict=True)
    lore_recursion_depth: int = Field(default=1, ge=0, le=16, strict=True)
    lore_max_entries: int = Field(default=5, ge=1, le=1000, strict=True)
    lore_preview_chars: int = Field(default=120, ge=1, le=2_000_000, strict=True)
    # Include a title line by default; users can choose ST-style content-only injection.
    lore_injection_title: bool = Field(default=True, strict=True)
    # Legacy setting retained for stored profiles; selected entries are always complete.
    lore_max_chars: int = Field(default=3000, ge=1, le=2000000, strict=True)
    policy: Policy = "automatic"
    preference_prompt: str = DEFAULT_PREFERENCE_PROMPT
    regenerate_prompt: str = DEFAULT_REGENERATE_PROMPT
    probe_system_prompt: str = DEFAULT_PROBE_PROMPT
    probe_marker_prompt: str = DEFAULT_PROBE_MARKER_PROMPT


class StorySettings(AgentSettings):
    model: ModelProfile = Field(default_factory=lambda: ModelProfile(cache=runtime_cache()))
    official_model: ModelProfile | None = None
    tools: StoryConfig = Field(default_factory=StoryConfig)
    system_prompt: PromptChoices = Field(default_factory=lambda: PromptChoices.from_text(STORY_PROMPT))
    novel_research: NovelConfig = Field(default_factory=NovelConfig)
    media: MediaDefaults = Field(default_factory=MediaDefaults)

    @model_validator(mode="before")
    @classmethod
    def default_story_cache(cls, value):
        if isinstance(value, dict) and isinstance(value.get("model"), dict) and "cache" not in value["model"]:
            value = deepcopy(value)
            value["model"]["cache"] = {"mode": "runtime"}
        return value


class LocalResources(StrictSettings):
    embedding: LocalModel = Field(default_factory=lambda: LocalModel(model="Qwen/Qwen3-Embedding-0.6B"))
    reranker: LocalModel = Field(default_factory=lambda: LocalModel(model="Qwen/Qwen3-Reranker-0.6B"))

    @model_validator(mode="after")
    def supported(self):
        from tavern.search.retrieval_config import LocalModels

        LocalModels.model_validate(self.model_dump())
        return self


class AdvisorSettings(AdvisorOptions):
    system_prompt: PromptChoices = Field(default_factory=lambda: PromptChoices.from_text(ADVISOR_PROMPT))

    def runtime(self) -> AdvisorConfig:
        return AdvisorConfig(
            **self.model_dump(exclude={"system_prompt"}), system_prompt=self.system_prompt.current
        )


class CardAuthorSettings(CardAuthorOptions):
    system_prompt: PromptChoices = Field(default_factory=lambda: PromptChoices.from_text(CARD_AUTHOR_PROMPT))


class SharedSettings(StrictSettings):
    kemini: KeminiConfig = Field(default_factory=KeminiConfig)
    unicode_story_enabled: bool = Field(default=False, strict=True)
    send_shortcut: Literal["enter", "ctrl_enter", "shift_enter", "button_only"] = "ctrl_enter"
    debug_failed_requests: bool = Field(default=False, strict=True)
    debug_story_prefix: bool = Field(default=False, strict=True)
    model_request_mode: Literal["auto", "stream", "non_stream"] = "auto"
    providers: list[Provider] = Field(default_factory=lambda: [Provider(id="default")])
    card_author: ModelProfile = Field(default_factory=ModelProfile)
    card_author_config: CardAuthorSettings = Field(default_factory=CardAuthorSettings)
    advisor: AdvisorSettings = Field(default_factory=AdvisorSettings)
    web: WebConfig = Field(default_factory=WebConfig)
    preference_presets: list[PreferencePreset] = Field(default_factory=list)
    local_models: LocalResources = Field(default_factory=LocalResources)
    network: Network = Field(default_factory=Network)
    log_retention_days: int = Field(default=14, ge=1, le=365, strict=True)
    log_max_mib: int = Field(default=512, ge=1, le=16384, strict=True)
    retrieval_cache_max_mib: int = Field(default=256, ge=1, le=16384, strict=True)

    @model_validator(mode="after")
    def unique_presets(self):
        ids = [preset.id for preset in self.preference_presets]
        if len(ids) != len(set(ids)):
            raise ValueError("常用偏好标识不能重复")
        return self


class SettingsDocument(StrictSettings):
    version: Literal[25] = 25
    story: StorySettings = Field(default_factory=StorySettings)
    shared: SharedSettings = Field(default_factory=SharedSettings)
    revisions: dict[str, int] = Field(default_factory=lambda: {"story": 0, "shared": 0})

    def profiles(self):
        for scope, keys in (
            ("story", ("model", "official_model")),
            ("shared", ("card_author",)),
        ):
            for key in keys:
                profile = getattr(getattr(self, scope), key)
                if profile is not None:
                    yield scope, key, profile

    @model_validator(mode="after")
    def references(self):
        ids = [p.id for p in self.shared.providers]
        if not ids or len(ids) != len(set(ids)):
            raise ValueError("提供商不能为空或重复")
        for route in (self.story.media.speech, self.story.media.image):
            validate_channels(route, self.shared.providers)
        for scope, key, profile in self.profiles():
            if not profile.provider_id and not profile.model:
                continue  # An unconfigured channel never borrows another channel's connection.
            if profile.provider_id not in ids:
                raise ValueError("提供商仍被模型配置引用，请先切换对应模型的提供商")
            provider = next(p for p in self.shared.providers if p.id == profile.provider_id)
            if scope != "shared" and provider_channel(provider) != (
                "chatgpt" if key.startswith("official_") else "standard"
            ):
                raise ValueError("模型配置与渠道不一致，请在对应渠道中选择供应商")
        for key in ("model", "official_model"):
            profile = getattr(self.shared.advisor, key)
            if profile is None or (not profile.provider_id and not profile.model):
                continue
            provider = next((p for p in self.shared.providers if p.id == profile.provider_id), None)
            if provider is None:
                raise ValueError("提供商仍被参谋 agent 模型引用，请先切换参谋 agent 的提供商")
            if provider_channel(provider) != ("chatgpt" if key == "official_model" else "standard"):
                raise ValueError("参谋 agent 模型配置与渠道不一致")
        for key in ("model", "official_model"):
            profile = getattr(self.story.novel_research, key)
            if profile is None:
                continue
            provider = next((p for p in self.shared.providers if p.id == profile.provider_id), None)
            if provider is None:
                raise ValueError("提供商仍被资料调查模型引用，请先切换调查模型的提供商")
            if provider_channel(provider) != ("chatgpt" if key == "official_model" else "standard"):
                raise ValueError("资料调查模型与渠道不一致")
        profiles = [profile for _, _, profile in self.profiles()]
        profiles.extend(
            (
                self.shared.advisor.model,
                self.shared.advisor.official_model,
                self.story.novel_research.model,
                self.story.novel_research.official_model,
            )
        )
        official_ids = {p.id for p in self.shared.providers if p.auth_mode == "openai_api_key"}
        for profile in profiles:
            if profile is None:
                continue
            connections = list(profile.provider_connections.items()) + [(profile.provider_id, profile)]
            for provider_id, connection in connections:
                if provider_id in official_ids and (
                    connection.api_mode != "responses" or connection.api_path != "v1"
                ):
                    raise ValueError("OpenAI 官方 API 固定使用 Responses 和 /v1，请修正模型连接配置")
        return self

    def runtime(self, channel=None) -> Settings:
        """Capture the selected channel and shared resources for this operation."""
        agent = self.story
        channel = channel or agent.default_channel
        providers = [p.model_copy(deep=True) for p in self.shared.providers]
        profile = agent.official_model if channel == "chatgpt" else agent.model
        if profile is None or not profile.provider_id:
            placeholder = Provider(
                id="unconfigured-" + channel,
                name="未配置",
                auth_mode="chatgpt_subscription" if channel == "chatgpt" else "api_key",
            )
            if not any(p.id == placeholder.id for p in providers):
                providers.append(placeholder)
            profile = ModelProfile(provider_id=placeholder.id, model="")

        data = agent.model_dump(
            exclude={
                "model",
                "official_model",
                "default_channel",
                "tools",
                "local_runtime",
                "system_prompt",
                "novel_research",
                "media",
            }
        )
        data.update(
            self.shared.model_dump(
                exclude={
                    "advisor": True,
                    "unicode_story_enabled": True,
                    "card_author_config": {"system_prompt"},
                }
            )
        )
        data["card_author_config"]["system_prompt"] = self.shared.card_author_config.system_prompt.current
        # Experiments are attached to the saved session after projecting shared settings.
        data["kemini"]["enabled"] = False
        data["local_models"].update(agent.local_runtime.model_dump())
        data.update(
            **profile.model_dump(exclude={"provider_id", "provider_connections"}),
            providers=providers,
            channel_origin=channel,
            story=profile.model_dump(),
            story_config=agent.tools.model_dump(),
            story_system_prompt=agent.system_prompt.current,
        )
        value = Settings.model_validate(data)
        value._settings_source = self.model_copy(deep=True)
        value._settings_channel = channel
        return value.for_role()


def migrate_model_connections(raw):
    """Move legacy provider protocols/API roots onto each referencing profile."""
    scoped = "version" in raw
    shared = raw.setdefault("shared", {}) if scoped else raw
    providers = shared.get("providers", [])
    originals = {p["id"]: deepcopy(p) for p in providers}
    changed = set()
    profiles = (
        (
            ("story", "model"),
            ("shared", "card_author"),
        )
        if scoped
        else ((None, "story"), (None, "card_author"))
    )
    for scope, name in profiles:
        owner = raw.setdefault(scope, {}) if scope else raw
        if not scoped and name in ("story", "card_author") and owner.get(name) is None:
            continue
        profile = owner.setdefault(name, {})
        provider = originals.get(profile.get("provider_id", "default"), {})
        if "api_mode" not in profile:
            profile["api_mode"] = provider.get("api_mode", "chat_completions")
            changed.add(scope)
        if "api_path" not in profile:
            base = provider.get("base_url", "https://api.openai.com")
            mode = profile["api_mode"]
            path = urlsplit(endpoint(base, mode)).path
            suffix = {
                "chat_completions": "/chat/completions",
                "responses": "/responses",
                "anthropic": "/messages",
                "gemini": "/models/:generateContent",
            }[mode]
            path = path.removesuffix(suffix).strip("/")
            profile["api_path"] = "" if path == ("v1beta" if mode == "gemini" else "v1") else path
            changed.add(scope)
    for provider in providers:
        if provider.get("auth_mode") == "openai_api_key":
            continue  # New official connections never carried legacy provider paths.
        original = deepcopy(provider)
        provider.pop("api_mode", None)
        parsed = urlsplit(provider.get("base_url", "https://api.openai.com"))
        # Query/credential validation still runs on the normalized provider.
        provider["base_url"] = urlunsplit((parsed.scheme, parsed.netloc, "", parsed.query, parsed.fragment))
        if provider != original:
            changed.add("shared" if scoped else None)
    return changed


def rename_card_author(raw):
    """Settings v3 renamed the Card Editor keys to Card Author; values move verbatim."""
    owner = raw.setdefault("shared", {}) if "version" in raw else raw
    for old, new in (("card_editor", "card_author"), ("card_editor_config", "card_author_config")):
        if old in owner:
            owner[new] = owner.pop(old)
    if raw.get("version") == 2:
        raw["version"] = 3
        return True
    return False


def migrate_prompt_choices(raw):
    shared = raw["shared"]
    for owner, key in (
        (raw["story"], "system_prompt"),
        (shared.get("advisor", {}), "system_prompt"),
        (shared.get("card_author_config", {}), "system_prompt"),
    ):
        if isinstance(owner.get(key), str):
            owner[key] = PromptChoices.from_text(owner[key]).model_dump()


def migrate_settings(raw: dict) -> SettingsDocument:
    raw = deepcopy(raw)
    if "version" not in raw or raw["version"] in range(2, 22):
        upgrade_settings_keys(raw)
    renamed = rename_card_author(raw)
    if "version" not in raw:
        # Restore the effective story and card author profiles from flat settings.
        raw["story"] = raw.get("story") or raw.get("foreground", {})
        raw["card_author"] = raw.get("card_author") or raw.get("background", {"reply_tokens": 8192})
    connection_changes = migrate_model_connections(raw)
    if "version" in raw:
        changed_scopes = connection_changes
        if raw["version"] == 3:
            # The active connection stays verbatim; inactive provider records
            # start empty rather than inventing settings for other providers.
            raw["version"] = 4
            changed_scopes.update(("story", "shared"))
        if raw["version"] == 4:
            raw["version"] = 5
            raw.setdefault("shared", {}).setdefault("kemini", KeminiConfig().model_dump())
            changed_scopes.add("shared")
        if raw["version"] == 5:
            raw["version"] = 6
            kemini = raw.setdefault("shared", {}).setdefault("kemini", {})
            defaults = KeminiConfig().model_dump()
            for key in (
                "clear_prompt",
                "info_prompt",
            ):
                kemini.setdefault(key, defaults[key])
            changed_scopes.add("shared")
        if raw["version"] == 6:
            raw["version"] = 7
            kemini = raw.setdefault("shared", {}).setdefault("kemini", {})
            defaults = KeminiConfig().model_dump()
            for key in ("roleplay_prompt", "content_prompt", "output_prompt"):
                kemini.setdefault(key, defaults[key])
            # Upgrade only exact old defaults in storage. Custom text, including
            # deliberately retained tags, remains the visible source of truth.
            for key, old in (
                ("clear_prompt", LEGACY_CLEAR_PROMPT),
                ("info_prompt", LEGACY_INFO_PROMPT),
                ("thinking_prompt", LEGACY_THINKING_PROMPT),
            ):
                if kemini.get(key) == old:
                    kemini[key] = defaults[key]
            changed_scopes.add("shared")
        if raw["version"] == 7:
            raw["version"] = 8
            tools = raw.setdefault("story", {}).setdefault("tools", {})
            for key in ("chapter_tokens", "chapters_total_tokens"):
                tools.setdefault(key, StoryConfig.model_fields[key].default)
            changed_scopes.add("story")
        if raw["version"] == 8:
            # v9 permits an explicit Android CPU/Q8_0 model configuration. Preserve
            # every saved CUDA setting; selecting the mobile preset is a UI action.
            raw["version"] = 9
        if raw["version"] == 9:
            raw["version"] = 10
            raw.setdefault("story", {}).setdefault("tools", {}).setdefault(
                "max_duration_seconds", StoryConfig.model_fields["max_duration_seconds"].default
            )
            changed_scopes.add("story")
        if raw["version"] == 10:
            # Expose the previous per-entry limit without changing saved query timeouts.
            raw["version"] = 11
            raw.setdefault("story", {}).setdefault("retrieval", {}).setdefault("index_timeout", 60)
            changed_scopes.add("story")
        if raw["version"] == 11:
            # New explicit Android devices; preserve every saved CPU/CUDA choice.
            raw["version"] = 12
        if raw["version"] == 12:
            raw["version"] = 13
            if migrate_interleaving_prompts(raw.setdefault("shared", {}).setdefault("kemini", {})):
                changed_scopes.add("shared")
        if raw["version"] == 13:
            raw["version"] = 14
        if renamed:
            changed_scopes.add("shared")
        if migrate_web_settings(raw.setdefault("shared", {})):
            changed_scopes.add("shared")
        providers = {p["id"]: p for p in raw.get("shared", {}).get("providers", [])}
        for provider in providers.values():
            # Rename only the defaults previously assigned to SIWC connections.
            if provider.get("auth_mode") == "chatgpt_subscription" and provider.get("name") in (
                "OpenAI Official",
                "ChatGPT 订阅",
            ):
                provider["name"] = "Sign In With ChatGPT"
                changed_scopes.add("shared")
        standard = next((p["id"] for p in providers.values() if provider_channel(p) == "standard"), "")
        agent = raw.setdefault("story", {})
        if "default_channel" not in agent:
            agent["default_channel"] = "standard"
            profile = agent.get("model", {})
            if provider_channel(providers.get(profile.get("provider_id"), {})) == "chatgpt":
                agent["official_model"] = deepcopy(profile)
                agent["model"] = ModelProfile(provider_id=standard, model="").model_dump()
                agent["default_channel"] = "chatgpt"
            changed_scopes.add("story")
        for scope, key in (
            ("story", "model"),
            ("shared", "card_author"),
        ):
            profile = raw.setdefault(scope, {}).setdefault(key, {})
            if "cache" not in profile:
                changed_scopes.add(scope)
                request = {}
                if profile.get("prompt_cache_key"):
                    request["prompt_cache_key"] = profile["prompt_cache_key"]
                if profile.get("prompt_cache_retention", "default") != "default":
                    request["prompt_cache_retention"] = profile["prompt_cache_retention"]
                profile["cache"] = {
                    "mode": "runtime" if key == "model" else "cloud",
                    "request": request,
                }
            for field in ("prompt_cache_key", "prompt_cache_mode", "prompt_cache_retention"):
                profile.pop(field, None)
        if raw["version"] == 14:
            raw["version"] = 15
            raw.setdefault("story", {}).setdefault("novel_research", NovelConfig().model_dump())
            changed_scopes.add("story")
        if raw["version"] == 15:
            raw["version"] = 16
        if raw["version"] == 16:
            raw["version"] = 17
        if raw["version"] == 17:
            # The reply-completion jump was removed; drop its saved switch so a
            # strict document cannot carry an option that no longer has any effect.
            raw["version"] = 18
            if raw.setdefault("shared", {}).pop("reply_locate_start", None) is not None:
                changed_scopes.add("shared")
        if raw["version"] == 18:
            migrate_prompt_choices(raw)
            raw["version"] = 19
            changed_scopes.update(("story", "shared"))
        if raw["version"] == 19:
            raw["version"] = 20
            raw["shared"]["unicode_story_enabled"] = False
            changed_scopes.add("shared")
        if raw["version"] == 20:
            raw["version"] = 21
            discard_retired_settings(raw)
            retrieval = raw["story"].get("retrieval", {})
            for key in ("query_prompt", "protocol_prompt"):
                retrieval.pop(key, None)
            kemini = raw["shared"].get("kemini", {})
            for key in ("transport_prompt", "tool_description", "content_description"):
                kemini.pop(key, None)
        if raw["version"] == 21:
            raw["version"] = 22
        if raw["version"] == 22:
            raw["version"] = 23
            changed_scopes.add("story")
        if raw["version"] == 23:
            raw["version"] = 24
            changed_scopes.add("story")
        if raw["version"] == 24:
            story = raw.setdefault("story", {})
            story["media"] = upgrade_defaults(
                story.get("media", {}), raw.get("shared", {}).get("providers", [])
            ).model_dump()
            raw["version"] = 25
            changed_scopes.add("story")
        doc = SettingsDocument.model_validate(raw)
        # Story and Advisor update the bundled default in slot 1.
        for scope, option, legacy_hashes, default in (
            ("story", doc.story.system_prompt.options[0], LEGACY_STORY_PROMPT_HASHES, STORY_PROMPT),
            (
                "shared",
                doc.shared.advisor.system_prompt.options[0],
                LEGACY_ADVISOR_PROMPT_HASHES,
                ADVISOR_PROMPT,
            ),
        ):
            if fingerprint(option.text) in legacy_hashes:
                option.text = default
                changed_scopes.add(scope)
        # Any Card Author slot can be enabled after upgrade. Migrate known
        # defaults and resource references in storage before the tool reads them.
        for option in doc.shared.card_author_config.system_prompt.options:
            text = (
                CARD_AUTHOR_PROMPT
                if fingerprint(option.text) in LEGACY_CARD_AUTHOR_PROMPT_HASHES
                else upgrade_card_author_guide_references(option.text)
            )
            if text != option.text:
                option.text = text
                changed_scopes.add("shared")
        for scope in changed_scopes:
            doc.revisions[scope] = doc.revisions.get(scope, 0) + 1
        return doc
    old = Settings.model_validate(raw)
    values = old.model_dump()
    story = {
        **{key: values[key] for key in AgentSettings.model_fields if key in values},
        "local_runtime": {key: values["local_models"][key] for key in RetrievalRuntime.model_fields},
        "model": old.story.model_dump(),
        "tools": old.story_config.model_dump(),
        "system_prompt": old.story_system_prompt,
    }
    shared = {key: values[key] for key in SharedSettings.model_fields if key in values and key != "advisor"}
    shared["local_models"] = {key: values["local_models"][key] for key in LocalResources.model_fields}
    for original, profile in (
        (raw["story"], story["model"]),
        (raw["card_author"], shared["card_author"]),
    ):
        if "cache" not in original:
            profile.pop("cache", None)
            profile.update(
                prompt_cache_key=original.get("prompt_cache_key"),
                prompt_cache_retention=original.get("prompt_cache_retention", "default"),
            )
    scoped = {"version": 20, "story": story, "shared": shared}
    migrate_prompt_choices(scoped)
    return migrate_settings(scoped)


def from_runtime(value: Settings) -> SettingsDocument:
    """Internal maintenance/test callers may edit a projection, never write flat storage."""
    if value._settings_source is None:
        return migrate_settings(value.model_dump())
    doc = value._settings_source.model_copy(deep=True)
    data = value.model_dump()
    doc.story.system_prompt.replace_current(value.story_system_prompt)
    doc.shared.card_author_config.system_prompt.replace_current(value.card_author_config.system_prompt)
    agent = doc.story.model_dump()
    for key in AgentSettings.model_fields:
        if key in data:
            agent[key] = data[key]
    key = "official_model" if value._settings_channel == "chatgpt" else "model"
    agent[key] = data["story"]
    agent["tools"] = data["story_config"]
    agent["local_runtime"] = {key: data["local_models"][key] for key in RetrievalRuntime.model_fields}
    doc.story = StorySettings.model_validate(agent)
    shared = {
        key: deepcopy(data[key])
        for key in SharedSettings.model_fields
        if key not in {"advisor", "unicode_story_enabled"}
    }
    shared["unicode_story_enabled"] = doc.shared.unicode_story_enabled
    shared["advisor"] = doc.shared.advisor.model_dump()
    shared["card_author_config"]["system_prompt"] = doc.shared.card_author_config.system_prompt.model_dump()
    shared["providers"] = [p for p in shared["providers"] if not p["id"].startswith("unconfigured-")]
    shared["local_models"] = {key: deepcopy(data["local_models"][key]) for key in LocalResources.model_fields}
    doc.shared = SharedSettings.model_validate(shared)
    return SettingsDocument.model_validate(doc.model_dump())
