"""Saving and review never grant permission to edit story content."""
from typing import Annotated, Literal

from pydantic import BeforeValidator


def normalize_policy(value):
    # Read old settings, sessions and exported saves without rewriting user data.
    if isinstance(value, str):
        return {"directed": "automatic", "evolving": "automatic", "preview": "review"}.get(value, value)
    return value


Policy = Annotated[Literal["automatic", "review"], BeforeValidator(normalize_policy)]
