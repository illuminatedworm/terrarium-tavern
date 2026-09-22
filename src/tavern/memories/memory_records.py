"""Bounded branch memory, with evidence kept separate from user assertions.

These are a projection of story history, not a second MVU state database. Omitted
operations do not delete records. Tombstones prevent an old prompt from reviving
user-deleted content. Source hashes detect edits; they deliberately do not trigger
automatic rereading of an already archived source.
"""
import copy
import hashlib
import json
import uuid
from typing import Literal

from pydantic import BaseModel, Field

CATEGORIES = {"current": "当前进展", "episode": "变化与经历", "open": "未完成事项",
              "uncertain": "待确认内容", "manual": "手工记忆"}


def usage_fields(usage):
    result = dict(usage or {})
    for field in ("prompt_tokens", "completion_tokens", "cached_tokens"):
        result.setdefault(field, None)
    return result


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def source_hash(message):
    # Hash stored text AND its macro inputs: a displayed identical template can
    # describe a different event after its snapshot variables have changed.
    snap = message.get("snapshot", {})
    if isinstance(snap, str):
        snap = json.loads(snap)
    return digest([message.get("model", message.get("raw", "")), snap.get("state", {}),
                   snap.get("content")])


class Evidence(BaseModel):
    message_id: str
    fingerprint: str = ""
    excerpt: str = Field(min_length=1, max_length=2000)


class Record(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    category: Literal["current", "episode", "open", "uncertain", "manual"]
    content: str = Field(min_length=1, max_length=200000)
    kind: Literal["fact", "claim", "inference"] = "fact"
    people: list[str] = Field(default_factory=list, max_length=20)
    story_time: str = "未知"
    known_to: list[str] = Field(default_factory=list, max_length=20)
    status: Literal["active", "completed", "superseded", "deleted"] = "active"
    sources: list[Evidence] = Field(default_factory=list, max_length=8)
    setting_refs: list[str] = Field(default_factory=list, max_length=8)
    manual: bool = False
    callback: bool = False


class RecordEdit(BaseModel):
    expected_revision: int
    action: Literal["add", "edit", "delete"]
    id: str | None = None
    content: str = Field(default="", max_length=20000)
    category: Literal["current", "episode", "open", "uncertain", "manual"] = "manual"
    kind: Literal["fact", "claim", "inference"] = "fact"


def read_memory(snapshot):
    memory = snapshot.get("memory_document", {"version": 1, "records": []})
    if memory.get("version") != 1:
        raise ValueError("记忆格式不受支持")
    return copy.deepcopy(memory)


def render_document(memory):
    parts = []
    for category, title in CATEGORIES.items():
        lines = []
        for r in memory["records"]:
            if r["category"] != category or r["status"] in ("deleted", "superseded"):
                continue
            labels = []
            if r.get("manual"):
                labels.append("手工修订")
            if r["kind"] != "fact":
                labels.append({"claim": "角色说法，未当作客观事实", "inference": "有待核实的推断"}[r["kind"]])
            if r["status"] == "completed":
                labels.append("已完成")
            if r.get("source_stale"):
                labels.append("原文已修改，尚未重建")
            if r.get("known_to"):
                labels.append("明确知情：" + "、".join(r["known_to"]))
            if r.get("story_time") and r["story_time"] != "未知":
                labels.append("时间：" + r["story_time"])
            lines.append("- " + ("（" + "；".join(labels) + "）" if labels else "") + r["content"])
        if lines:
            parts.append(title + "：\n" + "\n".join(lines))
    return "\n\n".join(parts)


def with_source_status(memory, history):
    result = copy.deepcopy(memory)
    by_id = {m["id"]: m for m in history}
    for r in result["records"]:
        for source in r.get("sources", []):
            m = by_id.get(source["message_id"])
            source["state"] = ("missing" if m is None else "unverified" if not source.get("fingerprint")
                               else "valid" if source_hash(m) == source["fingerprint"] else "changed")
        r["source_stale"] = any(s["state"] in ("changed", "missing") for s in r.get("sources", []))
    return result


def install_memory(snapshot, memory):
    snapshot["memory_document"] = copy.deepcopy(memory)


def manual_edit(memory, edit):
    result = copy.deepcopy(memory)
    if edit.action == "add":
        if not edit.content.strip():
            raise ValueError("记忆内容不能为空")
        result["records"].append(Record(category=edit.category, content=edit.content.strip(),
                                         kind=edit.kind, manual=True).model_dump())
    else:
        record = next((r for r in result["records"] if r["id"] == edit.id), None)
        if record is None or record["status"] == "deleted":
            raise ValueError("记忆条目不存在")
        record["manual"] = True
        if edit.action == "delete":
            record["status"] = "deleted"
            record["deleted_hash"] = digest(record["content"].strip())
            # Keep message identity separate so a fork can remap it even though
            # the deleted excerpt itself no longer occupies every later snapshot.
            record["deleted_sources"] = [{"message_id": s["message_id"], "excerpt_hash": digest(s["excerpt"])}
                                         for s in record["sources"]]
            record["content"] = record["content"][:256]
            record["sources"] = []
            record["setting_refs"] = []
        else:
            if not edit.content.strip():
                raise ValueError("记忆内容不能为空")
            record.update(content=edit.content.strip(), category=edit.category, kind=edit.kind)
    return result
