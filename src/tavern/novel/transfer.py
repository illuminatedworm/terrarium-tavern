"""Portable file references and paused research tasks in personal data packages."""

import copy
import hashlib
import io
import json
import time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from tavern.novel.source import MAX_BYTES, directory, import_file
from tavern.search.retrieval_config import ModelProfile
from tavern.storage.store import dump, uid


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Source(Model):
    id: str = Field(pattern=r"^[a-f0-9]{32}$")
    name: str = Field(min_length=1, max_length=255)
    encoding: Literal["utf-8", "utf-8-sig", "utf-16", "gb18030", "big5"]
    bytes: int = Field(ge=1, le=MAX_BYTES)
    characters: int = Field(ge=1, le=MAX_BYTES)
    created: float = Field(ge=0, le=253402300799, allow_inf_nan=False)
    blob: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


class Configuration(Model):
    profile: ModelProfile
    follows_main: bool
    channel: Literal["standard", "chatgpt"]
    max_requests: int = Field(ge=2, le=100)
    timeout_seconds: int = Field(ge=30, le=3600)


class Job(Model):
    id: str = Field(pattern=r"^[a-f0-9]{32}$")
    ref: int = Field(ge=1)
    source: str
    operation: str
    round_position: int = Field(ge=1)
    origin_turn: str
    task: str
    background: str
    status: Literal["running", "waiting", "completed", "paused", "failed", "destroyed"]
    config: Configuration
    progress: dict
    requests: int = Field(ge=0, le=100)
    elapsed: float = Field(ge=0, allow_inf_nan=False)
    usage: dict
    error: str | None
    created: float = Field(ge=0, le=253402300799, allow_inf_nan=False)
    trace: list[dict]
    mail: list[dict]


class Bundle(Model):
    source: str | None
    enabled: bool
    next_ref: int = Field(ge=1)
    sources: list[Source]
    jobs: list[Job]


def export_bundle(store, db, sid, include_files, files):
    binding = db.execute("SELECT * FROM novel_bindings WHERE session=?", (sid,)).fetchone()
    if binding is None:
        return None
    sources = []
    for row in db.execute(
        "SELECT s.* FROM novel_sources s JOIN novel_source_history h ON h.source=s.id WHERE h.session=?",
        (sid,),
    ):
        source = {**dict(row), "blob": None}
        path = directory(store.root, source["id"]) / "original"
        if include_files and path.is_file():
            with path.open("rb") as handle:
                digest = hashlib.file_digest(handle, "sha256").hexdigest()
            source["blob"] = digest
            files[digest] = path
        sources.append(source)
    jobs = []
    for row in db.execute("SELECT * FROM novel_jobs WHERE session=? ORDER BY ref", (sid,)):
        job = dict(row)
        job.pop("session")
        for key in ("config", "progress", "usage"):
            job[key] = json.loads(job[key])
        job["trace"] = [
            json.loads(r[0])
            for r in db.execute("SELECT message FROM novel_trace WHERE job=? ORDER BY seq", (job["id"],))
        ]
        job["mail"] = [
            dict(r)
            for r in db.execute(
                "SELECT sender,kind,content,operation,delivered,revoked,created FROM novel_mail WHERE job=? ORDER BY seq",
                (job["id"],),
            )
        ]
        jobs.append(job)
    return {
        "source": binding["source"],
        "enabled": bool(binding["enabled"]),
        "next_ref": binding["next_ref"],
        "sources": sources,
        "jobs": jobs,
    }


