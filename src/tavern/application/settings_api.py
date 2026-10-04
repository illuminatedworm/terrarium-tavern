"""Scoped, revision-checked writes and short-lived, secret-preserving page undo."""
import time
import uuid
from copy import deepcopy
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from tavern.core.prompts import (
    FULL_AGENT_SYSTEM_PROMPT_DEFINITION,
    HALF_AGENT_SYSTEM_PROMPT_DEFINITION,
    PROMPT_DEFINITIONS,
)
from tavern.core.settings import SettingsDocument
from tavern.storage.store import Conflict

Scope = Literal["full_agent", "half_agent", "shared"]


def get_path(value, path):
    for part in path.split("."):
        value = value[int(part)] if isinstance(value, list) else value[part]
    return value


def put_path(value, path, replacement):
    parts = path.split(".")
    for part in parts[:-1]:
        value = value[int(part)] if isinstance(value, list) else value[part]
    if not isinstance(value, list) and parts[-1] not in value:
        raise ValueError("未知设置字段")
    value[int(parts[-1]) if isinstance(value, list) else parts[-1]] = deepcopy(replacement)


def editable(value):
    if isinstance(value, dict):
        return {key: editable(item) for key, item in value.items() if key not in {"capability", "capability_key"}}
    if isinstance(value, list):
        return [editable(item) for item in value]
    return value


def prompt_catalog():
    result = []
    for scope in ("full_agent", "half_agent"):
        defaults = getattr(SettingsDocument(), scope).model_dump()
        system_prompt = (FULL_AGENT_SYSTEM_PROMPT_DEFINITION if scope == "full_agent"
                         else HALF_AGENT_SYSTEM_PROMPT_DEFINITION)
        for prompt in (system_prompt, *PROMPT_DEFINITIONS):
            path = prompt["path"].replace("local_models.", "local_runtime.")
            if scope == "full_agent" and path in {"tracker_system_prompt", "retrieval.query_prompt", "retrieval.protocol_prompt"}:
                continue
            result.append({**prompt, "scope": scope, "path": path,
                           "label": "系统提示词" if path == "system_prompt" else prompt["label"].replace("Reranker", "重排模型"),
                           "default": get_path(defaults, path)})
    return result


def page_catalog():
    result = []
    prompts = prompt_catalog()
    for scope in ("full_agent", "half_agent"):
        fields = getattr(SettingsDocument(), scope).model_dump()
        prompt_paths = [p["path"] for p in prompts if p["scope"] == scope]
        retrieval = [f"retrieval.{key}" for key in fields["retrieval"] if not key.endswith("prompt")]
        retrieval += [f"local_runtime.{key}" for key in ("reranker_enabled", "rerank_fragments")]
        retrieval += [key for key in fields if key.startswith("lore_")]
        pages = [("model", "模型与生成", ["model", "policy"] if scope == "full_agent" else ["foreground", "background", "policy"]),
                 ("retrieval", "检索与世界书", retrieval),
                 ("memory", "工具与记忆" if scope == "full_agent" else "历史与记忆",
                  ["tools"] if scope == "full_agent" else [key for key in fields if key.startswith(("chunk_", "history_", "memory_", "context_pressure_", "daily_", "tracker_")) and not key.endswith("prompt")]),
                 ("prompts", "提示词", prompt_paths)]
        if scope == "half_agent":
            pages.append(("recipe", "上下文编排", ["context_recipe"]))
        for key, label, paths in pages:
            result.append({"id": f"{scope}.{key}", "scope": scope, "key": key, "label": label, "paths": paths})
    for key, label, paths in [("providers", "提供商", ["providers"]),
                              ("preference_presets", "常用偏好", ["preference_presets"]),
                              ("editor", "角色卡编辑", ["card_editor"]),
                              ("local", "本地模型", ["local_models"]), ("network", "网络与代理", ["network"]),
                              ("storage", "存储与维护", ["log_retention_days", "log_max_mib", "retrieval_cache_max_mib"]),
                              ("data_transfer", "数据导入导出", [])]:
        result.append({"id": f"shared.{key}", "scope": "shared", "key": key, "label": label, "paths": paths})
    return result


def public_settings(doc):
    result = doc.model_dump()
    for provider in result["shared"]["providers"]:
        provider["has_api_key"] = bool(provider.pop("api_key"))
    result.update(pages=page_catalog(), prompt_definitions=prompt_catalog())
    return result


class SettingsPatch(BaseModel):
    expected_revision: int = Field(ge=0)
    changes: dict
    edit_session: str | None = None


class UndoRequest(BaseModel):
    page: str
    expected_revision: int = Field(ge=0)


def validate_document(raw):
    try:
        return SettingsDocument.model_validate(raw)
    except ValidationError as error:
        # Pydantic's default exception text includes rejected values, including secrets.
        raise ValueError("；".join(f"{'.'.join(map(str, e['loc']))}：{e['msg']}"
                                  for e in error.errors(include_input=False))) from None


def invalidate_capabilities(old, new):
    for scope, key in (("full_agent", "model"), ("half_agent", "foreground"), ("half_agent", "background"), ("shared", "card_editor")):
        before, after = getattr(getattr(old, scope), key), getattr(getattr(new, scope), key)
        old_provider = next(p for p in old.shared.providers if p.id == before.provider_id)
        provider = next(p for p in new.shared.providers if p.id == after.provider_id)
        if (before.model, before.provider_id, old_provider.base_url, before.api_mode, before.api_path) != (after.model, after.provider_id, provider.base_url, after.api_mode, after.api_path):
            after.capability, after.capability_key = "unknown", ""


