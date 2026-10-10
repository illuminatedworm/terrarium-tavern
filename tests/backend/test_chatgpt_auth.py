import asyncio
import base64
import hashlib
import json
import os
import stat
import time
from urllib.parse import parse_qs

import httpx
import pytest

from tavern.chatgpt import API_ROOT, PLAN_SCOPE
from tavern.chatgpt.errors import ChatGPTError
from tests.backend.chatgpt_fixtures import (
    ACCESS,
    CLIENT,
    META,
    REFRESH,
    IdentityFixture,
    callback,
    seed_account,
)


@pytest.fixture
def identity():
    return IdentityFixture()


async def test_browser_registration_callback_verification_and_protected_restart(tmp_path, identity):
    service = identity.service(tmp_path)
    try:
        attempt = await service.start_login(provider_id="fixture-provider")
        params = identity.params
        assert params["client_id"] == "dynamic_agent_client"
        assert params["agent_name_hint"] == "Terrarium Tavern"
        assert params["ext_agent_host_id"].startswith("urn:uuid:")
        assert params["resource"] == API_ROOT and PLAN_SCOPE in params["scope"]
        assert params["redirect_uri"].startswith("http://127.0.0.1:")
        expected = base64.urlsafe_b64encode(hashlib.sha256(service.attempt["verifier"].encode()).digest()).decode().rstrip("=")
        assert params["code_challenge"] == expected
        assert not {"state", "nonce", "verifier", "client_id"} & attempt.keys()
        result = await callback(service, identity)
        assert result.status_code == 200
        assert ACCESS not in result.text and "no-store" in result.headers["cache-control"]
        account_id = service.status(attempt["id"])["account_id"]
        account = service.require_account(account_id)
        assert account["subject"] == "fixture-person" and account["client_id"] == CLIENT
        assert account["tokens"]["access_token"] == ACCESS
        assert service.public()["welcome_required"]
        service.acknowledge()
        restarted = identity.service(tmp_path)
        assert restarted.require_account(account_id)["tokens"]["refresh_token"] == REFRESH
        assert not restarted.public()["welcome_required"]
        raw = service.vault.path.read_bytes()
        if os.name == "nt":
            assert raw.startswith(b"DPAPI\0") and ACCESS.encode() not in raw and REFRESH.encode() not in raw
        else:
            assert stat.S_IMODE(service.vault.path.stat().st_mode) == 0o600
            assert stat.S_IMODE(service.vault.directory.stat().st_mode) == 0o700
        public = json.dumps(service.public())
        assert all(secret not in public for secret in (ACCESS, REFRESH, account["tokens"]["id_token"], CLIENT))
        again = await service.start_login(account_id=account_id)
        assert identity.params["client_id"] == CLIENT
        assert identity.params["ext_agent_host_id"] == params["ext_agent_host_id"]
        assert identity.params["id_token_hint"] == account["tokens"]["id_token"]
        assert "agent_name_hint" not in identity.params
        assert identity.params["state"] != params["state"]
        assert (await callback(service, identity, client_id=None)).status_code == 200
        assert service.status(again["id"])["account_id"] == account_id
        assert service.generation(account_id) != account["generation"]
    finally:
        await service.close()


@pytest.mark.parametrize("fault", ["nonce", "aud", "iss", "exp", "signature", "missing_client", "dynamic_client"])
async def test_invalid_identity_or_registration_never_saves_account(tmp_path, identity, fault):
    service = identity.service(tmp_path)
    try:
        await service.start_login()
        if fault == "signature":
            identity.jwk["kid"] = "other-key"
        elif fault in ("nonce", "aud", "iss"):
            identity.claim_overrides[fault] = "wrong"
        elif fault == "exp":
            identity.claim_overrides["exp"] = int(time.time()) - 60
        client_id = None if fault == "missing_client" else "dynamic_agent_client" if fault == "dynamic_client" else CLIENT
        assert (await callback(service, identity, client_id=client_id)).status_code == 400
        assert service.public()["accounts"] == []
        assert service.attempt["status"] == "failed"
    finally:
        await service.close()


