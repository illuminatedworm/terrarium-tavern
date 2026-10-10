"""Persisted settings; runtime projections are disposable, never the source of truth."""
import re
from copy import deepcopy
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

from tavern.character.editor_config import CardEditorConfig
from tavern.core.cache_policy import endpoint, fingerprint, runtime_cache
from tavern.core.channels import Channel, provider_channel
from tavern.core.models import ContextRecipe, PreferencePreset, Settings
from tavern.core.prompts import (
    DEFAULT_LOCAL_INSTRUCTION,
    DEFAULT_MAINTENANCE_PROMPT,
    DEFAULT_MEMORY_PROMPT,
    DEFAULT_PREFERENCE_PROMPT,
    DEFAULT_PROBE_MARKER_PROMPT,
    DEFAULT_PROBE_PROMPT,
    DEFAULT_REGENERATE_PROMPT,
    DEFAULT_RERANKER_PROMPT,
    DEFAULT_RETRIEVAL_PROTOCOL,
    DEFAULT_SYSTEM_PROMPT,
    DEFAULT_TRACKER_PROMPT,
    LEGACY_DEFAULT_RETRIEVAL_PROTOCOL,
)
from tavern.core.update_policy import Policy
from tavern.full_agent.config import FULL_AGENT_PROMPT, FullAgentConfig
from tavern.search.retrieval_config import LocalModel, ModelProfile, Network, Provider, RetrievalSettings

