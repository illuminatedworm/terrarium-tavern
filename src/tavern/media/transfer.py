"""Media travels with personal data, never with credentials or automatic paid replays."""

import copy
import io
import json
import wave
from typing import Literal

from pydantic import Field

from tavern.character.editor_images import image_metadata
from tavern.media.config import (
    MediaConfig,
    StrictModel,
    image_reference_limit,
    image_sizes,
    snapshot_options,
    validate_voice,
)
from tavern.media.routing import ModelKey, StoryMediaSettings


class AssetRecord(StrictModel):
    id: str
    session: str
    message: str | None
    ref: int = Field(gt=0)
    kind: Literal["image", "speech"]
    name: str
    description: str
    mime: Literal["image/png", "image/jpeg", "image/webp", "audio/wav"]
    blob: str = Field(pattern=r"^[a-f0-9]{64}$")
    size: int = Field(ge=0, le=64 * 1024 * 1024)
    selected: Literal[0, 1]
    created: float = Field(ge=0, lt=1e12)


class JobRecord(StrictModel):
    id: str
    session: str
    message: str
    kind: Literal["image", "speech"]
    status: Literal["queued", "running", "completed", "failed", "interrupted", "cancelled"]
    request: dict
    asset: str | None
    previous: str | None
    error: str
    created: float = Field(ge=0, lt=1e12)
    updated: float = Field(ge=0, lt=1e12)
    seq: int = Field(ge=0)
    ordinal: int = Field(gt=0)


def export_bundle(db, sid, blobs):
    assets = [dict(row) for row in db.execute("SELECT * FROM media_assets WHERE session=?", (sid,))]
    jobs = [dict(row) for row in db.execute("SELECT * FROM media_jobs WHERE session=?", (sid,))]
    for job in jobs:
        job["request"] = json.loads(job["request"])
    blobs.update(a["blob"] for a in assets)
    return {"assets": assets, "jobs": jobs}


