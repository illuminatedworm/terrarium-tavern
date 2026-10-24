import asyncio
import copy
import json
import zipfile

import httpx
import pytest

from tavern.application.api import create_app
from tavern.application.modes import ModeRuntime
from tavern.character.card_author import CardAuthorService
from tavern.chatgpt import API_ROOT
from tavern.chatgpt.errors import ChatGPTError
from tavern.core.models import CardAuthorRequest, SessionCreate, TurnRequest
from tavern.storage.backup import backup, restore
from tavern.storage.store import Store, uid
from tavern.storage.transfer import ExportSelection, export_package, read_package
from tavern.tracker.task import TrackerTask
from tests.backend.chatgpt_fixtures import (
    ACCESS,
    CLIENT,
    REFRESH,
    IdentityFixture,
    completed,
    seed_account,
    sse,
)


def connected_store(root):
    store = Store(root)
    identity = IdentityFixture()
    store._chatgpt_accounts = identity.service(root)
    account_id, _ = seed_account(store.chatgpt)
    doc = store.settings_document()
    provider = doc.shared.providers[0].model_copy(deep=True, update={"id": "official"})
    doc.shared.providers.append(provider)
    provider.auth_mode, provider.chatgpt_account_id, provider.use_proxy = (
        "chatgpt_subscription",
        account_id,
        False,
    )
    doc.shared.network.api_proxy = False
    doc.full_agent.default_channel = doc.half_agent.default_channel = "chatgpt"
    doc.full_agent.official_model = doc.full_agent.model.model_copy(deep=True)
    doc.half_agent.official_foreground = doc.half_agent.foreground.model_copy(deep=True)
    doc.half_agent.official_background = doc.half_agent.background.model_copy(deep=True)
    for profile, name in (
        (doc.full_agent.official_model, "fixture-full"),
        (doc.half_agent.official_foreground, "fixture-writer"),
        (doc.half_agent.official_background, "fixture-tracker"),
        (doc.shared.card_author, "fixture-editor"),
    ):
        profile.provider_id = provider.id
        profile.model = name
    doc.full_agent.tools.auto_retrieval = False
    doc.half_agent.chunk_mode = "off"
    doc.half_agent.memory_interval_turns = 100
    doc.half_agent.retrieval.channels = ["keyword"]
    store.save_settings(doc)
    return store, identity, account_id


def story(store, mode="half_agent"):
    card = store.import_card(
        json.dumps({"name": "旅人", "first_mes": "你站在门前。", "description": "完整的人物设定"}).encode(),
        "card.json",
    )
    return card, store.new_session(SessionCreate(card_id=card["id"], agent_mode=mode))


