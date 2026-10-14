from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator, model_validator

from tavern.character.editor_config import CardEditorConfig
from tavern.core.cache_policy import CacheSettings, runtime_cache
from tavern.core.message_adapter import MessageAdapterConfig, infer_type, type_definition
from tavern.core.prompts import (
    DEFAULT_MAINTENANCE_PROMPT,
    DEFAULT_MEMORY_PROMPT,
    DEFAULT_PREFERENCE_PROMPT,
    DEFAULT_PROBE_MARKER_PROMPT,
    DEFAULT_PROBE_PROMPT,
    DEFAULT_REGENERATE_PROMPT,
    DEFAULT_SYSTEM_PROMPT,
    DEFAULT_TRACKER_PROMPT,
)
from tavern.core.update_policy import Policy
from tavern.full_agent.config import FULL_AGENT_PROMPT, FullAgentConfig
from tavern.search.retrieval_config import (
    LocalModels,
    ModelProfile,
    Network,
    Provider,
    RetrievalSettings,
    api_root,
)

LoreInjectionMode = Literal["conversation", "st_top"]
ReasoningEffort = Literal["default", "none", "minimal", "low", "medium", "high", "xhigh", "max"]


class ContextBlock(BaseModel):
    id: str
    source: str
    role: Literal["system", "developer", "user", "assistant"] = "system"
    message_type: str = ""
    enabled: bool = True
    content: str = ""
    depth: int | None = Field(default=None, ge=0)
    priority: int = 50
    budget: int | None = Field(default=None, ge=1)
    required: bool = False
    condition: str = ""

    @model_validator(mode="after")
    def known_message_type(self):
        if not self.message_type and self.source != "history":
            self.message_type = infer_type({"context_source": self.source, "role": self.role})
        if self.message_type:
            type_definition(self.message_type)
        return self


class ContextRecipe(BaseModel):
    version: int = 1
    blocks: list[ContextBlock] = Field(
        default_factory=lambda: [
            ContextBlock(id="rules", source="rules", required=True, priority=100),
            ContextBlock(id="lore_before", source="lore_before"),
            ContextBlock(id="card_system_prompt", source="system_prompt", required=True, priority=100),
            ContextBlock(id="character", source="description", required=True, priority=100),
            ContextBlock(id="card_personality", source="personality", required=True, priority=100),
            ContextBlock(id="card_scenario", source="scenario", required=True, priority=100),
            ContextBlock(id="persona", source="persona"),
            ContextBlock(id="lore_after", source="lore_after"),
            ContextBlock(id="examples", source="examples", role="assistant", priority=30),
            ContextBlock(id="history", source="history", priority=60),
            ContextBlock(id="input", source="input", role="user", required=True, priority=100),
            ContextBlock(id="post", source="post", priority=90),
            ContextBlock(id="author_note", source="author_note", priority=80),
            ContextBlock(id="memory", source="memory", priority=70),
            ContextBlock(id="state", source="state", required=True, priority=100),
        ]
    )

class PreferencePreset(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=80)
    content: str = Field(min_length=1)

    @field_validator("id", "name", "content")
    @classmethod
    def nonempty(cls, value):
        if not value.strip():
            raise ValueError("常用偏好的标识、名称和内容不能为空")
        return value


