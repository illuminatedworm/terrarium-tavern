"""Official checkpoints stay local to Advisor and are invalidated by model changes."""

import copy
import json

import httpx
import pytest
from kohakuterrarium.llm.base import ChatResponse
from kohakuterrarium.testing.llm import ScriptedLLM

from tavern.advisor.runtime import ConsultationRequest
from tavern.core.models import SessionCreate
from tavern.search.retrieval_config import ModelProfile, Provider
from tavern.storage.store import dump, uid
from tests.backend.test_advisor import ask, setup
from tests.backend.test_card_author import ToolLLM


async def test_resume_after_context_rejection_compacts_before_repeating_consultation(tmp_path):
    from tavern.advisor.context import runtime_config
    from tavern.scripting.model_transport import ModelTransport

    _, store, session, advisor = setup(tmp_path)
    settings = store.settings_document()
    settings.shared.advisor.keep_recent = 1
    store.save_settings(settings)
    for index in range(3):
        result = await ask(advisor, session["id"], ToolLLM([]), f"旧咨询{index}")
        assert result["status"] == "completed", result
    original = copy.deepcopy(store.session(session["id"])["snapshot"])
    cfg, _ = runtime_config(store, session, advisor.data.get(session["id"])["options"])
    requests = []

    def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            return httpx.Response(
                400, json={"error": {"code": "context_length_exceeded", "message": "prompt is too long"}}
            )
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "完整咨询摘要或回答"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 100, "completion_tokens": 10},
            },
        )

    def provider():
        return ModelTransport(
            cfg, "test-key", http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond))
        )

    cfg.model_request_mode = "non_stream"
    failed = await ask(advisor, session["id"], provider(), "继续讨论")
    assert failed["status"] == "error"
    assert advisor.data.run(failed["id"], private=True)["progress"]["context_prepared"]
    resumed = await ask(advisor, session["id"], provider(), "", resume=failed["id"])
    assert resumed["status"] == "completed", resumed
    assert len(requests) == 3
    assert "整理用户与参谋 agent 的旧咨询" in requests[1]["messages"][0]["content"]
    assert "旧咨询0" not in dump(requests[2]) and "旧咨询2" in dump(requests[2])
    assert advisor.data.get(session["id"])["context"]["through"] > 0
    assert store.session(session["id"])["snapshot"] == original


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


async def test_truncated_summary_keeps_the_original_consultation_window(tmp_path):
    _, store, session, advisor = setup(tmp_path)
    settings = store.settings_document()
    settings.shared.advisor.keep_recent = 1
    store.save_settings(settings)
    for index in range(3):
        result = await ask(advisor, session["id"], ToolLLM([]), f"完整咨询{index}")
        assert result["status"] == "completed", result
    before = advisor.data.get(session["id"])

    class IncompleteSummary(ScriptedLLM):
        async def chat_complete(self, messages, **kwargs):
            return ChatResponse(content="只有半段的摘要", finish_reason="length", usage={}, model="fixture")

    advisor.provider_factory = lambda: IncompleteSummary([])
    run = advisor.submit(
        session["id"], ConsultationRequest(expected_revision=3, operation_id=uid(), compact=True)
    )
    await advisor.tasks[run["id"]]
    result = advisor.data.run(run["id"], private=True)
    assert result["status"] == "error", result
    assert result["progress"]["context"] == before["context"]
    current = advisor.data.get(session["id"])
    assert current["context"] == before["context"]
    assert current["exchanges"] == before["exchanges"]
    assert "完整咨询0" in dump(advisor.preview(session["id"])["messages"])
