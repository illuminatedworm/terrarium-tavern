"""Conversation-local connection choices; credentials always come from settings."""
from pydantic import BaseModel, ConfigDict, Field, field_validator

from tavern.core.channels import require_channel
from tavern.search.retrieval_config import ModelProfile


class ModelSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider_id: str = Field(min_length=1)
    model: str = Field(min_length=1, max_length=200)

    @field_validator("model")
    @classmethod
    def nonblank_model(cls, value):
        if not value.strip():
            raise ValueError("模型名称不能为空白")
        return value.strip()

    def profile(self, base):
        result = ModelProfile.model_validate({**base.model_dump(), **self.model_dump()})
        return result


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
