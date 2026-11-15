import json
import re
from pathlib import Path

from httpx import ASGITransport, AsyncClient

from tavern.application.api import create_app
from tavern.core.kemini import (
    PREFIX_SOURCES,
    STORY_TRANSPORT_PROMPT,
    KeminiConfig,
    control_text,
    prefix_frames,
    turn_frames,
)
from tavern.core.models import Settings
from tavern.core.prompts import (
    PROMPT_DEFINITIONS,
)
from tavern.core.settings import SettingsDocument, migrate_settings
from tavern.story.config import STORY_PROMPT


def read_path(value, path):
    for part in path.split("."):
        value = value[int(part)] if isinstance(value, list) else value[part]
    return value


def test_defaults_and_custom_prompts():
    defaults = SettingsDocument().story.model_dump()
    for item in PROMPT_DEFINITIONS:
        assert read_path(defaults, item["path"]) == item["default"]
    assert "handoff" not in STORY_PROMPT and "new_context" not in STORY_PROMPT
    # Keep the source excerpts as evidence; only the requested boundary markers
    # and references to omitted ST wrappers differ from these saved originals.
    source = json.loads(
        (Path(__file__).parents[1] / "fixtures" / "kemini_source_prompts.json").read_text("utf-8")
    )
    kemini = KeminiConfig(enabled=True, normal_disclaimer_enabled=True, filler_text="用户填充原文")
    for field in ("clear_prompt", "identity_prompt", "info_prompt", "thinking_prompt", "disclaimer_text"):
        expected = re.sub(r"\{\{(?:setvar|getvar)::.*?\}\}", "", source[field], flags=re.S)
        if field == "clear_prompt":
            expected = (
                expected.replace("</clear>", "---\n").replace("after <Interleaving>", "after Interleaving")
                + "\n"
            )
        elif field == "identity_prompt":
            expected = expected.removeprefix("\n").replace("</instructions>\n\n\n", "</instructions>\n\n")
            expected += "\n"
        elif field == "info_prompt":
            expected = expected.replace("<info>", "---").replace("下面是", "分割线后是") + "\n"
        elif field == "thinking_prompt":
            expected = (
                expected.replace("<interactive_input>", "当前用户输入").replace("<char_guide>", "角色设定")
                + "\n"
            )
        assert getattr(kemini, field) == expected
    frames = prefix_frames(kemini)
    assert [m["source"] for m in frames] == list(PREFIX_SOURCES)
    assert [m["role"] for m in frames] == ["user", "system", "user", "system"]
    assert [m["content"] for m in frames] == [
        kemini.filler_text,
        kemini.clear_prompt,
        kemini.identity_prompt,
        kemini.info_prompt,
    ]
    assert control_text(kemini) == "\n\n".join(
        [
            kemini.thinking_prompt,
            kemini.content_prompt,
            kemini.disclaimer_text,
            STORY_TRANSPORT_PROMPT,
            kemini.output_prompt,
        ]
    )
    assert "你会在</think>后直接以<thinking>进行首次思考" in kemini.output_prompt
    assert kemini.output_prompt == (
        "## 输出格式\n\n你会在</think>后直接以<thinking>进行首次思考\n\n"
        + source["clear_prompt"].rsplit("\n\n", 1)[1].replace("after <Interleaving>", "after Interleaving")
        + "\n\n然后直接开始输出\n"
    )
    assert "不少于1000字" in kemini.content_prompt
    assert not any(tag in control_text(kemini) for tag in ("<plot_guide>", "<char_guide>", "殷仙龄"))
    paired = [
        frame
        for frame in [*frames, *turn_frames(kemini)]
        if "<Interleaving>" in frame["content"] or "</Interleaving>" in frame["content"]
    ]
    assert len(paired) == 1 and paired[0]["message_type"] == "kemini_control"
    assert paired[0]["content"].count("<Interleaving>") == 1
    assert paired[0]["content"].count("</Interleaving>") == 1
    old = SettingsDocument().model_dump()
    old["version"] = 5
    old["shared"]["kemini"]["identity_prompt"] = "已保存的身份"
    added = (
        "clear_prompt",
        "info_prompt",
        "roleplay_prompt",
        "content_prompt",
        "output_prompt",
    )
    for field in added:
        old["shared"]["kemini"].pop(field)
    # Version 8 adds archive recall and handoff chapter limits.
    old["story"]["tools"].pop("chapter_tokens")
    old["story"]["tools"].pop("chapters_total_tokens")
    migrated = migrate_settings(old)
    assert migrated.version == 22
    assert migrated.shared.kemini.identity_prompt == "已保存的身份"
    assert all(getattr(migrated.shared.kemini, field) == getattr(kemini, field) for field in added)
    assert (migrated.story.tools.chapter_tokens, migrated.story.tools.chapters_total_tokens) == (
        2048,
        16384,
    )
    assert migrate_settings(migrated.model_dump()) == migrated