@pytest.mark.parametrize("mode", ["half_agent", "full_agent"])
async def test_real_story_engines_use_subscription_and_commit_only_completed_rounds(tmp_path, mode):
    store, _, _ = connected_store(tmp_path)
    _, session = story(store, mode)
    calls = []
    fail = False

    def model(request):
        body = json.loads(request.content)
        calls.append(body)
        assert str(request.url) == API_ROOT + "/responses"
        assert body["stream"] is True and body["store"] is False
        assert not {"max_output_tokens", "temperature", "top_p", "metadata"} & body.keys()
        if fail:
            return sse(
                events=[
                    {"type": "response.output_text.delta", "delta": "未完成的剧情"},
                    {
                        "type": "response.failed",
                        "response": {"error": {"code": "subscription_sharing_usage_limit_exceeded"}},
                    },
                ]
            )
        if mode == "full_agent":
            if len(calls) == 1:
                return sse(completed("", [("read_history", {"ref": "H1"})]))
            return sse(completed("她推开门，完整的故事继续。"))
        return sse(completed("她推开门，完整的故事继续。"))

    store.chatgpt.http_transport = httpx.MockTransport(model)
    runtime = ModeRuntime(store)
    try:
        turn = runtime.submit(
            session["id"], TurnRequest(text="我走进门。", operation_id="success", expected_revision=0)
        )
        await runtime.tasks[turn["id"]]
        saved = store.turn(turn["id"])
        assert saved["status"] == "committed", saved.get("error")
        current = store.session(session["id"])
        assert current["messages"][-1]["raw"] == "她推开门，完整的故事继续。"
        assert all(
            body["model"] == ("fixture-full" if mode == "full_agent" else "fixture-writer") for body in calls
        )
        if mode == "half_agent":
            assert (
                store.root / "runs" / f"{turn['id']}.kohakutr"
            ).exists()  # Real Terrarium, not ScriptedLLM.
        else:
            assert len(calls) == 2
            for body in calls:
                assert [item["role"] for item in body["input"][:3]] == ["developer"] * 3
                assert body["input"][0]["content"] == store.settings().full_agent_system_prompt
                assert body["input"][1]["content"].startswith("# Full Agent 工具协议")
                assert body["input"][2]["content"].startswith("本次上下文窗口上限为")
                assert body["tools"][0]["type"] == "namespace"
                assert body["tools"][0]["name"] == "tavern"
                assert all("tools" not in item for item in body["input"])
            assert any(
                item.get("type") == "function_call_output" and item.get("namespace") == "tavern"
                for item in calls[1]["input"]
            )
        fail = True
        failed = runtime.submit(
            session["id"],
            TurnRequest(text="下一步。", operation_id="failure", expected_revision=current["revision"]),
        )
        await runtime.tasks[failed["id"]]
        assert store.turn(failed["id"])["status"] != "committed"
        assert store.session(session["id"])["head"] == current["head"]
        assert store.session(session["id"])["snapshot"] == current["snapshot"]
        assert "subscription_sharing_usage_limit_exceeded" in store.turn(failed["id"])["error"]
        error_records = [
            event["payload"]
            for event in store.events(failed["id"])
            if event["kind"] == "provider_response" and event["payload"].get("error")
        ]
        assert error_records[-1]["error"]["request_id"] == "fixture-request"
        assert error_records[-1]["error"]["code"] == "subscription_sharing_usage_limit_exceeded"
        assert ACCESS not in json.dumps(store.events(turn["id"]))
    finally:
        await runtime.close()