LEGACY_FULL_AGENT_PROMPT_HASHES = {
    "d72b35768688226ad359f3fdc8dcb04286633b4da808a8ee6aa70f7f47e8e00b",
    "4bc871cd5021ed1be8b86b36fd4f072ecd6cbd8e017cd074eaa1107f47af257e",
    "7ee23c619af1a6cb406489c4c9316a8457e2161d68d1ef5feba332c8fdb267ea",
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
    # Legacy setting retained for stored profiles; selected entries are always complete.
    lore_max_chars: int = Field(default=3000, ge=1, le=2000000, strict=True)
    policy: Policy = "automatic"
    preference_prompt: str = DEFAULT_PREFERENCE_PROMPT
    regenerate_prompt: str = DEFAULT_REGENERATE_PROMPT
    probe_system_prompt: str = DEFAULT_PROBE_PROMPT
    probe_marker_prompt: str = DEFAULT_PROBE_MARKER_PROMPT


class FullSettings(AgentSettings):
    model: ModelProfile = Field(default_factory=lambda: ModelProfile(cache=runtime_cache()))
    official_model: ModelProfile | None = None
    tools: FullAgentConfig = Field(default_factory=FullAgentConfig)
    system_prompt: str = FULL_AGENT_PROMPT

    @model_validator(mode="before")
    @classmethod
    def default_writer_cache(cls, value):
        if isinstance(value, dict) and isinstance(value.get("model"), dict) and "cache" not in value["model"]:
            value = deepcopy(value)
            value["model"]["cache"] = {"mode": "runtime"}
        return value


class HalfSettings(AgentSettings):
    foreground: ModelProfile = Field(default_factory=lambda: ModelProfile(cache=runtime_cache()))
    background: ModelProfile = Field(default_factory=lambda: ModelProfile(reply_tokens=1024))
    official_foreground: ModelProfile | None = None
    official_background: ModelProfile | None = None
    system_prompt: str = DEFAULT_SYSTEM_PROMPT
    tracker_system_prompt: str = DEFAULT_TRACKER_PROMPT
    tracker_max_steps: int = Field(default=8, ge=2, le=100, strict=True)
    lore_injection_mode: Literal["conversation", "st_top"] = "conversation"
    context_recipe: ContextRecipe = Field(default_factory=ContextRecipe)
    chunk_mode: Literal["messages", "tokens", "off"] = "messages"
    chunk_size: int = Field(default=10, ge=2, le=100, strict=True)
    history_message_cap: int = Field(default=40, ge=8, le=1000, strict=True)
    memory_interval_turns: int = Field(default=5, ge=1, le=100, strict=True)
    context_pressure_trigger: float = Field(default=0.85, ge=0.1, le=0.99)
    context_pressure_target: float = Field(default=0.70, ge=0.05, le=0.98)
    daily_context_budget: int = Field(default=98304, ge=2048, le=2000000, strict=True)
    chunk_max_tokens: int = Field(default=6000, ge=512, le=2000000, strict=True)
    memory_timeout: int = Field(default=120, ge=5, le=180, strict=True)
    memory_input_tokens: int = Field(default=32768, ge=2048, le=2000000, strict=True)
    memory_evidence_tokens: int = Field(default=4096, ge=0, le=32768, strict=True)
    memory_output_tokens: int = Field(default=4096, ge=256, le=65536, strict=True)
    memory_capacity_tokens: int = Field(default=4096, ge=512, le=65536, strict=True)
    memory_current_max_records: int = Field(default=5, ge=1, le=100, strict=True)
    memory_episode_cooldown_rounds: int = Field(default=20, ge=0, le=10000, strict=True)
    memory_review_enabled: bool = Field(default=True, strict=True)

    @model_validator(mode="before")
    @classmethod
    def default_writer_cache(cls, value):
        if isinstance(value, dict) and isinstance(value.get("foreground"), dict) and "cache" not in value["foreground"]:
            value = deepcopy(value)
            value["foreground"]["cache"] = {"mode": "runtime"}
        return value

    @model_validator(mode="after")
    def linked(self):
        blocks = self.context_recipe.blocks
        if any(not block.id.strip() or not block.source.strip() for block in blocks):
            raise ValueError("上下文块标识和来源不能为空")
        if len({block.id for block in blocks}) != len(blocks):
            raise ValueError("上下文块标识不能重复")
        if self.memory_output_tokens + 2048 >= self.background.context_tokens:
            raise ValueError("记忆输出上限必须为后台模型输入留出安全余量")
        if self.context_pressure_target >= self.context_pressure_trigger:
            raise ValueError("压力裁剪目标必须小于压力触发阈值")
        if self.chunk_mode == "tokens" and self.chunk_max_tokens > self.foreground.context_tokens - self.foreground.reply_tokens - 2048:
            raise ValueError("历史预算不能超过正文模型的可用输入容量")
        return self


class LocalResources(StrictSettings):
    embedding: LocalModel = Field(default_factory=lambda: LocalModel(model="Qwen/Qwen3-Embedding-0.6B"))
    reranker: LocalModel = Field(default_factory=lambda: LocalModel(model="Qwen/Qwen3-Reranker-0.6B"))

    @model_validator(mode="after")
    def supported(self):
        from tavern.search.retrieval_config import LocalModels
        LocalModels.model_validate(self.model_dump())
        return self


class SharedSettings(StrictSettings):
    providers: list[Provider] = Field(default_factory=lambda: [Provider(id="default")])
    card_editor: ModelProfile = Field(default_factory=lambda: ModelProfile(reply_tokens=1024))
    card_editor_config: CardEditorConfig = Field(default_factory=CardEditorConfig)
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
    version: Literal[2] = 2
    full_agent: FullSettings = Field(default_factory=FullSettings)
    half_agent: HalfSettings = Field(default_factory=HalfSettings)
    shared: SharedSettings = Field(default_factory=SharedSettings)
    revisions: dict[str, int] = Field(default_factory=lambda: {k: 0 for k in ("full_agent", "half_agent", "shared")})

    def profiles(self):
        for scope, keys in (("full_agent", ("model", "official_model")),
                            ("half_agent", ("foreground", "background", "official_foreground", "official_background")),
                            ("shared", ("card_editor",))):
            for key in keys:
                profile = getattr(getattr(self, scope), key)
                if profile is not None:
                    yield scope, key, profile

    @model_validator(mode="after")
    def references(self):
        ids = [p.id for p in self.shared.providers]
        if not ids or len(ids) != len(set(ids)):
            raise ValueError("提供商不能为空或重复")
        for scope, key, profile in self.profiles():
            if not profile.provider_id and not profile.model:
                continue  # An unconfigured channel never borrows another channel's connection.
            if profile.provider_id not in ids:
                raise ValueError("提供商仍被模型配置引用，请先切换对应模型的提供商")
            provider = next(p for p in self.shared.providers if p.id == profile.provider_id)
            if scope != "shared" and provider_channel(provider) != ("chatgpt" if key.startswith("official_") else "standard"):
                raise ValueError("模型配置与渠道不一致，请在对应渠道中选择供应商")
        return self

    def runtime(self, mode="half_agent", channel=None) -> Settings:
        """Each projection is a deep operation snapshot, including its role changes."""
        agent = getattr(self, mode).model_copy(deep=True)
        channel = channel or agent.default_channel
        providers = [p.model_copy(deep=True) for p in self.shared.providers]
        def selected(owner, key):
            profile = getattr(owner, ("official_" if channel == "chatgpt" else "") + key)
            if profile is not None and profile.provider_id:
                return profile.model_copy(deep=True)
            missing = "unconfigured-" + channel
            if not any(p.id == missing for p in providers):
                providers.append(Provider(id=missing, name="未配置", auth_mode=(
                    "chatgpt_subscription" if channel == "chatgpt" else "api_key")))
            return ModelProfile(provider_id=missing, model="")
        full_profile = selected(self.full_agent, "model")
        if mode == "full_agent":
            agent.model = full_profile
        else:
            agent.foreground, agent.background = selected(agent, "foreground"), selected(agent, "background")
        data = agent.model_dump(exclude={"model", "tools", "local_runtime", "official_model",
                                        "official_foreground", "official_background", "default_channel"})
        data["runtime_mode"] = mode
        data.update(self.shared.model_dump())
        data.update(providers=providers, channel_origin=channel)
        data["local_models"].update(agent.local_runtime.model_dump())
        data.update(full_agent=full_profile.model_dump(),
                    full_agent_config=self.full_agent.tools.model_dump(),
                    full_agent_system_prompt=self.full_agent.system_prompt)
        if mode == "full_agent":
            # Script adapters use role-shaped access too; they must never reach Half Agent.
            data.update(foreground=agent.model.model_dump(), background=agent.model.model_dump(),
                        lore_injection_mode="conversation")
        profile = agent.model if mode == "full_agent" else agent.foreground
        data.update(profile.model_dump(exclude={"provider_id"}))
        # Runtime-only Half Agent fields are irrelevant to a Full Agent request.
        if mode == "full_agent":
            data.update(memory_output_tokens=256, chunk_mode="off")
        value = Settings.model_validate(data)
        value._settings_source = self.model_copy(deep=True)
        value._settings_mode = mode
        value._settings_channel = channel
        if mode == "half_agent":
            value._full_runtime = self.runtime("full_agent", channel)
        return value.for_role("full_agent" if mode == "full_agent" else "foreground")


def migrate_model_connections(raw):
    """Move legacy provider protocols/API roots onto each referencing profile."""
    scoped = "version" in raw
    shared = raw.setdefault("shared", {}) if scoped else raw
    providers = shared.get("providers", [])
    originals = {p["id"]: deepcopy(p) for p in providers}
    changed = set()
    profiles = (("full_agent", "model"), ("half_agent", "foreground"),
                ("half_agent", "background"), ("shared", "card_editor")) if scoped else (
                (None, "foreground"), (None, "background"), (None, "full_agent"), (None, "card_editor"))
    for scope, name in profiles:
        owner = raw.setdefault(scope, {}) if scope else raw
        if not scoped and name in ("full_agent", "card_editor") and owner.get(name) is None:
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
            suffix = {"chat_completions": "/chat/completions", "responses": "/responses",
                      "anthropic": "/messages", "gemini": "/models/:generateContent"}[mode]
            path = path.removesuffix(suffix).strip("/")
            profile["api_path"] = "" if path == ("v1beta" if mode == "gemini" else "v1") else path
            changed.add(scope)
    for provider in providers:
        original = deepcopy(provider)
        provider.pop("api_mode", None)
        parsed = urlsplit(provider.get("base_url", "https://api.openai.com"))
        # Query/credential validation still runs on the normalized provider.
        provider["base_url"] = urlunsplit((parsed.scheme, parsed.netloc, "", parsed.query, parsed.fragment))
        if provider != original:
            changed.add("shared" if scoped else None)
    return changed


def migrate_tracker_settings(agent):
    """Fold legacy content policies verbatim into one editable Tracker prompt."""
    changed = False
    custom = []
    for key, title, default in (("memory_system_prompt", "记忆整理时", DEFAULT_MEMORY_PROMPT),
                                ("maintenance_system_prompt", "设定维护时", DEFAULT_MAINTENANCE_PROMPT)):
        if key in agent:
            old = agent.pop(key)
            changed = True
            if isinstance(old, str) and old != default:
                custom.append(f"## {title}\n{old}")
    if "tracker_system_prompt" not in agent or custom:
        agent["tracker_system_prompt"] = "\n\n".join([agent.get("tracker_system_prompt", DEFAULT_TRACKER_PROMPT), *custom])
        changed = True
    if "tracker_max_steps" not in agent:
        agent["tracker_max_steps"] = 8
        changed = True
    return changed


def migrate_settings(raw: dict) -> SettingsDocument:
    raw = deepcopy(raw)
    connection_changes = migrate_model_connections(raw)
    if "version" in raw:
        changed_scopes = connection_changes
        providers = {p["id"]: p for p in raw.get("shared", {}).get("providers", [])}
        standard = next((p["id"] for p in providers.values() if provider_channel(p) == "standard"), "")
        for scope, keys in (("full_agent", ("model",)), ("half_agent", ("foreground", "background"))):
            agent = raw.setdefault(scope, {})
            if "default_channel" not in agent:
                agent["default_channel"] = "standard"
                for key in keys:
                    profile = agent.get(key, {})
                    if provider_channel(providers.get(profile.get("provider_id"), {})) == "chatgpt":
                        agent["official_" + key] = deepcopy(profile)
                        agent[key] = ModelProfile(provider_id=standard, model="").model_dump()
                        if key in ("model", "foreground"):
                            agent["default_channel"] = "chatgpt"
                changed_scopes.add(scope)
        if migrate_tracker_settings(raw.setdefault("half_agent", {})):
            changed_scopes.add("half_agent")
        # Only application prompt settings use the human participant terminology.
        # Character documents, persona names, dialogue and user input stay verbatim.
        for scope in ("full_agent", "half_agent"):
            agent = raw.setdefault(scope, {})
            groups = [(agent, ("system_prompt", "memory_system_prompt", "maintenance_system_prompt",
                               "preference_prompt", "regenerate_prompt", "probe_system_prompt", "probe_marker_prompt")),
                      (agent.get("retrieval", {}), ("query_prompt", "protocol_prompt")),
                      (agent.get("local_runtime", {}), ("instruction_prompt", "reranker_prompt"))]
            for values, fields in groups:
                for field in fields:
                    old = values.get(field)
                    if not isinstance(old, str):
                        continue
                    new = re.sub(r"\bplayer\b", "user", old.replace("玩家", "用户"), flags=re.IGNORECASE)
                    if new != old:
                        values[field] = new
                        changed_scopes.add(scope)
        # Upgrade only the exact previous application default. Custom story
        # instructions remain user-owned; tool descriptions carry the new rule too.
        if fingerprint(raw["full_agent"].get("system_prompt")) in LEGACY_FULL_AGENT_PROMPT_HASHES:
            raw["full_agent"]["system_prompt"] = FULL_AGENT_PROMPT
            changed_scopes.add("full_agent")
        for scope, key in (("full_agent", "model"), ("half_agent", "foreground"),
                           ("half_agent", "background"), ("shared", "card_editor")):
            profile = raw.setdefault(scope, {}).setdefault(key, {})
            if "cache" not in profile:
                changed_scopes.add(scope)
                request = {}
                if profile.get("prompt_cache_key"):
                    request["prompt_cache_key"] = profile["prompt_cache_key"]
                if profile.get("prompt_cache_retention", "default") != "default":
                    request["prompt_cache_retention"] = profile["prompt_cache_retention"]
                profile["cache"] = {"mode": "runtime" if key in ("model", "foreground") else "cloud",
                                    "request": request}
            for field in ("prompt_cache_key", "prompt_cache_mode", "prompt_cache_retention"):
                profile.pop(field, None)
        for scope in changed_scopes:
            revisions = raw.setdefault("revisions", {name: 0 for name in ("full_agent", "half_agent", "shared")})
            revisions[scope] = revisions.get(scope, 0) + 1
        return SettingsDocument.model_validate(raw)
    migrate_tracker_settings(raw)
    old = Settings.model_validate(raw)
    if fingerprint(old.full_agent_system_prompt) in LEGACY_FULL_AGENT_PROMPT_HASHES:
        old.full_agent_system_prompt = FULL_AGENT_PROMPT
    if old.retrieval.protocol_prompt == LEGACY_DEFAULT_RETRIEVAL_PROTOCOL:
        old.retrieval.protocol_prompt = DEFAULT_RETRIEVAL_PROTOCOL
    values = old.model_dump()
    half = {key: deepcopy(values[key]) for key in HalfSettings.model_fields if key in values}
    common = {key: deepcopy(values[key]) for key in AgentSettings.model_fields if key in values}
    local = {key: deepcopy(values["local_models"][key]) for key in RetrievalRuntime.model_fields}
    half["local_runtime"] = deepcopy(local)
    full = {**common, "local_runtime": deepcopy(local), "model": (old.full_agent or old.foreground).model_dump(),
            "tools": old.full_agent_config.model_dump(), "system_prompt": old.full_agent_system_prompt}
    shared = {key: deepcopy(values[key]) for key in SharedSettings.model_fields if key in values}
    shared["card_editor"] = (old.card_editor or old.background).model_dump()
    shared["local_models"] = {key: deepcopy(values["local_models"][key]) for key in LocalResources.model_fields}
    result = SettingsDocument(full_agent=full, half_agent=half, shared=shared).model_dump()
    for scope, key, profile in (("full_agent", "model", old.full_agent or old.foreground),
                                ("half_agent", "foreground", old.foreground),
                                ("half_agent", "background", old.background),
                                ("shared", "card_editor", old.card_editor or old.background)):
        original = (raw.get("full_agent") or raw.get("foreground", {}) if scope == "full_agent" else
                    raw.get("card_editor") or raw.get("background", {}) if key == "card_editor" else
                    raw.get(key, {}))
        if "cache" not in original:
            result[scope][key].pop("cache", None)
            result[scope][key].update(prompt_cache_key=profile.prompt_cache_key,
                                      prompt_cache_retention=profile.prompt_cache_retention)
    return migrate_settings(result)


def from_runtime(value: Settings) -> SettingsDocument:
    """Internal maintenance/test callers may edit a projection, never write flat storage."""
    if value._settings_source is None:
        return migrate_settings(value.model_dump())
    doc = value._settings_source.model_copy(deep=True)
    data = value.model_dump()
    mode = value._settings_mode
    agent = getattr(doc, mode).model_dump()
    for key in agent:
        if key in data and key not in {"model", "tools", "local_runtime"}:
            agent[key] = deepcopy(data[key])
    agent["local_runtime"] = {key: deepcopy(data["local_models"][key]) for key in RetrievalRuntime.model_fields}
    if mode == "full_agent":
        key = "official_model" if value._settings_channel == "chatgpt" else "model"
        agent.update({key: data["full_agent"], "tools": data["full_agent_config"], "system_prompt": data["full_agent_system_prompt"]})
    else:
        for role in ("foreground", "background"):
            key = "official_" + role if value._settings_channel == "chatgpt" else role
            agent[key] = data[role]
            if value._settings_channel == "chatgpt":
                agent[role] = getattr(doc.half_agent, role).model_dump()
        setattr(doc.full_agent, "official_model" if value._settings_channel == "chatgpt" else "model",
                value.full_agent.model_copy(deep=True))
        doc.full_agent.tools = value.full_agent_config.model_copy(deep=True)
        doc.full_agent.system_prompt = value.full_agent_system_prompt
    setattr(doc, mode, (FullSettings if mode == "full_agent" else HalfSettings).model_validate(agent))
    shared = {key: deepcopy(data[key]) for key in SharedSettings.model_fields}
    shared["providers"] = [p for p in shared["providers"] if not p["id"].startswith("unconfigured-")]
    shared["local_models"] = {key: deepcopy(data["local_models"][key]) for key in LocalResources.model_fields}
    doc.shared = SharedSettings.model_validate(shared)
    return SettingsDocument.model_validate(doc.model_dump())
