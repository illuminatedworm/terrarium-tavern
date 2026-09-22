import pytest
from pydantic import ValidationError

from tavern.search.retrieval_config import LocalModels


def test_fragment_settings_preserve_existing_count():
    config = LocalModels(rerank_fragments=5)
    assert config.rerank_fragments == 5
    assert "rerank_max_chars" not in config.model_dump()


def test_temporary_character_setting_falls_back_to_default_fragments():
    # Character counts have no exact token equivalent; restore the default.
    assert LocalModels(rerank_max_chars=2400).rerank_fragments == 3
    assert LocalModels(rerank_max_chars=5000, rerank_fragments=6).rerank_fragments == 6


@pytest.mark.parametrize("value", [0, 9, 2.5])
def test_fragment_count_validation(value):
    with pytest.raises(ValidationError):
        LocalModels(rerank_fragments=value)
