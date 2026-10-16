import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from tavern.application.api import create_app
from tavern.core.models import Settings


@pytest.mark.parametrize("changes", [
    {"history_message_cap": 7}, {"context_pressure_trigger": .05},
    {"chunk_mode": "tokens", "chunk_max_tokens": 29000},
])
def test_invalid_linked_settings_are_rejected(changes):
    with pytest.raises(ValidationError):
        Settings(**changes)


def test_boundary_and_off_mode_values():
    assert Settings().memory_timeout == 120
    assert Settings(memory_timeout=60).memory_timeout == 60
    cfg = Settings(history_message_cap=8, chunk_size=2, memory_interval_turns=1)
    assert cfg.history_message_cap == 8
    assert Settings(chunk_mode="off", chunk_size=3, ).chunk_size == 3
    assert Settings(history_message_cap=1000, chunk_size=100, memory_interval_turns=50).memory_interval_turns == 50
    assert (Settings().memory_current_max_records, Settings().memory_episode_cooldown_rounds) == (5, 20)
    assert Settings(memory_current_max_records=1, memory_episode_cooldown_rounds=0).memory_episode_cooldown_rounds == 0


@pytest.mark.parametrize("change", [
    {"memory_current_max_records": 0}, {"memory_episode_cooldown_rounds": 10001},
])
def test_memory_retention_settings_validate(change):
    with pytest.raises(ValidationError):
        Settings(**change)


async def test_api_validates_merged_settings_and_does_not_silently_adjust(tmp_path):
    app = create_app(tmp_path)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        before = (await client.get("/api/settings")).json()
        for change in ({"chunk_size": 101}, {"history_message_cap": 7}, {"memory_interval_turns": 101}):
            result = await client.patch("/api/settings/half_agent", json={"expected_revision": 0, "changes": change})
            assert result.status_code == 400
            assert (await client.get("/api/settings")).json() == before
        result = await client.patch("/api/settings/half_agent", json={"expected_revision": 0,
            "changes": {"history_message_cap": 8, "chunk_size": 2, "memory_interval_turns": 1}})
        assert result.status_code == 200
        assert result.json()["half_agent"]["history_message_cap"] == 8
        result = await client.patch("/api/settings/half_agent", json={"expected_revision": 1,
            "changes": {"memory_current_max_records": 8, "memory_episode_cooldown_rounds": 30}})
        assert result.status_code == 200
        assert result.json()["half_agent"]["memory_current_max_records"] == 8
        assert (await client.get("/api/settings")).json()["half_agent"]["memory_episode_cooldown_rounds"] == 30


def test_lore_allowances_defaults_and_validation():
    cfg = Settings()
    assert (cfg.lore_max_entries, cfg.lore_max_chars) == (5, 3000)
    for changes in ({"lore_max_entries": 0}, {"lore_max_chars": 0}, {"lore_max_chars": 1.5}):
        with pytest.raises(ValidationError):
            Settings(**changes)


def test_mixed_lore_windows_have_no_parity_or_timer_formula():
    cfg = Settings(history_message_cap=12, chunk_size=3, memory_interval_turns=100)
    assert cfg.chunk_size == 3 and cfg.memory_interval_turns == 100
