"""Conversation-local connection choices; credentials always come from settings."""
from pydantic import BaseModel, ConfigDict, Field, field_validator

from tavern.core.channels import require_channel
from tavern.search.retrieval_config import ModelProfile


class ModelSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider_id: str = Field(min_length=1)
    # An empty name explicitly leaves a newly selected provider unconfigured.
    model: str = Field(max_length=200)
    provider_models: dict[str, str] = Field(default_factory=dict)

    @field_validator("model")
    @classmethod
    def normalize_model(cls, value):
        if value and not value.strip():
            raise ValueError("模型名称不能为空白")
        return value.strip()

    def profile(self, base: ModelProfile) -> ModelProfile:
        return base.for_provider(self.provider_id).model_copy(update={"model": self.model})

    def remember(self, previous: dict | None, base: ModelProfile) -> "ModelSelection":
        """Remember conversation choices without freezing global connection settings."""
        old = ModelSelection.model_validate(previous) if previous else None
        models = dict(old.provider_models) if old else {}
        provider_id, model = (old.provider_id, old.model) if old else (base.provider_id, base.model)
        if provider_id and model and provider_id != self.provider_id:
            models[provider_id] = model
        models.pop(self.provider_id, None)
        return self.model_copy(update={"provider_models": models})


def select_runtime(settings, role, selection, channel):
    if selection:
        profile = ModelSelection.model_validate(selection).profile(getattr(settings, role))
        setattr(settings, role, profile)
        if role == "full_agent":
            settings.foreground = profile.model_copy(deep=True)
            settings.background = profile.model_copy(deep=True)
    result = settings.for_role(role)
    require_channel(result, channel)
    result.channel_origin = channel
    return result