def validate_bundle(raw, blobs, channel, known_sources):
    if raw is None:
        return
    bundle = Bundle.model_validate(raw)
    sources = {source.id: source for source in bundle.sources}
    if len(sources) != len(bundle.sources) or bundle.source is not None and bundle.source not in sources:
        raise ValueError("原著参考文件绑定无效")
    if bundle.enabled and bundle.source is None:
        raise ValueError("原著参考已开启但没有绑定文件")
    for source in bundle.sources:
        if source.id in known_sources and known_sources[source.id] != source.model_dump():
            raise ValueError("多个对话中的同一原著版本存在冲突，不能静默选择其中一个")
        known_sources[source.id] = source.model_dump()
        if source.blob and (source.blob not in blobs or len(blobs[source.blob]) != source.bytes):
            raise ValueError("数据包原著文件缺失或大小不符")
    if len({j.id for j in bundle.jobs}) != len(bundle.jobs) or len({j.ref for j in bundle.jobs}) != len(
        bundle.jobs
    ):
        raise ValueError("原著调查者标识重复")
    if sum(job.status != "destroyed" for job in bundle.jobs) > 3:
        raise ValueError("数据包包含超过三个活跃调查者")
    if any(job.ref >= bundle.next_ref for job in bundle.jobs):
        raise ValueError("原著调查者编号进度无效")
    counts = {}
    for job in bundle.jobs:
        counts[job.round_position] = counts.get(job.round_position, 0) + 1
        if counts[job.round_position] > 5:
            raise ValueError("原著调查记录超过每轮五次启动上限")
        if job.source not in sources or job.config.channel != channel:
            raise ValueError("调查者的文件或模型渠道与所属对话不符")
        if job.requests > job.config.max_requests:
            raise ValueError("调查者已用请求数超过其额度")
        context = job.progress.get("context", {})
        if (
            not isinstance(context, dict)
            or type(context.get("through", 0)) is not int
            or not 0 <= context.get("through", 0) <= len(job.trace)
        ):
            raise ValueError("原著调查压缩范围无效")
        pending = job.progress.get("pending")
        if pending is not None and (
            not isinstance(pending, dict) or not isinstance(pending.get("tool_calls"), list)
        ):
            raise ValueError("原著调查待执行工具格式无效")
        for message in [*job.trace, *([pending] if pending else [])]:
            if message.get("role") not in {"user", "assistant", "tool", "system"} or not isinstance(
                message.get("content"), str
            ):
                raise ValueError("原著调查记录格式无效")
            calls = message.get("tool_calls", [])
            if not isinstance(calls, list) or any(
                not isinstance(c, dict)
                or not isinstance(c.get("id"), str)
                or not isinstance(c.get("function"), dict)
                or not isinstance(c["function"].get("name"), str)
                or not isinstance(c["function"].get("arguments"), str)
                for c in calls
            ):
                raise ValueError("原著调查工具记录格式无效")
        for mail in job.mail:
            if (
                set(mail) != {"sender", "kind", "content", "operation", "delivered", "revoked", "created"}
                or mail["sender"] not in {"main", "worker"}
                or mail["kind"] not in {"message", "report", "question", "error"}
                or not isinstance(mail["content"], str)
                or not isinstance(mail["operation"], str)
                or mail["revoked"] not in (0, 1)
                or mail["delivered"] is not None
                and not isinstance(mail["delivered"], str)
                or type(mail["created"]) not in (int, float)
                or not 0 <= mail["created"] <= 253402300799
            ):
                raise ValueError("原著调查通讯记录格式无效")
    # Persist the validated model defaults too; the UI and runtime must see the
    # same frozen profile rather than separately filling omitted imported fields.
    raw.clear()
    raw.update(bundle.model_dump())


def import_bundle(store, db, sid, raw, blobs, written, source_ids):
    if raw is None:
        return
    for source in raw["sources"]:
        identity = source_ids.setdefault(source["id"], uid())
        if not db.execute("SELECT 1 FROM novel_sources WHERE id=?", (identity,)).fetchone():
            if source["blob"]:
                folder = directory(store.root, identity)
                try:
                    metadata = import_file(
                        store.root,
                        identity,
                        io.BytesIO(blobs[source["blob"]]),
                        source["name"],
                        "utf-8" if source["encoding"] == "utf-8-sig" else source["encoding"],
                    )
                finally:
                    # A decoding/IO failure can leave only part of the new copy;
                    # register those files too so the import transaction rolls them back.
                    if folder.exists():
                        written.extend(folder.iterdir())
                if metadata["characters"] != source["characters"]:
                    raise ValueError("原著编码或字符数与数据包记录不符")
            db.execute(
                "INSERT INTO novel_sources VALUES (?,?,?,?,?,?)",
                (
                    identity,
                    source["name"],
                    source["encoding"],
                    source["bytes"],
                    source["characters"],
                    source["created"],
                ),
            )
        db.execute("INSERT INTO novel_source_history VALUES (?,?)", (sid, identity))
    db.execute(
        "INSERT INTO novel_bindings(session,source,enabled,next_ref) VALUES (?,?,?,?)",
        (sid, source_ids[raw["source"]] if raw["source"] else None, int(raw["enabled"]), raw["next_ref"]),
    )
    for original in raw["jobs"]:
        job = copy.deepcopy(original)
        identity, progress = uid(), job["progress"]
        progress.pop("routing", None)
        context = progress.get("context", {})
        if context.get("checkpoint"):
            progress.pop("context", None)  # Portable originals rebuild opaque state for the new owner.
        for message in job["trace"]:
            for key in ("_native_response", "_response_origin", "_response_digest"):
                message.pop(key, None)
        if progress.get("pending"):
            for key in ("_native_response", "_response_origin", "_response_digest"):
                progress["pending"].pop(key, None)
        db.execute(
            "INSERT INTO novel_jobs(id,session,ref,source,operation,round_position,origin_turn,task,background,"
            "status,config,progress,requests,elapsed,usage,error,created) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                identity,
                sid,
                job["ref"],
                source_ids[job["source"]],
                "import:" + identity,
                job["round_position"],
                "",
                job["task"],
                job["background"],
                "destroyed" if job["status"] == "destroyed" else "paused",
                dump(job["config"]),
                dump(progress),
                job["requests"],
                job["elapsed"],
                dump(job["usage"]),
                "导入后暂停，已用额度保留",
                job["created"],
            ),
        )
        db.executemany(
            "INSERT INTO novel_trace(job,message,created) VALUES (?,?,?)",
            [(identity, dump(message), time.time()) for message in job["trace"]],
        )
        db.executemany(
            "INSERT INTO novel_mail(job,sender,kind,content,operation,delivered,revoked,created) VALUES (?,?,?,?,?,?,?,?)",
            [
                (
                    identity,
                    m["sender"],
                    m["kind"],
                    m["content"],
                    m["operation"],
                    "imported" if m["delivered"] else None,
                    m["revoked"],
                    m["created"],
                )
                for m in job["mail"]
            ],
        )