async def test_wrong_state_does_not_consume_attempt_and_denial_does_not_exchange(tmp_path, identity):
    service = identity.service(tmp_path)
    try:
        await service.start_login()
        for state in ("wrong", "非 ASCII 状态", ""):
            assert (await callback(service, identity, state=state)).status_code == 400
            assert service.attempt["status"] == "pending"
        assert (await callback(service, identity, error="access_denied")).status_code == 400
        assert not any(str(request.url) == META["token_endpoint"] for request in identity.requests)
        assert service.public()["accounts"] == []
    finally:
        await service.close()


async def test_cancel_expire_and_single_pending_attempt(tmp_path, identity):
    service = identity.service(tmp_path)
    try:
        started = await service.start_login()
        with pytest.raises(ChatGPTError, match="login_busy"):
            await service.start_login()
        await service.cancel(started["id"])
        assert service.attempt["status"] == "cancelled" and "verifier" not in service.attempt
        started = await service.start_login()
        service.attempt["expires_at"] = time.time() - 1
        service.attempt["timer"].cancel()
        await service._expire(service.attempt)
        assert service.status(started["id"])["status"] == "expired"
        assert service.public()["accounts"] == []
    finally:
        await service.close()


@pytest.mark.parametrize("ending", ["cancelled", "expired"])
async def test_callback_replay_and_cancel_during_exchange_never_activate_twice(tmp_path, identity, ending):
    service = identity.service(tmp_path)
    exchanging, release = asyncio.Event(), asyncio.Event()
    async def delayed(request):
        if str(request.url) == META["token_endpoint"]:
            exchanging.set()
            await release.wait()
        return await identity(request)
    service.http_transport = httpx.MockTransport(delayed)
    try:
        started = await service.start_login()
        incoming = asyncio.create_task(callback(service, identity))
        await asyncio.wait_for(exchanging.wait(), 3)
        if ending == "cancelled":
            await asyncio.wait_for(service.cancel(started["id"]), 3)
        else:
            service.attempt["timer"].cancel()
            service.attempt["expires_at"] = time.time() - 1
            service.attempt["timer"] = asyncio.create_task(service._expire(service.attempt))
            await asyncio.wait_for(service.attempt["timer"], 3)
        await asyncio.wait_for(asyncio.gather(incoming, return_exceptions=True), 3)
        assert service.attempt["status"] == ending and service.public()["accounts"] == []
        service.http_transport = httpx.MockTransport(identity)
        await service.start_login()
        original = {"state": identity.params["state"], "code": "fixture-code", "client_id": CLIENT}
        await callback(service, identity)
        requests = len(identity.requests)
        assert (await service._callback(service.attempt, original))[0] == 400
        assert len(identity.requests) == requests and len(service.public()["accounts"]) == 1
    finally:
        release.set()
        await service.close()


async def test_unavailable_callback_listener_does_not_open_browser(tmp_path, identity, monkeypatch):
    async def unavailable(*args, **kwargs):
        raise OSError("fixture port unavailable")
    monkeypatch.setattr(asyncio, "start_server", unavailable)
    service = identity.service(tmp_path)
    with pytest.raises(ChatGPTError, match="callback_unavailable"):
        await service.start_login()
    assert service.attempt is None and not identity.urls and service.public()["accounts"] == []


async def test_nonrenewable_token_is_usable_until_expiry_then_requires_login(tmp_path, identity):
    service = identity.service(tmp_path)
    account_id, _ = seed_account(service, expires_at=time.time() + 30)
    with service.vault.locked():
        value = service.vault.load()
        value["accounts"][account_id]["tokens"]["refresh_token"] = ""
        service.vault.save(value)
    assert (await service.access(account_id))["tokens"]["access_token"] == ACCESS
    with service.vault.locked():
        value = service.vault.load()
        value["accounts"][account_id]["tokens"]["expires_at"] = time.time() - 1
        service.vault.save(value)
    with pytest.raises(ChatGPTError, match="sign_in_required"):
        await service.access(account_id)
    assert service.public()["accounts"][0]["status"] == "signed_out"
    assert identity.refresh_count == 0


