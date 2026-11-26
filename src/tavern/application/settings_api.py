"""Scoped, revision-checked writes and short-lived, secret-preserving page undo."""

import time
import uuid
from copy import deepcopy
from uuid import UUID

from fastapi import Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, ValidationError

from tavern.application.settings_view import (
    Scope,
    get_path,
    page_catalog,
    public_settings,
    settings_since,
    settings_view,
)
from tavern.core.channels import profile_key
from tavern.core.errors import NotFound
from tavern.core.json_values import same_json
from tavern.core.models import PreferencePreset
from tavern.core.settings import SettingsDocument
from tavern.search.retrieval_config import ModelProfile
from tavern.storage.store import Conflict


def put_path(value, path, replacement):
    parts = path.split(".")
    for part in parts[:-1]:
        value = value[int(part)] if isinstance(value, list) else value[part]
    if not isinstance(value, list) and parts[-1] not in value:
        raise ValueError("未知设置字段")
    value[int(parts[-1]) if isinstance(value, list) else parts[-1]] = deepcopy(replacement)


def editable(value):
    if isinstance(value, dict):
        return {
            key: editable(item) for key, item in value.items() if key not in {"capability", "capability_key"}
        }
    if isinstance(value, list):
        return [editable(item) for item in value]
    return value


class SettingsRead(BaseModel):
    known_revisions: dict[Scope, int] | None = None


class SettingsPatch(SettingsRead):
    expected_revision: int = Field(ge=0)
    changes: dict
    edit_session: str | None = None


class UndoRequest(SettingsRead):
    page: str
    expected_revision: int = Field(ge=0)


class PresetAppend(SettingsRead):
    preset: PreferencePreset


def remember_edits(editor, scope, paths, document):
    latest = editor["latest"][scope]
    fields = {path.split(".", 1)[0] for path in paths}
    saved = getattr(document, scope).model_dump(include=fields)
    for path in paths:
        root, dot, _ = path.partition(".")
        if not dot:
            latest[root] = saved[root]
        else:
            if root not in latest:
                # Start with this editor's baseline, not another window's changes
                # to sibling paths. Undo must still detect those conflicts.
                latest[root] = deepcopy(editor["baseline"][scope][root])
            try:
                put_path(latest, path, get_path(saved, path))
            except (IndexError, KeyError, TypeError, ValueError):
                # Another window can create a nullable profile or a list slot absent
                # from our baseline. Keep that ancestor divergent for the undo check.
                continue


def validate_document(raw):
    try:
        return SettingsDocument.model_validate(raw)
    except ValidationError as error:
        # Pydantic's default exception text includes rejected values, including secrets.
        raise ValueError(
            "；".join(
                f"{'.'.join(map(str, e['loc']))}：{e['msg']}" for e in error.errors(include_input=False)
            )
        ) from None


def same_probe_config(before: ModelProfile, after: ModelProfile) -> bool:
    return all(
        getattr(before, key) == getattr(after, key)
        for key, field in ModelProfile.model_fields.items()
        if not field.exclude and key not in {"capability", "capability_key"}
    )


def invalidate_capabilities(old, new):
    providers_changed = old.shared.providers != new.shared.providers
    for previous, updated in (
        (old.story.novel_research, new.story.novel_research),
        (old.shared.advisor, new.shared.advisor),
    ):
        for key in ("model", "official_model"):
            before, after = getattr(previous, key), getattr(updated, key)
            if after is not None and (
                providers_changed or before is None or not same_probe_config(before, after)
            ):
                after.capability, after.capability_key = "unknown", ""
    for scope, key, after in new.profiles():
        before = getattr(getattr(old, scope), key)
        if before is None or not before.provider_id or not after.provider_id:
            after.capability, after.capability_key = "unknown", ""
            continue
        old_provider = next(p for p in old.shared.providers if p.id == before.provider_id)
        provider = next(p for p in new.shared.providers if p.id == after.provider_id)
        if (
            before.model,
            before.provider_id,
            old_provider.base_url,
            before.api_mode,
            before.api_path,
            old_provider.auth_mode,
            old_provider.chatgpt_account_id,
        ) != (
            after.model,
            after.provider_id,
            provider.base_url,
            after.api_mode,
            after.api_path,
            provider.auth_mode,
            provider.chatgpt_account_id,
        ):
            after.capability, after.capability_key = "unknown", ""


