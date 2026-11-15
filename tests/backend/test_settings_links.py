import pytest
from pydantic import ValidationError

from tavern.core.models import Settings


@pytest.mark.parametrize(
    "changes",
    [
        {"history_message_cap": 7},
        {"context_pressure_trigger": 0.05},
    ],
)
def test_invalid_linked_settings_are_rejected(changes):
    with pytest.raises(ValidationError):
        Settings(**changes)


def test_lore_allowances_defaults_and_validation():
    cfg = Settings()
    assert (cfg.lore_max_entries, cfg.lore_max_chars) == (5, 3000)
    for changes in ({"lore_max_entries": 0}, {"lore_max_chars": 0}, {"lore_max_chars": 1.5}):
        with pytest.raises(ValidationError):
            Settings(**changes)