class Settings(BaseModel):
    """Operation-local runtime configuration (persist SettingsDocument instead)."""
    _full_runtime: Any = PrivateAttr(default=None)
    _settings_source: Any = PrivateAttr(default=None)
    _settings_mode: str = PrivateAttr(default="half_agent")
    _settings_channel: str = PrivateAttr(default="standard")
    _adapter_loader: Any = PrivateAttr(default=None)
    _adapter_lane: str = PrivateAttr(default="foreground")
    _chatgpt: Any = PrivateAttr(default=None)
    _auth_generation: str = PrivateAttr(default="")
    _auth_generations: dict[str, str] = PrivateAttr(default_factory=dict)
    runtime_mode: Literal["half_agent", "full_agent"] = "half_agent"
    channel_origin: Literal["standard", "chatgpt"] | None = None
    context_strategy: Literal["new_context", "openai_compact"] = "new_context"
    compact_auto: bool = True
    providers: list[Provider] = Field(default_factory=lambda: [Provider(id="default")])
    # Stable persisted/API keys: foreground = Writer, background = Tracker.
    foreground: ModelProfile = Field(default_factory=lambda: ModelProfile(cache=runtime_cache()))
    background: ModelProfile = Field(default_factory=lambda: ModelProfile(reply_tokens=1024))
    card_editor: ModelProfile | None = None
    advisor: ModelProfile | None = None
    card_editor_config: CardEditorConfig = Field(default_factory=CardEditorConfig)
    preference_presets: list[PreferencePreset] = Field(default_factory=list)
    full_agent: ModelProfile | None = None
    full_agent_config: FullAgentConfig = Field(default_factory=FullAgentConfig)
    full_agent_system_prompt: str = FULL_AGENT_PROMPT
    retrieval: RetrievalSettings = Field(default_factory=RetrievalSettings)
    local_models: LocalModels = Field(default_factory=LocalModels)
    network: Network = Field(default_factory=Network)
    model_request_mode: Literal["auto", "stream", "non_stream"] = "auto"

    def for_role(self, role="foreground"):
        if role == "full_agent" and self._full_runtime is not None:
            return self._full_runtime.model_copy(deep=True)
        profile = (self.background if role == "card_editor" and self.card_editor is None else
                   self.foreground if role == "full_agent" and self.full_agent is None else getattr(self, role))
        if profile is None:
            return self.model_copy(deep=True)
        provider = next((p for p in self.providers if p.id == profile.provider_id), None)
        if provider is None and not profile.provider_id and not profile.model:
            return self.model_copy(deep=True, update={"model": "", "api_key": "", "channel_origin": None if role == "card_editor" else self.channel_origin})
        if provider is None:
            raise ValueError("所选模型引用了不存在的提供商")
        result = self.model_copy(deep=True, update={**profile.model_dump(exclude={"provider_id"}),
            "cache": profile.cache.model_copy(deep=True),
            "message_adapter": profile.message_adapter.model_copy(deep=True),
            "base_url": api_root(provider.base_url, profile.api_mode, profile.api_path), "api_key": provider.api_key,
            "proxy": self.network.proxy, "use_proxy": self.network.api_proxy and provider.use_proxy,
            "auth_mode": provider.auth_mode, "chatgpt_account_id": provider.chatgpt_account_id})
        if provider.auth_mode == "chatgpt_subscription":
            result.base_url, result.api_mode, result.api_path = "https://api.openai.com/v1", "responses", "v1"
            result.api_key = ""
            result._auth_generation = (self._auth_generations[provider.chatgpt_account_id]
                if provider.chatgpt_account_id in self._auth_generations else
                self._chatgpt.generation(provider.chatgpt_account_id) if self._chatgpt else "")
        if role == "card_editor":
            result.channel_origin = None
        result._adapter_lane = "full_agent" if self.runtime_mode == "full_agent" and role in ("foreground", "background") else role
        return result

    base_url: str = "https://api.openai.com/v1"
    model: str = ""
    tokenizer: Literal["auto", "o200k_base", "cl100k_base"] = "auto"
    api_key: str = Field(default="", exclude=True)
    auth_mode: Literal["api_key", "chatgpt_subscription"] = "api_key"
    chatgpt_account_id: str | None = None
    system_prompt: str = DEFAULT_SYSTEM_PROMPT
    # Compatibility fields for old flat profiles; production Tracker uses the
    # unified prompt below. Persisted HalfSettings migrates these fields away.
    memory_system_prompt: str = DEFAULT_MEMORY_PROMPT
    maintenance_system_prompt: str = DEFAULT_MAINTENANCE_PROMPT
    tracker_system_prompt: str = DEFAULT_TRACKER_PROMPT
    tracker_max_steps: int = Field(default=8, ge=2, le=100, strict=True)
    preference_prompt: str = DEFAULT_PREFERENCE_PROMPT
    regenerate_prompt: str = DEFAULT_REGENERATE_PROMPT
    probe_system_prompt: str = DEFAULT_PROBE_PROMPT
    probe_marker_prompt: str = DEFAULT_PROBE_MARKER_PROMPT
    lore_recursive: bool = Field(default=True, strict=True)
    lore_recursion_depth: int = Field(default=1, ge=0, le=16, strict=True)
    lore_max_entries: int = Field(default=5, ge=1, le=1000, strict=True)
    # Legacy setting retained for stored profiles; selected entries are always complete.
    lore_max_chars: int = Field(default=3000, ge=1, le=2000000, strict=True)
    proxy: str = ""
    use_proxy: bool = True
    api_mode: Literal["chat_completions", "responses", "anthropic", "gemini"] = "chat_completions"
    message_adapter: MessageAdapterConfig = Field(default_factory=MessageAdapterConfig)
    api_path: str | None = None
    cache: CacheSettings = Field(default_factory=CacheSettings)
    cache_scope: str = ""
    cache_window: str = ""
    temperature: float | None = Field(default=0.8, ge=0, le=2)
    top_p: float | None = Field(default=None, ge=0, le=1)
    top_k: int | None = Field(default=None, ge=0)
    frequency_penalty: float | None = Field(default=None, ge=-2, le=2)
    presence_penalty: float | None = Field(default=None, ge=-2, le=2)
    prompt_cache_key: str | None = Field(default=None, max_length=256)
    prompt_cache_mode: Literal["default", "no_write", "instructions"] = "default"
    prompt_cache_retention: Literal["default", "in_memory", "24h"] = "default"
    context_tokens: int = Field(default=32768, ge=2048, le=2000000, strict=True)
    reply_tokens: int = Field(default=2048, ge=128, le=65536, strict=True)
    policy: Policy = "automatic"
    lore_injection_mode: LoreInjectionMode = "conversation"
    capability: Literal["unknown", "accepted", "unsupported"] = "unknown"
    capability_key: str = ""
    reasoning_effort: ReasoningEffort = "default"
    reasoning_format: Literal["reasoning_effort", "reasoning"] = "reasoning_effort"
    chunk_mode: Literal["messages", "tokens", "off"] = "messages"
    chunk_size: int = Field(default=10, ge=2, le=100, strict=True)
    history_message_cap: int = Field(default=40, ge=8, le=1000, strict=True)
    memory_interval_turns: int = Field(default=5, ge=1, le=100, strict=True)
    context_pressure_trigger: float = Field(default=0.85, ge=0.1, le=0.99)
    context_pressure_target: float = Field(default=0.70, ge=0.05, le=0.98)
    daily_context_budget: int = Field(default=98304, ge=2048, le=2000000, strict=True)
    log_retention_days: int = Field(default=14, ge=1, le=365, strict=True)
    log_max_mib: int = Field(default=512, ge=1, le=16384, strict=True)
    retrieval_cache_max_mib: int = Field(default=256, ge=1, le=16384, strict=True)
    chunk_max_tokens: int = Field(default=6000, ge=512, le=2000000, strict=True)
    memory_timeout: int = Field(default=120, ge=5, le=180, strict=True)
    memory_input_tokens: int = Field(default=32768, ge=2048, le=2000000, strict=True)
    memory_evidence_tokens: int = Field(default=4096, ge=0, le=32768, strict=True)
    memory_output_tokens: int = Field(default=4096, ge=256, le=65536, strict=True)
    memory_capacity_tokens: int = Field(default=4096, ge=512, le=65536, strict=True)
    memory_current_max_records: int = Field(default=5, ge=1, le=100, strict=True)
    memory_episode_cooldown_rounds: int = Field(default=20, ge=0, le=10000, strict=True)
    memory_review_enabled: bool = Field(default=True, strict=True)
    context_recipe: ContextRecipe = Field(default_factory=ContextRecipe)


    @model_validator(mode="after")
    def validate_linked_settings(self):
        if self.runtime_mode == "half_agent" and self.memory_output_tokens + 2048 >= self.background.context_tokens:
            raise ValueError("记忆输出上限必须为后台模型的输入及安全余量留出空间")
        if len({p.id for p in self.providers}) != len(self.providers) or not self.providers:
            raise ValueError("提供商不能为空或重复")
        for profile in (self.foreground, self.background, self.card_editor, self.full_agent):
            if profile and (profile.provider_id or profile.model) and profile.provider_id not in {p.id for p in self.providers}:
                raise ValueError("所选模型引用了不存在的提供商")
        if self.context_tokens <= self.reply_tokens + 2048:
            raise ValueError("模型上下文窗口必须大于单次输出上限/维护输出预算加 2048 tokens 的安全余量")
        if self.context_pressure_target >= self.context_pressure_trigger:
            raise ValueError("压力裁剪目标必须小于压力触发阈值")
        # Lore messages share the window. Batch parity and timer arithmetic can
        # no longer prove coverage; the planner checks actual exchange boundaries.
        if self.chunk_mode == "tokens" and self.chunk_max_tokens > self.context_tokens - self.reply_tokens - 2048:
            raise ValueError("历史预算不能超过模型上下文窗口减单次输出上限及 2048 tokens 安全余量")
        return self


