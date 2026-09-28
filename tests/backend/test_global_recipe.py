import json
from pathlib import Path

from httpx import ASGITransport, AsyncClient

from tavern.application.api import create_app
from tavern.conversation.context import compile_context
from tavern.core.models import ContextBlock, ContextRecipe, SessionCreate
from tavern.storage.store import Store


def recipe(text):
    value = ContextRecipe()
    value.blocks.insert(0, ContextBlock(id="global_note", source="custom", content=text, required=True))
    return value


def card(store):
    return store.import_card(Path("samples/雾港来信.json").read_bytes(), "card.json")


def test_new_sessions_copy_global_recipe_and_remain_independent(tmp_path):
    store = Store(tmp_path)
    character = card(store)
    global_recipe = recipe("全局编排 A")
    settings = store.settings().model_copy(update={"context_recipe": global_recipe})
    store.save_settings(settings)
    first = store.new_session(SessionCreate(card_id=character["id"]))
    second = store.new_session(SessionCreate(card_id=character["id"]))
    assert first["recipe"] == second["recipe"] == global_recipe.model_dump()
    custom = recipe("对话自定义")
    second = store.update_session(second["id"], 0, recipe=custom.model_dump_json())
    store.save_settings(settings.model_copy(update={"context_recipe": recipe("全局编排 B")}))
    third = store.new_session(SessionCreate(card_id=character["id"]))
    assert third["recipe"]["blocks"][0]["content"] == "全局编排 B"
    assert store.session(first["id"])["recipe"] == global_recipe.model_dump()
    assert store.session(second["id"])["recipe"] == custom.model_dump()
    second["recipe"]["blocks"][0]["content"] = "内存中修改"
    assert store.session(second["id"])["recipe"] == custom.model_dump()
    compiled = compile_context(character["document"], first, first["messages"], first["snapshot"],
                               "继续", store.settings(), "global-recipe")
    assert next(m["content"] for m in compiled["trace"] if m["source"] == "global_note") == "全局编排 A"


def test_fork_copies_source_custom_recipe_instead_of_current_global(tmp_path):
    store = Store(tmp_path)
    character = card(store)
    source = store.new_session(SessionCreate(card_id=character["id"]))
    custom = recipe("分支自定义编排")
    source = store.update_session(source["id"], 0, recipe=custom.model_dump_json())
    store.save_settings(store.settings().model_copy(update={"context_recipe": recipe("新的全局编排")}))
    fork = store.fork_session(source["id"], source["head"], source["revision"])
    assert fork["recipe"] == source["recipe"] == custom.model_dump()
    changed = recipe("只修改新分支")
    store.update_session(fork["id"], 0, recipe=changed.model_dump_json())
    assert store.session(source["id"])["recipe"] == custom.model_dump()
    assert store.settings().context_recipe == recipe("新的全局编排")


def test_existing_settings_and_dialogue_recipe_survive_new_global_default(tmp_path):
    store = Store(tmp_path)
    character = card(store)
    source = store.new_session(SessionCreate(card_id=character["id"]))
    custom = recipe("旧对话编排")
    source = store.update_session(source["id"], 0, recipe=custom.model_dump_json())
    saved = store.settings().model_dump()
    saved.pop("context_recipe")
    with store.connection() as db:
        db.execute("UPDATE meta SET value='1' WHERE key='schema_version'")
        db.execute("UPDATE meta SET value=? WHERE key='settings'", (json.dumps(saved),))
    reopened = Store(tmp_path)
    assert reopened.settings().context_recipe == ContextRecipe()
    assert reopened.session(source["id"])["recipe"] == custom.model_dump()


async def test_global_settings_and_dialogue_reset_api(tmp_path):
    app = create_app(tmp_path)
    store = app.state.store
    character = card(store)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        global_recipe = recipe("通过设置保存")
        response = await client.patch("/api/settings/half_agent", json={"expected_revision": 0, "changes": {"context_recipe": global_recipe.model_dump()}})
        assert response.status_code == 200
        source = store.new_session(SessionCreate(card_id=character["id"]))
        assert source["recipe"] == global_recipe.model_dump()
        custom = recipe("自定义当前对话")
        url = f"/api/sessions/{source['id']}/recipe"
        response = await client.put(url, json={"recipe": custom.model_dump(), "expected_revision": 0})
        assert response.json()["recipe"] == custom.model_dump()
        assert (await client.get('/api/settings')).json()["half_agent"]["context_recipe"] == global_recipe.model_dump()
        assert (await client.put(url, json={"use_global": True, "expected_revision": 0})).status_code == 409
        response = await client.put(url, json={"use_global": True, "expected_revision": 1})
        assert response.status_code == 200 and response.json()["recipe"] == global_recipe.model_dump()
        invalid = global_recipe.model_dump()
        invalid["blocks"][0]["role"] = "invalid"
        assert (await client.patch('/api/settings/half_agent', json={"expected_revision": 1, "changes": {"context_recipe": invalid}})).status_code == 400
