"""Public settings documents and revision-relative field updates for local clients."""

from functools import cache
from sqlite3 import Connection
from typing import Literal

from tavern.core.prompts import PROMPT_DEFINITIONS, STORY_SYSTEM_PROMPT_DEFINITION
from tavern.core.settings import SettingsDocument

Scope = Literal["story", "shared"]


def get_path(value, path):
    for part in path.split("."):
        value = value[int(part)] if isinstance(value, list) else value[part]
    return value


# Only code-defined page/prompt metadata is shared. User settings always come from SQLite.
@cache
def prompt_catalog():
    result = []
    from tavern.advisor.config import SYSTEM
    from tavern.character.editor_config import SYSTEM as EDITOR_SYSTEM

    result.append(
        {
            "scope": "shared",
            "path": "advisor.system_prompt",
            "label": "参谋 agent 系统提示词",
            "description": "参谋 agent 的咨询方式与项目资料说明；实际写入权限由 Runtime 控制。",
            "default": SYSTEM,
            "choices": True,
        }
    )
    result.append(
        {
            "scope": "shared",
            "path": "card_author_config.system_prompt",
            "label": "制卡 agent 系统提示词",
            "description": "制卡 agent 的创作方式与工作规则；保存后从下一次发送或继续生效，工具权限仍由 Runtime 控制。",
            "default": EDITOR_SYSTEM,
            "choices": True,
        }
    )
    defaults = SettingsDocument().story.model_dump()
    for prompt in (STORY_SYSTEM_PROMPT_DEFINITION, *PROMPT_DEFINITIONS):
        path = prompt["path"]
        default = get_path(defaults, path)
        definition = {
            **prompt,
            "scope": "story",
            "path": path,
            "label": prompt["label"].replace("Reranker", "重排模型"),
            "default": default,
        }
        if isinstance(default, dict):
            definition.update(choices=True, default=default["options"][0]["text"])
        result.append(definition)
    return result


@cache
def page_catalog():
    result = []
    prompts = prompt_catalog()
    fields = SettingsDocument().story.model_dump()
    retrieval = [f"retrieval.{key}" for key in fields["retrieval"]]
    retrieval += [f"local_runtime.{key}" for key in ("reranker_enabled", "rerank_fragments")]
    retrieval += [key for key in fields if key.startswith("lore_")]
    for key, label, paths in [
        ("model", "模型与生成", ["model", "official_model", "default_channel", "policy"]),
        ("retrieval", "检索与世界书", retrieval),
        ("memory", "工具与记忆", ["tools"]),
        ("media", "配音与生图", ["media"]),
        ("prompts", "提示词", [p["path"] for p in prompts if p["scope"] == "story"]),
        ("novel", "资料调查", ["novel_research", "model.message_adapter"]),
    ]:
        result.append({"id": f"story.{key}", "scope": "story", "key": key, "label": label, "paths": paths})
    for key, label, paths in [
        ("providers", "提供商", ["providers"]),
        ("advisor", "参谋 agent", ["advisor"]),
        ("editor", "制卡 agent", ["card_author", "card_author_config"]),
        ("preference_presets", "常用偏好", ["preference_presets"]),
        ("web", "联网与代理", ["web", "network", "model_request_mode"]),
        ("local", "本地资源", ["local_models"]),
        ("storage", "数据与存储", ["log_retention_days", "log_max_mib", "retrieval_cache_max_mib"]),
        (
            "misc",
            "杂项",
            [
                "send_shortcut",
                "debug_failed_requests",
                "debug_story_prefix",
                "kemini",
                "unicode_story_enabled",
            ],
        ),
    ]:
        result.append({"id": f"shared.{key}", "scope": "shared", "key": key, "label": label, "paths": paths})
    return result


def public_scope(doc: SettingsDocument, scope: Scope, fields: set[str] | None = None) -> dict:
    from tavern.web.config import public_web

    # Redact before diffing. Neither an individual path nor a replacement
    # collection may expose a secret, even when the acknowledged value changed.
    result = getattr(doc, scope).model_dump(include=fields, exclude={"web"} if scope == "shared" else None)
    if scope == "shared":
        if fields is None or "web" in fields:
            result["web"] = public_web(doc.shared.web)
        for provider in result.get("providers", ()):
            provider["has_api_key"] = bool(provider.pop("api_key"))
    return result


def public_settings(doc: SettingsDocument) -> dict:
    return {
        "version": doc.version,
        "revisions": doc.revisions,
        "story": public_scope(doc, "story"),
        "shared": public_scope(doc, "shared"),
        "pages": page_catalog(),
        "prompt_definitions": prompt_catalog(),
    }


def changed_paths(before, after, path: str, changes: dict) -> None:
    if before == after:
        return
    if isinstance(after, dict) and isinstance(before, dict) and after.keys() == before.keys():
        # User-defined keys (e.g. provider IDs) may contain dots. Replace the
        # containing map when a segment cannot be expressed by a settings path.
        if all(key and "." not in key for key in after):
            for key, value in after.items():
                changed_paths(before[key], value, f"{path}.{key}", changes)
            return
    if isinstance(after, list) and isinstance(before, list) and len(after) == len(before):
        for index, value in enumerate(after):
            changed_paths(before[index], value, f"{path}.{index}", changes)
        return
    changes[path] = after


def settings_view(
    doc: SettingsDocument, known: dict | None, previous: SettingsDocument | None = None
) -> dict:
    if known is None:
        return public_settings(doc)
    changes = {}
    for scope, revision in doc.revisions.items():
        if known.get(scope) == revision:
            continue
        if previous is None or known.get(scope) != previous.revisions[scope]:
            # The client missed a prior write in this scope. Replace its fields
            # from the authoritative document before advancing its revision.
            changes[scope] = public_scope(doc, scope)
            continue
        old, new = getattr(previous, scope), getattr(doc, scope)
        fields = {key for key in type(new).model_fields if getattr(old, key) != getattr(new, key)}
        updated = {}
        if scope == "shared" and "preference_presets" in fields:
            earlier, current = old.preference_presets, new.preference_presets
            if len(current) == len(earlier) + 1 and all(a == b for a, b in zip(earlier, current)):
                # The ordered prefix is already on the client. Serialize just the new item.
                updated[f"preference_presets.{len(earlier)}"] = current[-1].model_dump()
                fields.remove("preference_presets")
        before = public_scope(previous, scope, fields)
        for key, value in public_scope(doc, scope, fields).items():
            changed_paths(before[key], value, key, updated)
        if updated:
            changes[scope] = updated
    return {"revisions": doc.revisions, "changes": changes}


def settings_since(db: Connection, story_revision: int, shared_revision: int) -> dict:
    known = {"story": story_revision, "shared": shared_revision}
    # Read revisions and, only when stale, their exact document in one statement.
    raw = db.execute(
        "SELECT CASE WHEN json_extract(value,'$.revisions.story')=? AND json_extract(value,'$.revisions.shared')=? "
        "THEN NULL ELSE value END FROM meta WHERE key='settings'",
        (story_revision, shared_revision),
    ).fetchone()[0]
    if raw is None:
        return {"revisions": known, "changes": {}}
    return settings_view(SettingsDocument.model_validate_json(raw), known)