class SessionCreate(BaseModel):
    card_id: str
    agent_mode: Literal["half_agent", "full_agent"] = "half_agent"
    channel_origin: Literal["standard", "chatgpt"] | None = None
    greeting: int = 0
    persona: str = "用户"
    policy: Policy | None = None
    persona_id: str | None = None


class TurnRequest(BaseModel):
    text: str = ""
    operation_id: str = Field(min_length=1, max_length=100)
    expected_revision: int
    mode: Literal["send", "regenerate"] = "send"
    message_id: str | None = None
    allow_content_edit: bool = False
    update_variables: bool = False
    generate: dict[str, Any] | None = None
    reasoning_effort: ReasoningEffort | None = None
    regenerate_note: str = ""
    regenerate_include_reply: bool = False
    scene_refresh: Literal["memory"] | None = None
    resume_turn_id: str | None = None


class SceneRefreshRequest(BaseModel):
    operation_id: str = Field(min_length=1, max_length=100)
    expected_revision: int


class LoreInjectionRemoval(BaseModel):
    expected_revision: int


class RevisionRequest(BaseModel):
    expected_revision: int
    head: str | None


class SessionFork(BaseModel):
    expected_revision: int
    message_id: str


class SessionTitle(BaseModel):
    expected_revision: int
    title: str = Field(min_length=1, max_length=100)


