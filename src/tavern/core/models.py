from typing import Any, Literal

from pydantic import BaseModel, Field, PrivateAttr, model_validator

from tavern.core.prompts import (
    DEFAULT_MAINTENANCE_PROMPT,
    DEFAULT_MEMORY_PROMPT,
    DEFAULT_PREFERENCE_PROMPT,
    DEFAULT_PROBE_MARKER_PROMPT,
    DEFAULT_PROBE_PROMPT,
    DEFAULT_REGENERATE_PROMPT,
    DEFAULT_SYSTEM_PROMPT,
)
from tavern.full_agent.config import FULL_AGENT_PROMPT, FullAgentConfig
from tavern.search.retrieval_config import LocalModels, ModelProfile, Network, Provider, RetrievalSettings

Policy = Literal["directed", "evolving", "preview"]
LoreInjectionMode = Literal["conversation", "st_top"]
ReasoningEffort = Literal["default", "none", "minimal", "low", "medium", "high", "xhigh"]


class ContextBlock(BaseModel):
    id: str
    source: str
    role: Literal["system", "developer", "user", "assistant"] = "system"
    enabled: bool = True
    content: str = ""
    depth: int | None = Field(default=None, ge=0)
    priority: int = 50
    budget: int | None = Field(default=None, ge=1)
    required: bool = False
    condition: str = ""


class ContextRecipe(BaseModel):
    version: int = 1
    blocks: list[ContextBlock] = Field(
        default_factory=lambda: [
            ContextBlock(id="rules", source="rules", required=True, priority=100),
            ContextBlock(id="lore_before", source="lore_before"),
            ContextBlock(id="character", source="character", required=True, priority=100),
            ContextBlock(id="persona", source="persona"),
            ContextBlock(id="lore_after", source="lore_after"),
            ContextBlock(id="examples", source="examples", role="assistant", priority=30),
            ContextBlock(id="history", source="history", priority=60),
            ContextBlock(id="input", source="input", role="user", required=True, priority=100),
            ContextBlock(id="post", source="post", priority=90),
            ContextBlock(id="author_note", source="author_note", priority=80),
            ContextBlock(id="memory", source="memory", priority=70),
            ContextBlock(id="state", source="state", required=True, priority=100, depth=0),
        ]
    )

    @model_validator(mode="after")
    def current_input_follows_history(self):
        """Keep dialogue adjacent and dynamic context at the end of old recipes too."""
        blocks = self.blocks
        dialogue = {"history", "input"}
        trailing = ("author_note", "memory", "state")
        first = next((i for i, block in enumerate(blocks) if block.source in dialogue), len(blocks))
        special = dialogue | set(trailing)
        before = [block for i, block in enumerate(blocks) if i < first and block.source not in special]
        after = [block for i, block in enumerate(blocks) if i >= first and block.source not in special]
        self.blocks = (before
                       + [block for block in blocks if block.source == "history"]
                       + [block for block in blocks if block.source == "input"]
                       + after
                       + [block for source in trailing for block in blocks if block.source == source])
        return self


class Settings(BaseModel):
    """Operation-local runtime configuration (persist SettingsDocument instead)."""
    _full_runtime: Any = PrivateAttr(default=None)
    _settings_source: Any = PrivateAttr(default=None)
    _settings_mode: str = PrivateAttr(default="half_agent")
    runtime_mode: Literal["half_agent", "full_agent"] = "half_agent"
    providers: list[Provider] = Field(default_factory=lambda: [Provider(id="default")])
    # Stable persisted/API keys: foreground = Writer, background = Tracker.
    foreground: ModelProfile = Field(default_factory=ModelProfile)
    background: ModelProfile = Field(default_factory=lambda: ModelProfile(reply_tokens=1024))
    card_editor: ModelProfile | None = None
    full_agent: ModelProfile | None = None
    full_agent_config: FullAgentConfig = Field(default_factory=FullAgentConfig)
    full_agent_system_prompt: str = FULL_AGENT_PROMPT
    retrieval: RetrievalSettings = Field(default_factory=RetrievalSettings)
    local_models: LocalModels = Field(default_factory=LocalModels)
    network: Network = Field(default_factory=Network)

    def for_role(self, role="foreground"):
        if role == "full_agent" and self._full_runtime is not None:
            return self._full_runtime.model_copy(deep=True)
        profile = (self.background if role == "card_editor" and self.card_editor is None else
                   self.foreground if role == "full_agent" and self.full_agent is None else getattr(self, role))
        if profile is None:
            return self.model_copy(deep=True)
        provider = next(p for p in self.providers if p.id == profile.provider_id)
        return self.model_copy(deep=True, update={**profile.model_dump(exclude={"provider_id"}),
            "base_url": provider.base_url, "api_key": provider.api_key,
            "api_mode": provider.api_mode,
            "proxy": self.network.proxy, "use_proxy": self.network.api_proxy and provider.use_proxy})

    base_url: str = "https://api.openai.com/v1"
    model: str = ""
    tokenizer: Literal["auto", "o200k_base", "cl100k_base"] = "auto"
    api_key: str = Field(default="", exclude=True)
    system_prompt: str = DEFAULT_SYSTEM_PROMPT
    memory_system_prompt: str = DEFAULT_MEMORY_PROMPT
    maintenance_system_prompt: str = DEFAULT_MAINTENANCE_PROMPT
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
    api_mode: Literal["chat_completions", "responses"] = "chat_completions"
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
    policy: Policy = "directed"
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
            if profile and profile.provider_id not in {p.id for p in self.providers}:
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
    greeting: int = 0
    persona: str = "旅人"
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


class MessageEdit(BaseModel):
    text: str = Field(min_length=1)
    expected_revision: int


class ContentEdit(BaseModel):
    document: dict[str, Any]
    expected_revision: int | None = Field(default=None, ge=0)
    operation_id: str | None = Field(default=None, max_length=100)


class CardEditorCreate(BaseModel):
    document: dict[str, Any] | None = None
    expected_revision: int = Field(ge=0)


class CardEditorRequest(BaseModel):
    text: str = Field(min_length=1)
    operation_id: str = Field(min_length=1, max_length=100)
    expected_revision: int = Field(ge=0)
    adapt: bool = False


class CardEditorRevision(BaseModel):
    expected_revision: int = Field(ge=0)


class CardEditorSave(CardEditorRevision):
    mode: Literal["overwrite", "copy"]
    operation_id: str = Field(min_length=1, max_length=100)


class SessionOptions(BaseModel):
    expected_revision: int
    policy: Policy | None = None
    persona: str = "旅人"
    persona_data: dict[str, Any] = Field(default_factory=dict)
    turn_preference: str = ""


class PersonaEdit(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str = ""
    expected_version: int = 0
