from pydantic import BaseModel, ConfigDict, Field


class PromptChoice(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str
    name: str = ""


class PromptChoices(BaseModel):
    model_config = ConfigDict(extra="forbid")

    active: int = Field(default=1, strict=True, ge=1, le=4)
    options: list[PromptChoice] = Field(min_length=4, max_length=4)

    @classmethod
    def from_text(cls, text: str):
        return cls(options=[PromptChoice(text=value) for value in (text, "", "", "")])

    @property
    def current(self) -> str:
        return self.options[self.active - 1].text

    def replace_current(self, text: str) -> None:
        self.options[self.active - 1].text = text
