"""Explicit catalogue refreshes, with durable connection-specific cached reads."""
import json
import time

import httpx

from tavern.core.cache_policy import endpoint, fingerprint
from tavern.search.retrieval_config import ApiMode, ModelProfile


def register_model_routes(app, store):
    def connection(profile):
        config = store.settings_document().shared
        provider = next((p for p in config.providers if p.id == profile.provider_id), None)
        if provider is None:
            raise ValueError("提供商不存在，请先保存设置")
        if provider.auth_mode == "chatgpt_subscription":
            from tavern.chatgpt import API_ROOT
            url = API_ROOT + "/models"
            key = "provider_models:" + fingerprint([provider.id, url, "chatgpt_subscription_v2",
                provider.chatgpt_account_id, store.chatgpt.generation(provider.chatgpt_account_id)])
            return config, provider, url, key
        url = endpoint(provider.base_url, profile.api_mode, resource="models", api_path=profile.api_path)
        key = "provider_models:" + fingerprint([provider.id, url, profile.api_mode, fingerprint(provider.api_key)])
        return config, provider, url, key

    @app.get("/api/settings/models")
    async def cached_models(provider_id: str, api_mode: ApiMode = "chat_completions", api_path: str = ""):
        _, provider, _, key = connection(ModelProfile(provider_id=provider_id, api_mode=api_mode, api_path=api_path))
        with store.connection() as db:
            row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        result = json.loads(row[0]) if row else {"models": [], "updated_at": None}
        if provider.auth_mode == "chatgpt_subscription" and row:
            store.chatgpt.remember_models(provider.chatgpt_account_id, store.chatgpt.generation(provider.chatgpt_account_id),
                                          result.get("model_details", []))
        return result

    @app.post("/api/settings/models")
    async def refresh_models(profile: ModelProfile):
        config, provider, url, key = connection(profile)
        if provider.auth_mode == "chatgpt_subscription":
            result = await subscription_models(store.chatgpt, provider, url)
            # Reauthorization/exit while listing must not publish an obsolete account catalogue.
            if connection(profile)[3] != key:
                raise ValueError("ChatGPT 账号已改变，请重新更新模型列表")
            with store.connection() as db:
                db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES (?,?)",
                           (key, json.dumps(result, ensure_ascii=False)))
            return result
        mode = profile.api_mode
        headers = ({"x-goog-api-key": provider.api_key} if mode == "gemini" else
                   {"x-api-key": provider.api_key, "anthropic-version": "2023-06-01"} if mode == "anthropic" else
                   {"Authorization": f"Bearer {provider.api_key}"}) if provider.api_key else {}
        proxy = (config.network.proxy or None) if config.network.api_proxy and provider.use_proxy else None
        try:
            async with httpx.AsyncClient(proxy=proxy, trust_env=False, timeout=30) as client:
                response = await client.get(url, headers=headers)
                response.raise_for_status()
                payload = response.json()
            data = payload.get("models" if mode == "gemini" else "data") if isinstance(payload, dict) else None
            if not isinstance(data, list):
                raise ValueError("模型接口未返回有效的模型列表")
            field = "name" if mode == "gemini" else "id"
            result = {"models": sorted({m[field].removeprefix("models/") for m in data
                                        if isinstance(m, dict) and isinstance(m.get(field), str) and m[field]}),
                      "updated_at": time.time()}
        except httpx.HTTPStatusError as error:
            raise ValueError(f"获取模型列表失败：HTTP {error.response.status_code}") from None
        except httpx.RequestError:
            raise ValueError("无法连接模型接口，请检查地址、代理和网络") from None
        except json.JSONDecodeError:
            raise ValueError("模型接口未返回有效的 JSON") from None
        # Only a successful refresh replaces the previous list (including an empty list).
        with store.connection() as db:
            db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES (?,?)",
                       (key, json.dumps(result, ensure_ascii=False)))
        return result


async def subscription_models(accounts, provider, url):
    from tavern.chatgpt.errors import ChatGPTError, response_error
    from tavern.chatgpt.models import model_details
    account = await accounts.access(provider.chatgpt_account_id, use_proxy=provider.use_proxy)
    owner = accounts.attach(provider.chatgpt_account_id, account["generation"])
    token = account["tokens"]["access_token"]
    try:
        async with accounts.client(provider.use_proxy) as client:
            response = await client.get(url, headers={"Authorization": "Bearer " + token})
            try:
                payload = response.json()
            except ValueError:
                payload = {"detail": response.text}
            if response.status_code != 200:
                raise response_error(payload, response.status_code, response.headers.get("x-request-id"), secrets=(token,))
        accounts.require_account(provider.chatgpt_account_id, account["generation"])
        if not isinstance(payload, dict) or not isinstance(payload.get("models"), list):
            raise ChatGPTError("ChatGPT 未返回有效模型目录", code="invalid_model_catalog")
        choices = []
        for item in payload["models"]:
            if (isinstance(item, dict) and item.get("visibility") == "list"
                    and isinstance(item.get("slug"), str) and item["slug"]
                    and item["slug"] not in [choice["id"] for choice in choices]):
                choices.append(model_details(item))
        accounts.remember_models(provider.chatgpt_account_id, account["generation"], choices)
        return {"models": [item["id"] for item in choices], "model_details": choices, "updated_at": time.time()}
    except httpx.HTTPError:
        raise ChatGPTError("无法连接 ChatGPT 模型目录，请检查网络设置", code="catalog_unavailable") from None
    finally:
        accounts.detach(provider.chatgpt_account_id, owner)