def test_kemini_tail_upgrade_backs_up_defaults_and_preserves_custom_text(tmp_path):
    from tavern.storage.store import Store

    store = Store(tmp_path)
    raw = store.settings_document().model_dump()
    raw["version"] = 12
    fields = raw["shared"]["kemini"]
    defaults = KeminiConfig()
    leading, trailing = "\r\n \r\n", "\r\n\t\r\n"
    clear = defaults.clear_prompt.strip().replace("after Interleaving", "after the Markdown separator")
    clear += "\n\n用户自行保留的要求"
    thinking = (
        "用户自检前置要求\n\n"
        + defaults.thinking_prompt.replace("<Interleaving>", "---").replace("</Interleaving>", "---")
        + "\n\n用户自检末尾要求"
    )
    fields["clear_prompt"] = leading + clear.replace("\n", "\r\n") + trailing
    fields["thinking_prompt"] = leading + thinking.replace("\n", "\r\n") + trailing
    fields["output_prompt"] = leading + "用户末尾要求" + trailing
    fields.update(filler_text="用户小说全文末尾", enabled=True, normal_disclaimer_enabled=True)
    expected = {
        "clear_prompt": leading
        + clear.replace("after the Markdown separator", "after Interleaving").replace("\n", "\r\n")
        + trailing,
        "thinking_prompt": leading
        + ("用户自检前置要求\n\n" + defaults.thinking_prompt + "\n\n用户自检末尾要求").replace("\n", "\r\n")
        + trailing,
        "output_prompt": leading
        + "用户末尾要求\r\n\r\n"
        + defaults.output_prompt.strip().replace("\n", "\r\n")
        + trailing,
    }
    with store.connection() as db:
        db.execute("UPDATE meta SET value=? WHERE key='settings'", (json.dumps(raw),))
    upgraded = Store(tmp_path).settings_document()
    assert upgraded.version == 22 and upgraded.revisions["shared"] == raw["revisions"]["shared"] + 1
    for key, value in upgraded.shared.kemini.model_dump().items():
        assert value == expected.get(key, fields[key])
    assert migrate_settings(upgraded.model_dump()) == upgraded
    backups = list(tmp_path.glob("before-upgrade-*.sqlite3"))
    assert len(backups) == 1
    import sqlite3

    with sqlite3.connect(backups[0]) as db:
        assert json.loads(db.execute("SELECT value FROM meta WHERE key='settings'").fetchone()[0]) == raw
    assert Store(tmp_path).settings_document() == upgraded
    assert list(tmp_path.glob("before-upgrade-*.sqlite3")) == backups


def test_advisor_default_upgrade_preserves_custom_text_and_is_idempotent(tmp_path):
    from tavern.advisor.config import SYSTEM
    from tavern.storage.store import Store

    old = (Path(__file__).parents[1] / "fixtures" / "advisor_prompt_before_gui.txt").read_text("utf-8")
    store = Store(tmp_path)
    config = store.settings_document()
    config.shared.advisor.system_prompt.options[0].text = old
    config.shared.advisor.system_prompt.options[1].text = old
    store.save_settings(config)
    revision = store.settings_document().revisions["shared"]

    upgraded = Store(tmp_path).settings_document()
    assert upgraded.shared.advisor.system_prompt.options[0].text == SYSTEM
    assert upgraded.shared.advisor.system_prompt.options[1].text == old
    assert upgraded.revisions["shared"] == revision + 1
    assert Store(tmp_path).settings_document() == upgraded

    # Even a small user change to the old default must survive reopening.
    custom = old + "\n\n请保留我自行补充的要求。"
    upgraded.shared.advisor.system_prompt.options[0].text = custom
    store.save_settings(upgraded)
    saved = store.settings_document()
    assert Store(tmp_path).settings_document() == saved
    assert saved.shared.advisor.system_prompt.current == custom


