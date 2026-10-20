"""Shared account lifecycle. OAuth grants never masquerade as saved API keys."""

import asyncio
import base64
import hashlib
import hmac
import math
import re
import secrets
import time
import uuid
import webbrowser
from contextlib import asynccontextmanager
from datetime import datetime
from urllib.parse import urlencode

import httpx
from filelock import FileLock, Timeout

from tavern.chatgpt import API_ROOT, ISSUER, PLAN_SCOPE, SCOPES, USAGE_URL
from tavern.chatgpt.errors import TERMINAL_REFRESH, ChatGPTError, response_error
from tavern.chatgpt.oauth import LoopbackCallback, metadata, validate_identity
from tavern.chatgpt.vault import CredentialVault
from tavern.core.errors import NotFound


def _tokens(payload, *, old=None, now=None):
    now = time.time() if now is None else now
    if not isinstance(payload, dict):
        raise ChatGPTError("ChatGPT 凭据响应无效", code="invalid_token_response")
    scope = payload.get("scope")
    if scope is not None and not isinstance(scope, str):
        raise ChatGPTError("ChatGPT 返回的权限无效", code="invalid_token_response")
    granted = scope.split() if scope is not None else (old or {}).get("scopes", [])
    access = payload.get("access_token", "")
    if (
        not isinstance(access, str)
        or (PLAN_SCOPE in granted and not access)
        or (access and str(payload.get("token_type", "")).lower() != "bearer")
    ):
        raise ChatGPTError("ChatGPT 未返回有效访问凭据", code="invalid_token_response")
    try:
        lifetime = float(payload.get("expires_in", 0))
        if not math.isfinite(lifetime) or not 0 <= lifetime <= 86400 or (access and lifetime == 0):
            raise ValueError("invalid expiry")
        earliest = payload.get("earliest_refresh_at", 0)
        if isinstance(earliest, str) and not earliest.replace(".", "", 1).isdigit():
            earliest = datetime.fromisoformat(earliest.replace("Z", "+00:00")).timestamp()
        earliest = float(earliest or 0)
        if not math.isfinite(earliest) or earliest < 0:
            raise ValueError("invalid refresh time")
    except (TypeError, ValueError, KeyError, OverflowError):
        raise ChatGPTError("ChatGPT 凭据有效期无效", code="invalid_token_response") from None
    refresh = payload.get("refresh_token", "")
    if not isinstance(refresh, str) or (access and "offline_access" in granted and not refresh):
        raise ChatGPTError("ChatGPT 未返回可续期凭据", code="invalid_token_response")
    return {
        "access_token": access,
        "refresh_token": refresh,
        "id_token": payload.get("id_token") or (old or {}).get("id_token", ""),
        "scopes": granted,
        "expires_at": now + lifetime,
        "earliest_refresh_at": earliest,
        "saved_at": now,
    }


def _usable_without_refresh(tokens, now):
    return tokens["expires_at"] > now + 60 or (
        tokens["expires_at"] > now and (now < tokens["earliest_refresh_at"] or not tokens["refresh_token"])
    )


