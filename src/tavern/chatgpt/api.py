"""Local account controls; settings only store an opaque account reference."""
from fastapi import HTTPException, Request
from pydantic import BaseModel, Field

from tavern.core.settings import SettingsDocument


class LoginRequest(BaseModel):
    provider_id: str
    account_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")
    enable_plan: bool = False


class AccountAction(BaseModel):
    provider_id: str | None = None


def register_chatgpt_routes(app, store):
    accounts = store.chatgpt

    def boundary(request):
        if request.headers.get("sec-fetch-site") == "cross-site":
            raise HTTPException(403, "跨来源账号操作被拒绝")

    def proxy(provider_id):
        if provider_id is None:
            return True
        provider = next((item for item in store.settings_document().shared.providers if item.id == provider_id), None)
        if provider is None or provider.auth_mode != "chatgpt_subscription":
            raise ValueError("请先保存 ChatGPT 订阅提供商")
        return provider.use_proxy

    @app.get("/api/auth/chatgpt/accounts")
    async def list_accounts(request: Request):
        boundary(request)
        return accounts.public()

    @app.post("/api/auth/chatgpt/login")
    async def login(payload: LoginRequest, request: Request):
        boundary(request)
        return await accounts.start_login(account_id=payload.account_id, use_proxy=proxy(payload.provider_id),
                                          enable_plan=payload.enable_plan, provider_id=payload.provider_id)

    @app.get("/api/auth/chatgpt/login")
    async def current_login(request: Request):
        boundary(request)
        return accounts.status(accounts.attempt["id"]) if accounts.attempt else {"status": "idle"}

    @app.get("/api/auth/chatgpt/login/{attempt_id}")
    async def login_status(attempt_id: str, request: Request):
        boundary(request)
        return accounts.status(attempt_id)

    @app.delete("/api/auth/chatgpt/login/{attempt_id}")
    async def cancel_login(attempt_id: str, request: Request):
        boundary(request)
        return await accounts.cancel(attempt_id)

    @app.post("/api/auth/chatgpt/accounts/{account_id}/logout")
    async def logout(account_id: str, payload: AccountAction, request: Request):
        boundary(request)
        return await accounts.logout(account_id, use_proxy=proxy(payload.provider_id))

    @app.post("/api/auth/chatgpt/welcome")
    async def acknowledge_welcome(request: Request):
        boundary(request)
        accounts.acknowledge()
        return {"ok": True}

    @app.delete("/api/auth/chatgpt/accounts/{account_id}")
    async def forget_account(account_id: str, request: Request):
        boundary(request)
        if any(p.chatgpt_account_id == account_id for p in store.settings_document().shared.providers):
            raise ValueError("账号仍被提供商引用，请先解除关联；可直接退出账号")
        if accounts.attempt and accounts.attempt.get("account_id") == account_id and accounts.attempt["status"] in ("pending", "exchanging"):
            raise ValueError("请先完成或取消此账号的登录")
        result = await accounts.logout(account_id)
        async with accounts.account_lock(account_id):
            # A second settings window may bind this account while revocation is in flight.
            with store.connection() as db:
                db.execute("BEGIN IMMEDIATE")
                current = SettingsDocument.model_validate_json(db.execute("SELECT value FROM meta WHERE key='settings'").fetchone()[0])
                if any(provider.chatgpt_account_id == account_id for provider in current.shared.providers):
                    raise ValueError("账号在退出期间已被提供商引用，保留注册记录；已退出的会话不会恢复")
                with accounts.vault.locked():
                    value = accounts.vault.load()
                    value["accounts"].pop(account_id, None)
                    accounts.vault.save(value)
        return result
