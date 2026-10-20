"""Official checkpoints stay local to Advisor and are invalidated by model changes."""

import copy

import pytest
from kohakuterrarium.llm.base import ChatResponse
from kohakuterrarium.testing.llm import ScriptedLLM

from tavern.advisor.runtime import ConsultationRequest
from tavern.core.models import SessionCreate
from tavern.search.retrieval_config import ModelProfile, Provider
from tavern.storage.store import dump, uid
from tests.backend.test_advisor import ask, setup
from tests.backend.test_card_author import ToolLLM


class Cloud(ScriptedLLM):
    def __init__(self, valid=True):
        super().__init__(["回答"])
        self.last_native_response = None
        self.valid = valid

    async def chat_complete(self, messages, **kwargs):
        self.call_log.append(copy.deepcopy(messages))
        self.last_native_response = (
            {"items": [{"type": "compaction", "encrypted_content": "advisor-only-encrypted-state"}]}
            if self.valid
            else None
        )
        return ChatResponse(content="OK", finish_reason="stop", usage={}, model="fixture")


@pytest.mark.parametrize("valid", [True, False])
async def test_official_checkpoint_is_independent_and_requires_valid_state(tmp_path, valid):
    _, store, standard, advisor = setup(tmp_path)
    settings = store.settings_document()
    settings.shared.providers.append(Provider(id="official", name="官方", auth_mode="chatgpt_subscription"))
    settings.shared.advisor.official_model = ModelProfile(provider_id="official", model="advisor-cloud")
    settings.shared.advisor.keep_recent = 1
    settings.shared.advisor.context_strategy = "openai_compact"
    store.save_settings(settings)
    session = store.new_session(
        SessionCreate(card_id=standard["card"], agent_mode="full_agent", channel_origin="chatgpt")
    )
    for index in range(3):
        result = await ask(advisor, session["id"], ToolLLM([]), f"云端咨询{index}")
        assert result["status"] == "completed", result
    before = copy.deepcopy(store.session(session["id"])["snapshot"])
    model = Cloud(valid)
    advisor.provider_factory = lambda: model
    run = advisor.submit(
        session["id"], ConsultationRequest(expected_revision=3, operation_id=uid(), compact=True)
    )
    await advisor.tasks[run["id"]]
    result = advisor.data.run(run["id"])
    assert result["status"] == ("completed" if valid else "error"), result
    assert store.session(session["id"])["snapshot"] == before
    if not valid:
        assert not advisor.preview(session["id"])["opaque"]
        return
    preview = advisor.preview(session["id"])
    assert preview["opaque"]
    assert "advisor-only-encrypted-state" in dump(preview["messages"])
    assert "云端咨询2" in dump(preview["messages"])
    settings = store.settings_document()
    settings.shared.advisor.official_model.model = "another-advisor-model"
    store.save_settings(settings)
    preview = advisor.preview(session["id"])
    assert not preview["opaque"]
    assert "云端咨询0" in dump(preview["messages"])