class ChatGPTAccounts:
    def __init__(self, root, network, *, http_transport=None, browser_open=None):
        self.vault, self.network = CredentialVault(root), network
        self.http_transport = http_transport
        self.browser_open = browser_open or webbrowser.open
        self.attempt = None
        self._login_lock = asyncio.Lock()
        self._active = {}
        self._blocked = set()
        self._model_capabilities = {}

    def remember_models(self, account_id, generation, details):
        self._model_capabilities[account_id] = (generation, {item["id"]: item for item in details})

    def model_capabilities(self, account_id, generation, model):
        cached = self._model_capabilities.get(account_id)
        return cached[1].get(model) if cached and cached[0] == generation else None

    def __deepcopy__(self, memo):
        # Operation snapshots share one lifecycle manager, not copies of its locks/tasks.
        return self

    def client(self, use_proxy=True):
        config = self.network()
        proxy = (config.proxy or None) if config.api_proxy and use_proxy else None
        return httpx.AsyncClient(
            proxy=proxy, trust_env=False, follow_redirects=False, timeout=30, transport=self.http_transport
        )

    def _read(self, account_id):
        with self.vault.locked():
            account = self.vault.load()["accounts"].get(account_id)
        if account is None:
            raise ChatGPTError("请连接此安装中的 ChatGPT 账号", code="account_missing")
        return account

    def generation(self, account_id):
        if not account_id or not self.vault.path.exists():
            return ""
        try:
            return self._read(account_id).get("generation", "")
        except ChatGPTError:
            return ""

    def require_account(self, account_id, generation=None):
        if not account_id:
            raise ChatGPTError("请先选择已连接的 ChatGPT 账号", code="account_missing")
        account = self._read(account_id)
        if (
            account_id in self._blocked
            or not account.get("tokens")
            or (generation and generation != account["generation"])
        ):
            raise ChatGPTError("ChatGPT 连接已退出或改变，请重新登录或重新开始任务", code="sign_in_required")
        if PLAN_SCOPE not in account["tokens"]["scopes"]:
            raise ChatGPTError(
                "已登录，但未授权使用 ChatGPT 订阅，请启用订阅使用权限", code="plan_permission_required"
            )
        return account

    def public(self):
        if not self.vault.path.exists():
            return {"accounts": [], "welcome_required": False, "usage_url": USAGE_URL}
        with self.vault.locked():
            value = self.vault.load()
        result = []
        for key, account in value["accounts"].items():
            tokens = account.get("tokens") or {}
            enabled = PLAN_SCOPE in tokens.get("scopes", [])
            result.append(
                {
                    "id": key,
                    "label": account["label"],
                    "email": account.get("email", ""),
                    "status": "connected" if enabled else "permission_required" if tokens else "signed_out",
                    "plan_enabled": enabled,
                    "generation": account["generation"],
                }
            )
        return {
            "accounts": result,
            "welcome_required": not value.get("welcomed", False)
            and any(item["plan_enabled"] for item in result),
            "usage_url": USAGE_URL,
        }

    def acknowledge(self):
        with self.vault.locked():
            value = self.vault.load()
            value["welcomed"] = True
            self.vault.save(value)

    @asynccontextmanager
    async def account_lock(self, account_id):
        if not isinstance(account_id, str) or not re.fullmatch(r"[a-f0-9]{32}", account_id):
            raise ChatGPTError("ChatGPT 账号引用无效", code="account_missing")
        self.vault.prepare()
        lock = FileLock(self.vault.directory / f"session-{account_id}.lock", mode=0o600)
        until = time.monotonic() + 40
        while True:
            try:
                lock.acquire(timeout=0)
                break
            except Timeout:
                if time.monotonic() >= until:
                    raise ChatGPTError("ChatGPT 账号正在更新，请稍后重试", code="account_busy") from None
                await asyncio.sleep(0.05)
        try:
            yield
        finally:
            lock.release()

    async def access(self, account_id, *, use_proxy=True, generation=None):
        account = self.require_account(account_id, generation)
        tokens, now = account["tokens"], time.time()
        if _usable_without_refresh(tokens, now):
            return account
        async with self.account_lock(account_id):
            account = self.require_account(account_id, generation)
            tokens, now = account["tokens"], time.time()
            if _usable_without_refresh(tokens, now):
                return account
            if not tokens["refresh_token"]:
                self._clear(account_id, account["generation"])
                raise ChatGPTError("ChatGPT 登录已到期，请重新登录", code="sign_in_required")
            if tokens["earliest_refresh_at"] > now:
                raise ChatGPTError("ChatGPT 凭据暂不能刷新，请稍后重试", code="refresh_not_ready")
            try:
                async with self.client(use_proxy) as client:
                    config = await metadata(client)
                    response = await client.post(
                        config["token_endpoint"],
                        data={
                            "grant_type": "refresh_token",
                            "client_id": account["client_id"],
                            "refresh_token": tokens["refresh_token"],
                            "resource": API_ROOT,
                        },
                    )
                    payload = response.json()
                    if response.status_code != 200:
                        error = response_error(
                            payload,
                            response.status_code,
                            response.headers.get("x-request-id"),
                            secrets=tuple(
                                tokens.get(key, "") for key in ("access_token", "refresh_token", "id_token")
                            ),
                        )
                        if error.code in TERMINAL_REFRESH:
                            self._clear(account_id, account["generation"])
                            raise ChatGPTError(
                                "ChatGPT 登录已失效，请重新登录",
                                code="sign_in_required",
                                status=error.status,
                                request_id=error.request_id,
                                diagnostic=error.diagnostic,
                            )
                        raise error
                    if not isinstance(payload, dict):
                        raise ChatGPTError("ChatGPT 凭据响应无效", code="invalid_token_response")
                    if payload.get("id_token"):
                        await validate_identity(
                            client,
                            config,
                            payload["id_token"],
                            account["client_id"],
                            subject=account["subject"],
                        )
                    replacement = _tokens(payload, old=tokens)
            except (httpx.HTTPError, ValueError) as error:
                if isinstance(error, ChatGPTError):
                    raise
                raise ChatGPTError(
                    "ChatGPT 凭据刷新失败，连接已保留，请稍后重试", code="refresh_unavailable"
                ) from None
            with self.vault.locked():
                value = self.vault.load()
                current = value["accounts"].get(account_id)
                if (
                    not current
                    or current["generation"] != account["generation"]
                    or account_id in self._blocked
                ):
                    raise ChatGPTError("ChatGPT 连接已改变，请重新开始任务", code="sign_in_required")
                current["tokens"] = replacement
                self.vault.save(value)
            return self.require_account(account_id, account["generation"])

    def attach(self, account_id, generation):
        self.require_account(account_id, generation)
        task = asyncio.current_task()
        owners = self._active.setdefault(account_id, {})
        owners[task] = owners.get(task, 0) + 1
        return task

    def detach(self, account_id, task):
        owners = self._active.get(account_id, {})
        if task in owners:
            owners[task] -= 1
            if owners[task] <= 0:
                del owners[task]
        if not owners:
            self._active.pop(account_id, None)

    async def stop_requests(self, account_id):
        tasks = [
            task
            for task in self._active.get(account_id, {})
            if task is not asyncio.current_task() and not task.done()
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _clear(self, account_id, generation):
        with self.vault.locked():
            value = self.vault.load()
            account = value["accounts"].get(account_id)
            if account and account["generation"] == generation:
                account["tokens"] = None
                account["generation"] = uuid.uuid4().hex
                self.vault.save(value)

    async def logout(self, account_id, *, use_proxy=True):
        self._blocked.add(account_id)
        try:
            if (
                self.attempt
                and self.attempt.get("account_id") == account_id
                and self.attempt["status"] in ("pending", "exchanging")
            ):
                await self.cancel(self.attempt["id"])
            await self.stop_requests(account_id)
            async with self.account_lock(account_id):
                account = self._read(account_id)
                tokens = account.get("tokens") or {}
                self._clear(account_id, account["generation"])
                confirmed = not tokens.get("refresh_token")
                if not confirmed:
                    for attempt in range(2):
                        try:
                            async with self.client(use_proxy) as client:
                                config = await metadata(client)
                                response = await client.post(
                                    config["revocation_endpoint"],
                                    data={
                                        "token": tokens["refresh_token"],
                                        "token_type_hint": "refresh_token",
                                        "client_id": account["client_id"],
                                    },
                                )
                            confirmed = response.status_code == 200
                            if confirmed or response.status_code < 500:
                                break
                        except (httpx.HTTPError, ValueError):
                            pass
                        if attempt == 0:
                            await asyncio.sleep(0.25)
            return {
                "signed_out": True,
                "revocation_confirmed": confirmed,
                "usage_url": USAGE_URL,
                "message": "已退出 ChatGPT"
                if confirmed
                else "已在本地退出，但未确认远程撤销；请在 ChatGPT 设置中断开应用",
            }
        finally:
            self._blocked.discard(account_id)

    def status(self, attempt_id):
        if not self.attempt or self.attempt["id"] != attempt_id:
            raise NotFound("登录事务已失效")
        return {
            key: self.attempt.get(key)
            for key in ("id", "status", "account_id", "provider_id", "error", "expires_at")
        }

    async def start_login(self, *, account_id=None, use_proxy=True, enable_plan=False, provider_id=None):
        async with self._login_lock:
            if self.attempt and self.attempt["status"] in ("pending", "exchanging"):
                raise ChatGPTError("已有 ChatGPT 登录正在进行，请先完成或取消", code="login_busy")
            if account_id in self._blocked:
                raise ChatGPTError("ChatGPT 账号正在退出，请稍后重试", code="account_busy")
            account = self._read(account_id) if account_id else None
            retry_client = (
                self.attempt.get("retry_client_id")
                if self.attempt
                and self.attempt["status"] == "failed"
                and self.attempt.get("account_id") is None
                and not account_id
                else None
            )
            try:
                async with self.client(use_proxy) as client:
                    config = await metadata(client)
            except (httpx.HTTPError, ValueError):
                raise ChatGPTError(
                    "无法连接 OpenAI 登录服务，请检查网络设置", code="login_unavailable"
                ) from None
            with self.vault.locked():
                value = self.vault.load()
                if not value.get("host_id"):
                    value["host_id"] = "urn:uuid:" + str(uuid.uuid4())
                    self.vault.save(value)
                host_id = value["host_id"]
            verifier = secrets.token_urlsafe(64)
            attempt = {
                "id": uuid.uuid4().hex,
                "status": "pending",
                "account_id": account_id,
                "provider_id": provider_id,
                "generation": account["generation"] if account else None,
                "expires_at": time.time() + 600,
                "state": secrets.token_urlsafe(32),
                "nonce": secrets.token_urlsafe(32),
                "verifier": verifier,
                "config": config,
                "use_proxy": use_proxy,
                "client_id": account["client_id"] if account else retry_client,
            }
            listener = LoopbackCallback(lambda query: self._callback(attempt, query))
            try:
                uri = await listener.start()
            except OSError:
                raise ChatGPTError(
                    "无法启动本地登录回调，请检查本机监听权限", code="callback_unavailable"
                ) from None
            attempt.update(listener=listener, redirect_uri=uri)
            self.attempt = attempt
            params = {
                "client_id": attempt["client_id"] or "dynamic_agent_client",
                "ext_agent_host_id": host_id,
                "response_type": "code",
                "redirect_uri": uri,
                "scope": SCOPES,
                "resource": API_ROOT,
                "state": attempt["state"],
                "nonce": attempt["nonce"],
                "code_challenge_method": "S256",
                "code_challenge": base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
                .decode()
                .rstrip("="),
            }
            if attempt["client_id"] is None:
                params["agent_name_hint"] = "Terrarium Tavern"
            elif account is not None:
                if account.get("email"):
                    params["login_hint"] = account["email"]
                if account.get("tokens", {}) and account["tokens"].get("id_token"):
                    params["id_token_hint"] = account["tokens"]["id_token"]
            if enable_plan:
                params["prompt"] = "consent"
            attempt["timer"] = asyncio.create_task(self._expire(attempt))
            try:
                opened = await asyncio.to_thread(
                    self.browser_open, config["authorization_endpoint"] + "?" + urlencode(params)
                )
                if opened is False:
                    raise OSError("browser unavailable")
            except Exception:
                await self.cancel(attempt["id"])
                raise ChatGPTError(
                    "无法打开系统浏览器，请设置默认浏览器后重试", code="browser_unavailable"
                ) from None
            return self.status(attempt["id"])

    async def _expire(self, attempt):
        await asyncio.sleep(max(0, attempt["expires_at"] - time.time()))
        if self.attempt is attempt and attempt["status"] in ("pending", "exchanging"):
            attempt["status"] = "expired"
            await attempt["listener"].stop()
            self._erase_transaction(attempt)

    @staticmethod
    def _erase_transaction(attempt):
        for key in ("verifier", "nonce", "state"):
            attempt.pop(key, None)

    async def cancel(self, attempt_id):
        self.status(attempt_id)
        attempt = self.attempt
        if attempt["status"] in ("pending", "exchanging"):
            attempt["status"] = "cancelled"
            attempt["timer"].cancel()
            await attempt["listener"].stop()
            self._erase_transaction(attempt)
        return self.status(attempt_id)

    async def _callback(self, attempt, query):
        if (
            self.attempt is not attempt
            or attempt["status"] != "pending"
            or time.time() >= attempt["expires_at"]
            or not hmac.compare_digest(query.get("state", "").encode(), attempt.get("state", "").encode())
        ):
            return 400, "登录回调无效或已过期。请回到 Terrarium Tavern 重试。"
        attempt["status"] = "exchanging"
        try:
            if query.get("error"):
                raise ChatGPTError("此次 ChatGPT 登录未获授权", code="access_denied")
            issued = query.get("client_id") or attempt["client_id"]
            if (
                not issued
                or issued == "dynamic_agent_client"
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", issued)
                or (attempt["client_id"] and issued != attempt["client_id"])
                or not query.get("code")
            ):
                raise ChatGPTError("ChatGPT 客户端注册回调不完整或不匹配", code="invalid_registration")
            attempt["client_id"] = issued
            async with self.client(attempt["use_proxy"]) as client:
                response = await client.post(
                    attempt["config"]["token_endpoint"],
                    data={
                        "grant_type": "authorization_code",
                        "client_id": issued,
                        "code": query["code"],
                        "code_verifier": attempt["verifier"],
                        "redirect_uri": attempt["redirect_uri"],
                        "resource": API_ROOT,
                    },
                )
                if response.status_code != 200:
                    try:
                        error = response_error(response.json(), response.status_code)
                        if error.code == "invalid_grant" and not attempt["account_id"]:
                            attempt["retry_client_id"] = issued
                    except ValueError:
                        pass
                    raise ChatGPTError("ChatGPT 登录交换失败，请重新发起登录", code="code_exchange_failed")
                payload = response.json()
                if not isinstance(payload, dict):
                    raise ChatGPTError("ChatGPT 登录响应无效", code="invalid_token_response")
                old = self._read(attempt["account_id"]) if attempt["account_id"] else None
                claims = await validate_identity(
                    client,
                    attempt["config"],
                    payload.get("id_token"),
                    issued,
                    nonce=attempt["nonce"],
                    subject=old["subject"] if old else None,
                )
                tokens = _tokens(payload)
            if old is None:
                with self.vault.locked():
                    registrations = self.vault.load()["accounts"]
                    existing = next(
                        ((key, item) for key, item in registrations.items() if item["client_id"] == issued),
                        None,
                    )
                if existing:
                    account_id, old = existing
                    if old["subject"] != claims["sub"] or old["issuer"] != ISSUER:
                        raise ChatGPTError("ChatGPT 注册与已保存身份不匹配", code="account_changed")
                    attempt.update(account_id=account_id, generation=old["generation"])
            account_id = attempt["account_id"] or uuid.uuid4().hex
            async with self.account_lock(account_id):
                # No credential changes until validation finishes; cancellation cannot activate a connection.
                if attempt["status"] != "exchanging":
                    return 400, "登录已取消。请回到 Terrarium Tavern。"
                if old:
                    await self.stop_requests(account_id)
                with self.vault.locked():
                    value = self.vault.load()
                    current = value["accounts"].get(account_id)
                    if old and (not current or current["generation"] != attempt["generation"]):
                        raise ChatGPTError("ChatGPT 账号已改变，请重新登录", code="account_changed")
                    email = claims.get("email") if isinstance(claims.get("email"), str) else ""
                    if old:
                        label = old["label"]
                    else:
                        number = value.get("next_label", 1)
                        labels = {item["label"] for item in value["accounts"].values()}
                        while f"连接 {number}" in labels:
                            number += 1
                        label = f"连接 {number}"
                        value["next_label"] = number + 1
                    value["accounts"][account_id] = {
                        "client_id": issued,
                        "subject": claims["sub"],
                        "issuer": ISSUER,
                        "email": email,
                        "label": label,
                        "generation": uuid.uuid4().hex,
                        "tokens": tokens,
                    }
                    self.vault.save(value)
                attempt.update(status="completed", account_id=account_id)
                attempt.pop("retry_client_id", None)
            return 200, "ChatGPT 登录已完成。请回到 Terrarium Tavern 查看账号权限并选择模型。"
        except (ChatGPTError, httpx.HTTPError, ValueError) as error:
            if attempt["status"] == "exchanging":
                attempt.update(
                    status="failed",
                    error=str(error)
                    if isinstance(error, ChatGPTError)
                    else "ChatGPT 登录暂不可用，未保存此次登录，请重试",
                )
            return 400, "ChatGPT 登录未完成。请回到 Terrarium Tavern 查看详情。"
        finally:
            # The expiry task may currently be waiting for this callback to close.
            if attempt["status"] != "expired":
                attempt["timer"].cancel()
            # Keep this handler alive until it sends the fixed callback page.
            attempt["listener"].server.close()
            self._erase_transaction(attempt)

    async def close(self):
        if self.attempt:
            self.attempt["timer"].cancel()
            if self.attempt["status"] in ("pending", "exchanging"):
                await self.cancel(self.attempt["id"])
            await self.attempt["listener"].stop()