async def test_declined_plan_keeps_identity_and_reconsent_is_explicit(tmp_path, identity):
    identity.token_overrides["scope"] = "openid profile email offline_access"
    service = identity.service(tmp_path)
    try:
        await service.start_login()
        assert "prompt" not in identity.params
        await callback(service, identity)
        account_id = service.attempt["account_id"]
        assert service.public()["accounts"][0]["status"] == "permission_required"
        with pytest.raises(ChatGPTError, match="plan_permission_required"):
            await service.access(account_id)
        await service.start_login(account_id=account_id, enable_plan=True)
        assert identity.params["prompt"] == "consent" and PLAN_SCOPE in identity.params["scope"]
        identity.token_overrides.clear()
        assert (await callback(service, identity)).status_code == 200
        assert service.require_account(account_id)
    finally:
        await service.close()


async def test_identity_only_result_without_inference_credentials_is_retained(tmp_path, identity):
    identity.token_overrides.update(scope="openid profile email", access_token="", refresh_token="", token_type="", expires_in=0)
    service = identity.service(tmp_path)
    try:
        await service.start_login()
        assert (await callback(service, identity)).status_code == 200
        account_id = service.attempt["account_id"]
        assert service.public()["accounts"][0]["status"] == "permission_required"
        with pytest.raises(ChatGPTError, match="plan_permission_required"):
            await service.access(account_id)
        assert service._read(account_id)["tokens"]["id_token"]
    finally:
        await service.close()


async def test_same_issued_registration_is_reused_and_completed_retry_is_not_reused_for_new_account(tmp_path, identity):
    service = identity.service(tmp_path)
    try:
        await service.start_login()
        identity.exchange_error = "invalid_grant"
        await callback(service, identity)
        await service.start_login()
        identity.exchange_error = None
        await callback(service, identity)
        account_id = service.attempt["account_id"]
        await service.start_login()
        assert identity.params["client_id"] == "dynamic_agent_client"
        await callback(service, identity)
        assert service.attempt["account_id"] == account_id
        assert len(service.public()["accounts"]) == 1
    finally:
        await service.close()


async def test_same_email_different_registration_and_mismatching_returning_client_or_subject(tmp_path, identity):
    service = identity.service(tmp_path)
    try:
        await service.start_login()
        await callback(service, identity)
        first = service.attempt["account_id"]
        saved = service.require_account(first)
        await service.start_login()
        await callback(service, identity, client_id="oaiapp_second_workspace")
        second = service.attempt["account_id"]
        assert second != first and len(service.public()["accounts"]) == 2
        await service.start_login(account_id=first)
        assert (await callback(service, identity, client_id="oaiapp_second_workspace")).status_code == 400
        assert service.require_account(first) == saved
        await service.start_login(account_id=first)
        identity.claim_overrides["sub"] = "someone-else"
        assert (await callback(service, identity)).status_code == 400
        assert service.require_account(first) == saved
    finally:
        await service.close()


async def test_invalid_grant_reuses_issued_registration_without_saving_unverified_identity(tmp_path, identity):
    service = identity.service(tmp_path)
    try:
        await service.start_login()
        identity.exchange_error = "invalid_grant"
        assert (await callback(service, identity)).status_code == 400
        assert not service.public()["accounts"]
        await service.start_login()
        assert identity.params["client_id"] == CLIENT and "agent_name_hint" not in identity.params
        identity.exchange_error = None
        assert (await callback(service, identity, client_id=None)).status_code == 200
    finally:
        await service.close()


async def test_concurrent_refreshes_share_rotation_and_keep_generation(tmp_path, identity):
    first, second = identity.service(tmp_path), identity.service(tmp_path)
    account_id, account = seed_account(first, expires_at=time.time() - 1)
    refreshing, release = asyncio.Event(), asyncio.Event()
    async def delayed(request):
        if str(request.url) == META["token_endpoint"]:
            refreshing.set()
            await release.wait()
        return await identity(request)
    first.http_transport = second.http_transport = httpx.MockTransport(delayed)
    one = asyncio.create_task(first.access(account_id))
    await refreshing.wait()
    two = asyncio.create_task(second.access(account_id))
    await asyncio.sleep(0.02)
    assert not two.done()
    release.set()
    refreshed = await asyncio.gather(one, two)
    assert identity.refresh_count == 1
    assert all(item["tokens"]["refresh_token"] == "fixture-refresh-1" for item in refreshed)
    assert all(item["generation"] == account["generation"] for item in refreshed)
    data = parse_qs(next(request.content.decode() for request in identity.requests if str(request.url) == META["token_endpoint"]))
    assert data["client_id"] == [CLIENT] and data["resource"] == [API_ROOT] and "scope" not in data