def register_settings_routes(app, store):
    snapshots = {}
    pages = {page["id"]: page for page in page_catalog()}

    def prune():
        for key in list(snapshots):
            if snapshots[key]["expires"] < time.monotonic():
                del snapshots[key]

    def snapshot(token):
        prune()
        if token not in snapshots:
            raise Conflict("设置编辑已过期，请关闭并重新打开设置")
        snapshots[token]["expires"] = time.monotonic() + 7200
        return snapshots[token]

    @app.get("/api/settings")
    async def read_settings():
        return public_settings(store.settings_document())

    @app.post("/api/settings/edit-sessions")
    async def open_editor():
        prune()
        if len(snapshots) >= 64:
            raise Conflict("设置编辑窗口过多，请先关闭其他窗口")
        doc = store.settings_document()
        token = uuid.uuid4().hex
        snapshots[token] = {"baseline": doc.model_dump(), "latest": doc.model_dump(), "expires": time.monotonic() + 7200}
        return {"id": token, "settings": public_settings(doc)}

    @app.delete("/api/settings/edit-sessions/{token}")
    async def close_editor(token: str):
        snapshots.pop(token, None)
        return {"ok": True}

    @app.patch("/api/settings/{scope}")
    async def patch(scope: Scope, payload: SettingsPatch):
        editor = snapshot(payload.edit_session) if payload.edit_session else None
        with store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            old = SettingsDocument.model_validate_json(db.execute("SELECT value FROM meta WHERE key='settings'").fetchone()[0])
            if old.revisions[scope] != payload.expected_revision:
                raise Conflict("设置已在其他窗口改变，请重新载入后再修改；当前草稿仍保留")
            raw = old.model_dump()
            for path, value in payload.changes.items():
                if any(part in {"capability", "capability_key"} for part in path.split(".")):
                    raise ValueError("模型能力只能通过连接检查更新")
                try:
                    original = get_path(raw[scope], path)
                except (KeyError, TypeError):
                    raise ValueError("未知设置字段") from None
                if path == "providers" and scope == "shared":
                    existing = {p["id"]: p for p in original}
                    value = deepcopy(value)
                    for provider in value:
                        provider.pop("has_api_key", None)
                        if not provider.get("api_key"):
                            provider["api_key"] = existing.get(provider.get("id"), {}).get("api_key", "")
                if isinstance(value, dict):
                    # Parent profile writes cannot forge server-owned capability fields either.
                    def protect(v, o):
                        for k, child in v.items():
                            if k in {"capability", "capability_key"} and child != o.get(k):
                                raise ValueError("模型能力只能通过连接检查更新")
                            if isinstance(child, dict):
                                protect(child, o.get(k, {}))
                    protect(value, original if isinstance(original, dict) else {})
                put_path(raw[scope], path, value)
            new = validate_document(raw)
            invalidate_capabilities(old, new)
            # A shared connection edit can invalidate probes in either mode.
            for name in new.revisions:
                if getattr(new, name) != getattr(old, name):
                    new.revisions[name] += 1
            db.execute("UPDATE meta SET value=? WHERE key='settings'", (new.model_dump_json(),))
        if editor:
            for path in payload.changes:
                put_path(editor["latest"][scope], path, get_path(new.model_dump()[scope], path))
        return public_settings(new)

    @app.post("/api/settings/edit-sessions/{token}/undo")
    async def undo(token: str, payload: UndoRequest):
        editor = snapshot(token)
        if payload.page not in pages:
            raise ValueError("未知设置页")
        page = pages[payload.page]
        scope = page["scope"]
        with store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            old = SettingsDocument.model_validate_json(db.execute("SELECT value FROM meta WHERE key='settings'").fetchone()[0])
            if old.revisions[scope] != payload.expected_revision:
                raise Conflict("设置已在其他窗口改变，未执行撤回")
            raw = old.model_dump()
            for path in page["paths"]:
                current = get_path(raw[scope], path)
                if editable(current) != editable(get_path(editor["latest"][scope], path)):
                    raise Conflict("本页设置已被其他操作修改，未执行撤回")
                replacement = deepcopy(get_path(editor["baseline"][scope], path))
                if isinstance(replacement, dict) and "capability" in replacement:
                    for field in ("capability", "capability_key"):
                        replacement[field] = current[field]
                put_path(raw[scope], path, replacement)
            new = validate_document(raw)
            invalidate_capabilities(old, new)
            for name in new.revisions:
                if getattr(new, name) != getattr(old, name):
                    new.revisions[name] += 1
            db.execute("UPDATE meta SET value=? WHERE key='settings'", (new.model_dump_json(),))
        for path in page["paths"]:
            put_path(editor["latest"][scope], path, get_path(new.model_dump()[scope], path))
        return public_settings(new)


def save_probe(store, role, result, captured):
    scope, key = {"full_agent": ("full_agent", "model"), "foreground": ("half_agent", "foreground"),
                  "background": ("half_agent", "background"), "card_editor": ("shared", "card_editor")}[role]
    with store.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        doc = SettingsDocument.model_validate_json(db.execute("SELECT value FROM meta WHERE key='settings'").fetchone()[0])
        current = doc.runtime("full_agent" if role == "full_agent" else "half_agent").for_role(role)
        before = captured.for_role(role)
        if (current.model, current.base_url, current.api_mode, current.api_key) != (before.model, before.base_url, before.api_mode, before.api_key):
            raise Conflict("检查期间模型连接已改变，请重新检查")
        profile = getattr(getattr(doc, scope), key)
        profile.capability, profile.capability_key = result.capability, result.capability_key
        doc.revisions[scope] += 1
        db.execute("UPDATE meta SET value=? WHERE key='settings'", (doc.model_dump_json(),))