class SessionCacheBreakpoint(BaseModel):
    expected_revision: int
    automatic: bool
    message_id: str | None = None
    follow_interval: int | None = Field(default=None, ge=1, strict=True)
    reply_offset: int | None = Field(default=None, ge=1, strict=True)


class MessageEdit(BaseModel):
    text: str = Field(min_length=1)
    expected_revision: int
    notify_ai: bool = False


class ContentEdit(BaseModel):
    document: dict[str, Any]
    expected_revision: int | None = Field(default=None, ge=0)
    operation_id: str | None = Field(default=None, max_length=100)


class CardEditorCreate(BaseModel):
    document: dict[str, Any] | None = None
    expected_revision: int = Field(ge=0)


class CardEditorModelOptions(BaseModel):
    model: str | None = Field(default=None, min_length=1, max_length=200)
    reasoning_effort: ReasoningEffort | None = None

    @field_validator("model")
    @classmethod
    def nonblank_model(cls, value):
        if value is not None and not value.strip():
            raise ValueError("模型名称不能为空白")
        return value.strip() if value is not None else None


class CardEditorRequest(CardEditorModelOptions):
    text: str = ""
    operation_id: str = Field(min_length=1, max_length=100)
    expected_revision: int = Field(ge=0)
    adapt: bool = False
    resume_run_id: str | None = None


class CardEditorRevision(BaseModel):
    expected_revision: int = Field(ge=0)


class CardEditorSave(CardEditorRevision):
    mode: Literal["overwrite", "copy"]
    operation_id: str = Field(min_length=1, max_length=100)


class SessionOptions(BaseModel):
    expected_revision: int
    policy: Policy | None = None
    persona: str = "用户"
    persona_data: dict[str, Any] = Field(default_factory=dict)
    turn_preference: str = ""


class SessionScriptPermission(BaseModel):
    expected_revision: int = Field(ge=0)
    enabled: bool


class PersonaEdit(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str = ""
    expected_version: int = 0