@pytest.mark.parametrize("code,cleared", [("invalid_grant", True), ("refresh_token_reused", True), ("invalid_client", False)])
async def test_refresh_failure_does_not_destroy_recoverable_credentials(tmp_path, identity, code, cleared):
    service = identity.service(tmp_path)
    account_id, account = seed_account(service, expires_at=time.time() - 1)
    identity.refresh_error = code
    with pytest.raises(ChatGPTError):
        await service.access(account_id)
    saved = service._read(account_id)
    assert (saved["tokens"] is None) == cleared
    assert saved["client_id"] == account["client_id"]
    assert service.public()["accounts"][0]["status"] == ("signed_out" if cleared else "connected")


async def test_refresh_window_and_network_failures_keep_account(tmp_path, identity):
    service = identity.service(tmp_path)
    account_id, account = seed_account(service, expires_at=time.time() + 20)
    with service.vault.locked():
        value = service.vault.load()
        value["accounts"][account_id]["tokens"]["earliest_refresh_at"] = time.time() + 10
        service.vault.save(value)
    assert (await service.access(account_id))["tokens"]["access_token"] == ACCESS
    assert identity.refresh_count == 0
    with service.vault.locked():
        value = service.vault.load()
        value["accounts"][account_id]["tokens"]["earliest_refresh_at"] = 0
        service.vault.save(value)
    def unavailable(request):
        raise httpx.ConnectError("fixture offline", request=request)
    service.http_transport = httpx.MockTransport(unavailable)
    with pytest.raises(ChatGPTError, match="refresh_unavailable"):
        await service.access(account_id)
    assert service._read(account_id)["tokens"]["refresh_token"] == account["tokens"]["refresh_token"]


@pytest.mark.parametrize("remote_status", [200, 503])
async def test_logout_cancels_active_work_clears_secrets_and_preserves_registration(tmp_path, identity, remote_status):
    identity.revocation_status = remote_status
    service = identity.service(tmp_path)
    account_id, original = seed_account(service)
    ready = asyncio.Event()
    async def work():
        owner = service.attach(account_id, original["generation"])
        ready.set()
        try:
            await asyncio.Event().wait()
        finally:
            service.detach(account_id, owner)
    task = asyncio.create_task(work())
    await ready.wait()
    result = await service.logout(account_id)
    assert task.cancelled()
    assert result["revocation_confirmed"] == (remote_status == 200)
    saved = service._read(account_id)
    assert saved["tokens"] is None and saved["client_id"] == CLIENT and saved["generation"] != original["generation"]
    with pytest.raises(ChatGPTError, match="sign_in_required"):
        await service.access(account_id)


async def test_untrusted_discovery_and_browser_failure_do_not_start_login(tmp_path, identity):
    service = identity.service(tmp_path)
    def evil(request):
        return httpx.Response(200, json={**META, "token_endpoint": "https://example.invalid/steal"})
    service.http_transport = httpx.MockTransport(evil)
    with pytest.raises(ChatGPTError):
        await service.start_login()
    assert not identity.urls
    service.http_transport = httpx.MockTransport(identity)
    service.browser_open = lambda _: False
    with pytest.raises(ChatGPTError, match="browser_unavailable"):
        await service.start_login()
    assert service.attempt["status"] == "cancelled"
    await service.close()


def test_corrupted_vault_is_not_overwritten(tmp_path, identity):
    service = identity.service(tmp_path)
    seed_account(service)
    service.vault.path.write_bytes(b"damaged")
    with pytest.raises(ValueError, match="未覆盖"):
        service.public()
    assert service.vault.path.read_bytes() == b"damaged"