async def test_all_prompts_save_reload_and_restore_default(tmp_path):
    app = create_app(tmp_path)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1") as client:
            response = (await client.get("/api/settings")).json()
            assert {item["scope"] for item in response["prompt_definitions"]} == {
                "story",
                "shared",
            }
            system_prompts = {
                item["scope"]: item
                for item in response["prompt_definitions"]
                if item["path"] == "system_prompt"
            }
            assert system_prompts["story"]["default"] == STORY_PROMPT
            assert "叙事与连续性原则" in system_prompts["story"]["description"]
            assert "工具授权、记录职责和提交规则由运行时协议提供" in system_prompts["story"]["description"]
            assert {
                (item["scope"], item["path"])
                for item in response["prompt_definitions"]
                if item.get("choices")
            } == {
                ("story", "system_prompt"),
                ("shared", "advisor.system_prompt"),
                ("shared", "card_author_config.system_prompt"),
            }
            for scope in ("story", "shared"):
                prompts = [item for item in response["prompt_definitions"] if item["scope"] == scope]
                for item in prompts:
                    if item.get("choices"):
                        item["path"] += ".options.0.text"
                changes = {item["path"]: "CUSTOM " + scope + item["path"] for item in prompts}
                saved = await client.patch(
                    f"/api/settings/{scope}",
                    json={"expected_revision": response["revisions"][scope], "changes": changes},
                )
                assert saved.status_code == 200, saved.text
                response = (await client.get("/api/settings")).json()
                for item in prompts:
                    assert read_path(response[scope], item["path"]) == changes[item["path"]]
                restored = await client.patch(
                    f"/api/settings/{scope}",
                    json={
                        "expected_revision": response["revisions"][scope],
                        "changes": {item["path"]: item["default"] for item in prompts},
                    },
                )
                assert restored.status_code == 200, restored.text
                response = restored.json()
                for item in prompts:
                    assert read_path(response[scope], item["path"]) == item["default"]
    finally:
        await app.state.service.close()


async def test_local_prompt_overrides_reach_worker(tmp_path):
    from tavern.search.local_models import LocalModelManager
    from tavern.search.retrieval import digest
    from tavern.storage.store import Store

    manager = LocalModelManager(Store(tmp_path))
    config = Settings(local_models={"instruction_prompt": "LOCAL TASK", "reranker_prompt": "LOCAL JUDGE"})
    sent = []

    class Pipe:
        def send(self, payload):
            sent.append(payload)

    async def receive(*args):
        return {"result": [0.9]}

    manager.receive = receive
    manager.ready.add("reranker")
    manager.host = (None, {"reranker": Pipe()})
    manager.loaded["reranker"] = digest(config.local_models.reranker.model_dump())
    result = await manager.infer("reranker", "rerank", [["query", "document"]], config, timeout=1)
    assert result == [0.9]
    assert sent[0]["instruction_prompt"] == "LOCAL TASK"
    assert sent[0]["reranker_prompt"] == "LOCAL JUDGE"


async def test_probe_uses_saved_prompt_overrides(tmp_path, monkeypatch):
    from types import SimpleNamespace

    import tavern.core.provider

    app = create_app(tmp_path)
    seen = []

    class Provider:
        async def chat_complete(self, messages):
            seen.extend(messages)
            return SimpleNamespace(content="TAVERN_MID_42")

        async def close(self):
            pass

    async def make_provider(*args):
        return Provider()

    monkeypatch.setattr(tavern.core.provider, "make_provider", make_provider)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1") as client:
            saved = await client.patch(
                "/api/settings/story",
                json={
                    "expected_revision": 0,
                    "changes": {
                        "probe_system_prompt": "PROBE CUSTOM",
                        "probe_marker_prompt": "MARKER CUSTOM TAVERN_MID_42",
                    },
                },
            )
            assert saved.status_code == 200
            response = await client.post("/api/settings/probe")
            assert response.status_code == 200, response.text
            assert seen[0]["content"] == "PROBE CUSTOM"
            assert seen[3]["content"] == "MARKER CUSTOM TAVERN_MID_42"
    finally:
        await app.state.service.close()