async def test_tracker_uses_namespace_tools_and_returns_complete_result(tmp_path):
    store, _, _ = connected_store(tmp_path)
    card, session = story(store)
    requests = []

    def model(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            name = next(tool["name"] for tool in body["tools"][0]["tools"] if tool["name"] == "read_state")
            return sse(completed("", [(name, {})]))
        return sse(completed("已核对当前资料。"))

    store.chatgpt.http_transport = httpx.MockTransport(model)
    task = TrackerTask(
        store,
        "tracker-test",
        store.settings(),
        session,
        card["document"],
        session["messages"],
        session["snapshot"],
        phase="post",
    )
    await task.run()
    assert task.completed and task.summary == "已核对当前资料。"
    assert len(requests) == 2 and all(request["model"] == "fixture-tracker" for request in requests)
    assert requests[1]["input"][-1]["type"] == "function_call_output"


async def test_card_author_and_nested_script_requests_share_account_resolution(tmp_path, monkeypatch):
    store, _, _ = connected_store(tmp_path)
    requests = []

    def model(request):
        body = json.loads(request.content)
        requests.append(body)
        return sse(completed("编辑建议完成。" if body["model"] == "fixture-editor" else "嵌套与正文回复。"))

    store.chatgpt.http_transport = httpx.MockTransport(model)
    editor = CardAuthorService(store)
    workspace = editor.edits.create_conversation()
    try:
        task = editor.submit(
            workspace["id"],
            CardAuthorRequest(
                text="请提供角色设定建议，不修改卡片。",
                expected_revision=workspace["revision"],
                operation_id=uid(),
            ),
            legacy=False,
        )
        await editor.tasks[task["id"]]
        result = editor.edits.run(task["id"])
        assert result["status"] == "completed", result.get("error")
        assert requests[0]["model"] == "fixture-editor" and requests[0]["tools"][0]["name"] == "tavern"
    finally:
        await editor.close()
    _, session = story(store)
    runtime = ModeRuntime(store)
    original_open = runtime.half.runtime.open

    async def open_with_script(document, history, snapshot, permission, host, *args, **kwargs):
        original = copy.deepcopy(snapshot)
        assert await host("generate", {"user_input": "脚本嵌套请求"}) == "嵌套与正文回复。"
        assert original == snapshot
        return await original_open(document, history, snapshot, permission, host, *args, **kwargs)

    monkeypatch.setattr(runtime.half.runtime, "open", open_with_script)
    try:
        task = runtime.submit(
            session["id"], TurnRequest(text="继续。", operation_id=uid(), expected_revision=0)
        )
        await runtime.tasks[task["id"]]
        assert store.turn(task["id"])["status"] == "committed", store.turn(task["id"])["error"]
        records = [item["payload"] for item in store.events(task["id"]) if item["kind"] == "provider_request"]
        assert {item["purpose"] for item in records} >= {"script", "narrative"}
        assert all(body["stream"] for body in requests)
    finally:
        await runtime.close()


async def test_catalog_probe_and_local_api_do_not_leak_credentials(tmp_path):
    app = create_app(tmp_path)
    # Install synthetic credentials in the very same manager used by all registered routes.
    identity = IdentityFixture()
    accounts = app.state.store.chatgpt
    accounts.http_transport, accounts.browser_open = httpx.MockTransport(identity), identity.browser
    account_id, _ = seed_account(accounts)
    store = app.state.store
    doc = store.settings_document()
    provider = doc.shared.providers[0].model_copy(deep=True, update={"id": "official"})
    doc.shared.providers.append(provider)
    provider.auth_mode, provider.chatgpt_account_id, provider.use_proxy = (
        "chatgpt_subscription",
        account_id,
        False,
    )
    doc.shared.network.api_proxy = False
    doc.half_agent.official_foreground = doc.half_agent.foreground.model_copy(
        update={"provider_id": "official", "model": "fixture"}
    )
    doc.half_agent.default_channel = "chatgpt"
    store.save_settings(doc)
    identity.catalog = [
        {"slug": "z-last-alphabetically", "display_name": "首选模型", "visibility": "list"},
        {"slug": "hidden", "display_name": "隐藏", "visibility": "hide"},
        {"slug": "a-first-alphabetically", "display_name": "第二模型", "visibility": "list"},
    ]

    async def route(request):
        if str(request.url) == API_ROOT + "/responses":
            body = json.loads(request.content)
            assert body["input"][3]["role"] == "developer"
            return sse(completed("TAVERN_MID_42"))
        return await identity(request)

    accounts.http_transport = httpx.MockTransport(route)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as api:
        assert (
            await api.post(
                "/api/auth/chatgpt/login",
                json={"provider_id": "official"},
                headers={"sec-fetch-site": "cross-site"},
            )
        ).status_code == 403
        public = (await api.get("/api/auth/chatgpt/accounts")).json()
        assert public["accounts"][0]["plan_enabled"] is True
        before = (await api.get("/api/settings")).json()
        catalog = await api.post(
            "/api/settings/models",
            json={"provider_id": "official", "api_mode": "anthropic", "api_path": "untrusted"},
        )
        assert catalog.status_code == 200, catalog.text
        assert catalog.json()["models"] == ["z-last-alphabetically", "a-first-alphabetically"]
        assert catalog.json()["model_details"][0]["display_name"] == "首选模型"
        assert (
            await api.get("/api/settings/models", params={"provider_id": "official"})
        ).json() == catalog.json()
        probe = await api.post("/api/settings/probe?role=foreground", json={})
        assert probe.status_code == 200, probe.text
        assert probe.json()["capability"] == "accepted"
        editor = (await api.post("/api/settings/edit-sessions", json={})).json()
        for value in (public, before, editor, probe.json(), catalog.json()):
            assert all(
                secret not in json.dumps(value)
                for secret in (ACCESS, REFRESH, CLIENT, "fixture-id-token-only")
            )
        assert (await api.delete(f"/api/auth/chatgpt/accounts/{account_id}")).status_code == 400
        assert (await api.post(f"/api/auth/chatgpt/accounts/{account_id}/logout", json={})).status_code == 200
        assert (await api.get("/api/settings/models", params={"provider_id": "official"})).json()[
            "models"
        ] == []
        assert (await api.post("/api/settings/probe?role=foreground", json={})).json()[
            "code"
        ] == "sign_in_required"
    await accounts.close()


def test_settings_backups_exports_and_release_filters_exclude_oauth_material(tmp_path):
    from tools.package_release import publishable

    store, _, account_id = connected_store(tmp_path / "source")
    with store.chatgpt.vault.locked():
        host_id = store.chatgpt.vault.load()["host_id"]
    archive = tmp_path / "backup.zip"
    backup(store, archive)
    exported = export_package(store, ExportSelection(include_api_keys=True))
    package, _ = read_package(exported, store)
    secrets = (ACCESS, REFRESH, CLIENT, "fixture-id-token-only", host_id)
    assert all(secret not in json.dumps(package) for secret in secrets)
    with zipfile.ZipFile(archive) as zipped:
        assert not any(".chatgpt" in name for name in zipped.namelist())
        raw = b"".join(zipped.read(name) for name in zipped.namelist())
        assert all(secret.encode() not in raw for secret in secrets)
    restored = restore(archive, tmp_path / "restored")
    assert restored.settings().chatgpt_account_id == account_id
    assert restored.chatgpt.public()["accounts"] == []
    with pytest.raises(ChatGPTError, match="account_missing"):
        restored.chatgpt.require_account(account_id)
    from pathlib import Path

    assert not publishable(Path("src/tavern/.chatgpt/accounts.json"))
    assert not publishable(Path("src/tavern/.chatgpt/accounts.dat"))
    assert publishable(Path("src/tavern/chatgpt/service.py"))


async def test_operation_snapshot_cannot_silently_take_new_authorization(tmp_path):
    from tavern.core.provider import make_provider

    store, _, account_id = connected_store(tmp_path)
    snapshot = store.settings()
    generation = snapshot._auth_generation
    seed_account(store.chatgpt, account_id=account_id)
    assert store.settings()._auth_generation != generation
    for role in ("foreground", "background", "full_agent", "card_author"):
        config = snapshot.for_role(role)
        assert config._auth_generation == generation
        with pytest.raises(ChatGPTError, match="sign_in_required"):
            await make_provider(config)


async def test_account_bound_in_another_window_during_logout_cannot_be_deleted(tmp_path):
    app = create_app(tmp_path)
    store, identity = app.state.store, IdentityFixture()
    config = store.settings_document()
    config.shared.network.api_proxy = False
    store.save_settings(config)
    account_id, _ = seed_account(store.chatgpt)
    revoking, release = asyncio.Event(), asyncio.Event()

    async def route(request):
        if request.url.path.endswith("/revoke"):
            revoking.set()
            await release.wait()
        return await identity(request)

    store.chatgpt.http_transport = httpx.MockTransport(route)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        deleting = asyncio.create_task(client.delete(f"/api/auth/chatgpt/accounts/{account_id}"))
        await asyncio.wait_for(revoking.wait(), 3)
        config.shared.providers.append(
            config.shared.providers[0].model_copy(
                update={
                    "id": "official",
                    "auth_mode": "chatgpt_subscription",
                    "chatgpt_account_id": account_id,
                }
            )
        )
        store.save_settings(config)
        release.set()
        response = await asyncio.wait_for(deleting, 3)
    assert response.status_code == 400 and "保留注册记录" in response.json()["detail"]
    assert store.chatgpt._read(account_id)["tokens"] is None
    assert store.chatgpt.public()["accounts"][0]["status"] == "signed_out"