def save_changes(store, db, old: SettingsDocument, raw: dict) -> SettingsDocument:
    new = validate_document(raw)
    invalidate_capabilities(old, new)
    for scope in new.revisions:
        if getattr(new, scope) != getattr(old, scope):
            new.revisions[scope] += 1
    if new.revisions != old.revisions:
        store.write_settings(db, old, new)
    return new


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

    @app.get("/api/debug/failed-requests")
    async def failed_requests(offset: int = Query(default=0, ge=0)):
        from tavern.storage.request_debug import BODY_LIMIT, MAX_RECORDS, RequestDebugLog

        log = RequestDebugLog(store.root, store.settings())
        return {
            "items": log.records(offset),
            "total": len(log.entries()),
            "body_limit_bytes": BODY_LIMIT,
            "display_limit": MAX_RECORDS,
        }

    @app.delete("/api/debug/failed-requests")
    async def clear_failed_requests():
        from tavern.storage.maintenance import clear_request_debug

        return clear_request_debug(store)

    @app.get("/api/debug/failed-requests/{identity}")
    async def download_failed_request(identity: UUID):
        from tavern.storage.request_debug import RequestDebugLog

        path = RequestDebugLog(store.root, store.settings()).path(identity.hex)
        if not path.is_file():
            raise NotFound("失败请求记录不存在或已清理")
        return FileResponse(
            path,
            media_type="application/zip",
            filename=f"failed-request-{identity.hex}.zip",
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/settings")
    def read_settings(story_revision: int | None = None, shared_revision: int | None = None):
        if story_revision is not None and shared_revision is not None:
            with store.connection() as db:
                return settings_since(db, story_revision, shared_revision)
        return public_settings(store.settings_document())

    @app.post("/api/settings/edit-sessions")
    async def open_editor(payload: SettingsRead | None = None):
        prune()
        if len(snapshots) >= 64:
            raise Conflict("设置编辑窗口过多，请先关闭其他窗口")
        doc = store.settings_document()
        token = uuid.uuid4().hex
        snapshots[token] = {
            "baseline": doc.model_dump(),
            "latest": {"story": {}, "shared": {}},
            "expires": time.monotonic() + 7200,
        }
        return {"id": token, "settings": settings_view(doc, payload.known_revisions if payload else None)}

    @app.delete("/api/settings/edit-sessions/{token}")
    async def close_editor(token: str):
        snapshots.pop(token, None)
        return {"ok": True}

    @app.post("/api/settings/preference-presets")
    def append_preset(payload: PresetAppend):
        # Deduplicate and append to the current list under the same write lock.
        # Independent windows may hold older copies of the list.
        with store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            old = store.settings_document(db=db)
            content = payload.preset.content.strip()
            existing = next((p for p in old.shared.preference_presets if p.content.strip() == content), None)
            if existing is None:
                raw = old.model_dump()
                raw["shared"]["preference_presets"].append(payload.preset.model_dump())
                new = save_changes(store, db, old, raw)
            else:
                new = old
        preset = existing or payload.preset
        return {
            "id": preset.id,
            "name": preset.name,
            "created": existing is None,
            "settings": settings_view(new, payload.known_revisions, old),
        }

    @app.patch("/api/settings/{scope}")
    async def patch(scope: Scope, payload: SettingsPatch):
        editor = snapshot(payload.edit_session) if payload.edit_session else None
        with store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            old = store.settings_document(db=db)
            if old.revisions[scope] != payload.expected_revision:
                raise Conflict("设置已在其他窗口改变，请重新载入后再修改；当前草稿仍保留")
            raw = old.model_dump()
            for path, value in payload.changes.items():
                if scope == "shared" and path.startswith("web") and "has_api_key" in path.split("."):
                    raise ValueError("密钥状态只能由服务器更新")
                if any(part in {"capability", "capability_key"} for part in path.split(".")):
                    raise ValueError("模型能力只能通过连接检查更新")
                try:
                    original = get_path(raw[scope], path)
                except (IndexError, KeyError, TypeError, ValueError):
                    raise ValueError("未知设置字段") from None
                if path == "providers" and scope == "shared":
                    existing = {p["id"]: p for p in original}
                    value = deepcopy(value)
                    for provider in value:
                        provider.pop("has_api_key", None)
                        previous = existing.get(provider.get("id"))
                        if previous and provider.get("auth_mode", "api_key") != previous.get(
                            "auth_mode", "api_key"
                        ):
                            raise ValueError("供应商渠道创建后不可修改；请在另一渠道新建连接")
                        if not provider.get("api_key"):
                            provider["api_key"] = existing.get(provider.get("id"), {}).get("api_key", "")
                if scope == "shared" and isinstance(value, dict):
                    from tavern.web.config import merge_web_secrets

                    if path == "web":
                        value = merge_web_secrets(value, original)
                    elif path in {"web.exa", "web.tavily"}:
                        name = path.split(".")[1]
                        value = merge_web_secrets({name: value}, {name: original})[name]
                if isinstance(value, dict):
                    # Parent profile writes cannot forge server-owned capability fields either.
                    def protect(v, o):
                        for k, child in v.items():
                            if k in {"capability", "capability_key"} and child != o.get(
                                k, "unknown" if k == "capability" else ""
                            ):
                                raise ValueError("模型能力只能通过连接检查更新")
                            if isinstance(child, dict):
                                protect(child, o.get(k, {}))

                    protect(value, original if isinstance(original, dict) else {})
                put_path(raw[scope], path, value)
            new = save_changes(store, db, old, raw)
        if editor:
            remember_edits(editor, scope, payload.changes, new)
        return settings_view(new, payload.known_revisions, old)

    @app.post("/api/settings/edit-sessions/{token}/undo")
    async def undo(token: str, payload: UndoRequest):
        editor = snapshot(token)
        if payload.page not in pages:
            raise ValueError("未知设置页")
        page = pages[payload.page]
        scope = page["scope"]
        with store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            old = store.settings_document(db=db)
            if old.revisions[scope] != payload.expected_revision:
                raise Conflict("设置已在其他窗口改变，未执行撤回")
            raw = old.model_dump()
            latest = editor["baseline"][scope] | editor["latest"][scope]
            for path in page["paths"]:
                current = get_path(raw[scope], path)
                expected = get_path(latest, path)
                if not same_json(current, expected) and not same_json(editable(current), editable(expected)):
                    raise Conflict("本页设置已被其他操作修改，未执行撤回")
                replacement = get_path(editor["baseline"][scope], path)
                if current is not None and isinstance(replacement, dict) and "capability" in replacement:
                    replacement = {
                        **replacement,
                        "capability": current["capability"],
                        "capability_key": current["capability_key"],
                    }
                put_path(raw[scope], path, replacement)
            new = save_changes(store, db, old, raw)
        remember_edits(editor, scope, page["paths"], new)
        return settings_view(new, payload.known_revisions, old)


def save_probe(store, role, result, captured):
    if role == "novel_research":
        with store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            doc = SettingsDocument.model_validate_json(
                db.execute("SELECT value FROM meta WHERE key='settings'").fetchone()[0]
            )
            key = "official_model" if captured._settings_channel == "chatgpt" else "model"
            profile = getattr(doc.story.novel_research, key)
            before = captured.novel_research
            provider = next(
                (p for p in doc.shared.providers if profile and p.id == profile.provider_id), None
            )
            old_provider = next(
                (p for p in captured.providers if before and p.id == before.provider_id), None
            )
            if (
                profile is None
                or before is None
                or provider != old_provider
                or not same_probe_config(before, profile)
            ):
                raise Conflict("检查期间资料调查模型连接已改变，请重新检查")
            profile.capability, profile.capability_key = result.capability, result.capability_key
            doc.revisions["story"] += 1
            db.execute("UPDATE meta SET value=? WHERE key='settings'", (doc.model_dump_json(),))
        return
    if role == "advisor":
        with store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            doc = SettingsDocument.model_validate_json(
                db.execute("SELECT value FROM meta WHERE key='settings'").fetchone()[0]
            )
            key = "official_model" if captured._settings_channel == "chatgpt" else "model"
            profile = getattr(doc.shared.advisor, key)
            current = doc.runtime(captured._settings_channel)
            current.advisor = profile
            from tavern.scripting.request_context import response_origin

            if profile is None or response_origin(current.for_role("advisor")) != response_origin(
                captured.for_role("advisor")
            ):
                raise Conflict("检查期间参谋 agent 模型连接已改变，请重新检查")
            profile.capability, profile.capability_key = result.capability, result.capability_key
            doc.revisions["shared"] += 1
            db.execute("UPDATE meta SET value=? WHERE key='settings'", (doc.model_dump_json(),))
        return
    scope = "shared" if role == "card_author" else "story"
    with store.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        doc = SettingsDocument.model_validate_json(
            db.execute("SELECT value FROM meta WHERE key='settings'").fetchone()[0]
        )
        channel = captured._settings_channel
        key = profile_key(role, channel)
        current = doc.runtime(channel).for_role(role)
        before = captured.for_role(role)
        if (
            current.model,
            current.base_url,
            current.api_mode,
            current.api_key,
            current.auth_mode,
            current.chatgpt_account_id,
        ) != (
            before.model,
            before.base_url,
            before.api_mode,
            before.api_key,
            before.auth_mode,
            before.chatgpt_account_id,
        ):
            raise Conflict("检查期间模型连接已改变，请重新检查")
        profile = getattr(getattr(doc, scope), key)
        profile.capability, profile.capability_key = result.capability, result.capability_key
        doc.revisions[scope] += 1
        db.execute("UPDATE meta SET value=? WHERE key='settings'", (doc.model_dump_json(),))