def validate_bundle(bundle, messages, blobs, identity):
    from tavern.media.config import ImageArguments, SpeechArguments

    session = bundle["session"]
    sequence = session.get("media_sequence", 0)
    if type(sequence) is not int or sequence < 0:
        raise ValueError("媒体事件序号无效")
    config = session.get("media_config", {})
    # Pre-media packages omitted the column. A v23 re-export of one of these
    # imports can contain the old table's unconfigured flat SQLite default.
    if "media_config" not in session or (
        "voices" in config and MediaConfig.model_validate(config) == MediaConfig()
    ):
        session["media_config"] = StoryMediaSettings().model_dump()
    else:
        StoryMediaSettings.model_validate(config)
    media = bundle.get("media", {"assets": [], "jobs": []})
    if set(media) != {"assets", "jobs"}:
        raise ValueError("媒体数据包格式无效")
    assets, refs, checked = {}, set(), set()
    for raw in media["assets"]:
        item = AssetRecord.model_validate(raw)
        identity(item.id)
        if (
            item.session != session["id"]
            or item.ref in refs
            or (item.message and item.message not in messages)
        ):
            raise ValueError("媒体资源归属或图片编号无效")
        refs.add(item.ref)
        assets[item.id] = item
        if item.blob not in blobs or len(blobs[item.blob]) != item.size:
            raise ValueError("数据包缺少完整媒体文件")
        if (
            item.kind == "image"
            and not item.mime.startswith("image/")
            or item.kind == "speech"
            and item.mime != "audio/wav"
        ):
            raise ValueError("媒体文件类型不一致")
        if item.blob not in checked:
            if item.kind == "image":
                if item.size > 32 * 1024 * 1024:
                    raise ValueError("图片超过 32 MiB")
                image_metadata(
                    {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}[item.mime],
                    blobs[item.blob],
                )
            else:
                try:
                    with wave.open(io.BytesIO(blobs[item.blob]), "rb") as audio:
                        if audio.getcomptype() != "NONE":
                            raise ValueError("不支持此 WAV 编码")
                except (wave.Error, EOFError):
                    raise ValueError("语音文件损坏") from None
            checked.add(item.blob)
    jobs, positions = {}, set()
    for raw in media["jobs"]:
        item = JobRecord.model_validate(raw)
        if item.seq > sequence:
            raise ValueError("媒体任务序号超过所属故事的事件序号")
        identity(item.id)
        position = (item.message, item.ordinal)
        if position in positions:
            raise ValueError("媒体任务顺序重复")
        positions.add(position)
        jobs[item.id] = item
        if (
            item.session != session["id"]
            or item.message not in messages
            or messages[item.message]["role"] != "assistant"
        ):
            raise ValueError("媒体任务不属于对应回复")
        if item.asset and (
            item.asset not in assets
            or assets[item.asset].message != item.message
            or assets[item.asset].kind != item.kind
        ):
            raise ValueError("媒体任务产物归属无效")
        if item.status == "completed" and not item.asset:
            raise ValueError("已完成媒体任务缺少产物")
        request = item.request
        connection = request["connection"]
        # Validate only the connection and generation boundary, never compare TTS text with prose.
        from tavern.search.retrieval_config import Provider

        options = snapshot_options(item.kind, connection)
        Provider(
            id=connection["provider_id"], base_url=connection["base_url"], auth_mode=connection["auth_mode"]
        )
        if connection["auth_mode"] == "chatgpt_subscription":
            raise ValueError("媒体任务不能使用登录订阅")
        if item.kind == "speech":
            SpeechArguments.model_validate(
                {k: v for k, v in request.items() if k not in ("connection", "voice", "story_model")}
            )
            validate_voice(options, request["voice"], stored=True)
            if "story_model" in request:
                ModelKey.model_validate(request["story_model"])
        else:
            ImageArguments.model_validate(
                {k: v for k, v in request.items() if k not in ("connection", "reference_names")}
            )
            if request.get("size", "auto") not in image_sizes(options).values() or len(
                request["references"]
            ) > image_reference_limit(options, frozen=True):
                raise ValueError("媒体任务的尺寸或参考图数量与生图协议不符")
            if any(ref not in assets or assets[ref].kind != "image" for ref in request["references"]):
                raise ValueError("媒体任务引用的图片不存在")
    for item in jobs.values():
        if item.previous:
            previous = jobs.get(item.previous)
            if (
                previous is None
                or previous.message != item.message
                or previous.kind != item.kind
                or previous.ordinal >= item.ordinal
            ):
                raise ValueError("媒体重做来源或顺序无效")
    image_refs = {"I" + str(a.ref) for a in assets.values() if a.kind == "image"}
    for trace in bundle.get("story_trace", []):
        for ref in [
            *trace["payload"].get("_media_image_refs", []),
            *trace["payload"].get("_media_catalog_refs", []),
        ]:
            if ref not in image_refs:
                raise ValueError("看图历史引用了未随数据包保存的图片")


def import_bundle(db, sid, media, ids):
    from tavern.storage.store import dump, insert_row

    for raw in media.get("assets", []):
        item = copy.deepcopy(raw)
        item.update(id=ids[item["id"]], session=sid, message=ids.get(item["message"]))
        insert_row(db, "media_assets", item)
    for raw in media.get("jobs", []):
        item = copy.deepcopy(raw)
        request = item["request"]
        if "references" in request:
            request["references"] = [ids[ref] for ref in request["references"]]
        item.update(
            id=ids[item["id"]],
            session=sid,
            message=ids[item["message"]],
            asset=ids.get(item["asset"]),
            previous=ids.get(item["previous"]),
            request=dump(request),
        )
        if item["status"] in ("queued", "running"):
            item.update(status="interrupted", error="导入数据不会自动发起付费生成；可手动重试")
        insert_row(db, "media_jobs", item)
