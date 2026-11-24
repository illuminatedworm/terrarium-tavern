"""Local-only playback, visible configuration, reference selection and task controls."""

import asyncio
import json
from pathlib import Path
from typing import Literal

from fastapi import File, Form, Request, UploadFile
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import Field

from tavern.character.editor_images import image_metadata
from tavern.core.channels import Channel, provider_channel
from tavern.core.errors import NotFound
from tavern.media import storage
from tavern.media.config import (
    GEMINI_CLOUD_VOICE_MODELS,
    GEMINI_METADATA_VOICES,
    GEMINI_PROMPT_MODELS,
    SEED_VOICES,
    SPEECH_VOICES,
    GeminiImageSettings,
    GeminiSpeechSettings,
    HTTPSpeechSettings,
    ImageProfile,
    ImageSettings,
    SeedreamSettings,
    SeedSpeechSettings,
    SpeechProfile,
    SpeechSettings,
    StrictModel,
    available_voices,
    image_sizes,
    validate_voice,
)
from tavern.media.images import MAX_IMAGE_BYTES
from tavern.media.providers import ADAPTERS
from tavern.media.routing import MediaDefaults, StoryMediaSettings, story_key
from tavern.scripting.http_errors import redact_detail


class ConfigEdit(StrictModel):
    expected_revision: int = Field(ge=0)
    expected_selection: str | None = None
    config: dict


class SpeechPreview(StrictModel):
    kind: Literal["speech"]
    channel: Channel
    profile: SpeechProfile
    text: str = Field(min_length=1)
    voice: str = Field(min_length=1)


class ImagePreview(StrictModel):
    kind: Literal["image"]
    channel: Channel
    profile: ImageProfile
    prompt: str = Field(min_length=1)


class MessageQuery(StrictModel):
    message_ids: list[str] = Field(max_length=100)


class MediaAction(StrictModel):
    operation_id: str = Field(min_length=1, max_length=100)


class ReferenceEdit(StrictModel):
    expected_revision: int = Field(ge=0)
    name: str = Field(min_length=1, max_length=120)
    description: str = ""
    selected: bool = True


class CardReference(ReferenceEdit):
    key: str


def card_images(store, sid):
    card = store.session_card(sid)
    images = {
        name: digest
        for name, digest in card["resources"].items()
        if Path(name).suffix.lower() in (".png", ".jpg", ".jpeg", ".webp")
    }
    if card["container"] == "png":
        images["角色卡封面.png"] = card["original"]
    return images


def protocol_catalog(document):
    return {
        "presets": MediaDefaults().model_dump(),
        "keyless_adapters": {
            p.id: storage.LOCAL_HTTP_ADAPTERS
            for p in document.shared.providers
            if p.is_loopback and p.auth_mode == "api_key"
        },
        "defaults": document.story.media.model_dump(),
        "providers": [
            {
                "id": p.id,
                "name": p.name,
                "base_url": p.base_url,
                "auth_mode": p.auth_mode,
                "has_api_key": bool(p.api_key),
                "use_proxy": p.use_proxy,
            }
            for p in document.shared.providers
            if p.auth_mode != "chatgpt_subscription"
        ],
        "speech_voices": SPEECH_VOICES,
        "seed_voice_names": {identity: name for name, identity in SEED_VOICES.items()},
        "gemini_speech": {
            "prompt_prefixes": GEMINI_PROMPT_MODELS,
            "cloud_voice_prefixes": GEMINI_CLOUD_VOICE_MODELS,
            "metadata_voices": GEMINI_METADATA_VOICES,
        },
        "adapters": {
            preset.adapter: preset.model_dump()
            for preset in (
                HTTPSpeechSettings(),
                SpeechSettings(),
                GeminiSpeechSettings(),
                SeedSpeechSettings(),
                ImageSettings(),
                GeminiImageSettings(),
                SeedreamSettings(),
            )
        },
    }


def register_routes(app, service):
    store = service.store

    @app.get("/api/media/protocols")
    def protocols():
        return protocol_catalog(store.settings_document())

    @app.post("/api/media/preview")
    async def preview(edit: SpeechPreview | ImagePreview, request: Request):
        shared = store.settings_document().shared
        conn = storage.connection_snapshot(shared, edit.profile)
        provider = next(p for p in shared.providers if p.id == edit.profile.provider_id)
        if provider_channel(provider) != edit.channel:
            raise ValueError("测试渠道与当前提供商不匹配")
        if isinstance(edit, SpeechPreview):
            validate_voice(edit.profile, edit.voice)
            payload = {"connection": conn, "voice": edit.voice, "text": edit.text, "delivery": ""}
        else:
            payload = {"connection": conn, "prompt": edit.prompt, "size": image_sizes(edit.profile)["auto"]}
        proxy = (shared.network.proxy or None) if shared.network.api_proxy and provider.use_proxy else None
        # Preview has no story, SQLite intent, or automatic retry. The request
        # owns its transport and abandons it when the settings page disconnects.
        generation = asyncio.create_task(
            ADAPTERS[edit.profile.adapter].generate(payload, key=provider.api_key, proxy=proxy, references=[])
        )

        async def disconnected():
            while (await request.receive())["type"] != "http.disconnect":
                pass

        disconnect = asyncio.create_task(disconnected())
        try:
            async with asyncio.timeout(conn["timeout_seconds"]):
                done, _ = await asyncio.wait((generation, disconnect), return_when=asyncio.FIRST_COMPLETED)
                if generation not in done:
                    return Response(status_code=499)
                result = generation.result()
                return Response(result.data, media_type=result.mime, headers={"Cache-Control": "no-store"})
        except TimeoutError:
            raise ValueError("媒体试用超时，服务端可能已计费") from None
        except Exception as error:
            # Expose provider failures without leaking the configured credential.
            raise ValueError(redact_detail(str(error) or type(error).__name__, provider.api_key)) from None
        finally:
            generation.cancel()
            disconnect.cancel()
            await asyncio.gather(generation, disconnect, return_exceptions=True)

    @app.get("/api/sessions/{sid}/media")
    def settings(sid: str, library: bool = True):
        with store.connection() as db:
            current = db.execute(
                "SELECT revision,media_config,head,model_selection,channel_origin FROM sessions WHERE id=?",
                (sid,),
            ).fetchone()
            if current is None:
                raise NotFound("会话不存在")
            document = store.settings_document(db=db)
            parents = (
                dict(db.execute("SELECT id,parent FROM messages WHERE session=?", (sid,))) if library else {}
            )
        active, cursor = set(), current["head"]
        while library and cursor:
            if cursor not in parents or cursor in active:
                raise ValueError("剧情分支无效")
            active.add(cursor)
            cursor = parents[cursor]
        selection = StoryMediaSettings.model_validate_json(current["media_config"])
        model = story_key(current, document)
        config = selection.resolve(document.story.media, model)
        return {
            "config": config.model_dump(),
            "source": selection.source,
            "selection": selection.model_dump(),
            "selection_token": storage.selection_token(current["media_config"]),
            "story_model": model.model_dump(),
            "story_provider_name": next(
                (p.name for p in document.shared.providers if p.id == model.provider_id), ""
            ),
            "revision": current["revision"],
            "voices": available_voices(config.speech),
            "sizes": list(image_sizes(config.image)),
            **protocol_catalog(document),
            "references": [
                storage.asset_view(a)
                for a in storage.image_catalog(store, sid, active, selected=True, unbound=True)
            ]
            if library
            else [],
            "card_images": list(card_images(store, sid)) if library else [],
        }

    @app.put("/api/sessions/{sid}/media")
    async def save(sid: str, edit: ConfigEdit):
        selection = storage.save_config(
            store, sid, edit.expected_revision, edit.config, expected_selection=edit.expected_selection
        )
        disabled = tuple(kind for kind in ("speech", "image") if not getattr(selection, kind).enabled)
        if disabled:
            await service.cancel_running(sid, disabled)
        return store.session(sid)

    @app.post("/api/sessions/{sid}/media/jobs")
    def list_jobs(sid: str, query: MessageQuery):
        return storage.jobs(store, sid, query.message_ids)

    @app.post("/api/sessions/{sid}/media/jobs/{jid}/retry")
    def retry(sid: str, jid: str, edit: MediaAction):
        return storage.retry(store, sid, jid, operation_id=edit.operation_id)

    @app.post("/api/sessions/{sid}/media/jobs/{jid}/regenerate")
    def regenerate(sid: str, jid: str, edit: MediaAction):
        return storage.retry(store, sid, jid, regenerate=True, operation_id=edit.operation_id)

    @app.post("/api/sessions/{sid}/media/jobs/{jid}/cancel")
    async def cancel(sid: str, jid: str):
        await service.cancel(sid, jid)
        return storage.job(store, sid, jid)

    @app.get("/api/sessions/{sid}/media/assets/{aid}")
    def asset(sid: str, aid: str):
        with store.connection() as db:
            row = db.execute("SELECT * FROM media_assets WHERE id=? AND session=?", (aid, sid)).fetchone()
        if row is None:
            raise NotFound("媒体文件不存在")
        path = store.root / "blobs" / row["blob"]
        if not path.is_file():
            raise NotFound("媒体文件缺失，请从完整备份恢复")
        return FileResponse(
            path, media_type=row["mime"], headers={"Cache-Control": "private, max-age=31536000, immutable"}
        )

    @app.post("/api/sessions/{sid}/media/references")
    async def upload(
        sid: str,
        file: UploadFile = File(...),
        expected_revision: int = Form(...),
        name: str = Form(..., min_length=1, max_length=120),
        description: str = Form(""),
    ):
        data = await file.read(MAX_IMAGE_BYTES + 1)
        if len(data) > MAX_IMAGE_BYTES:
            raise ValueError("参考图不能超过 32 MiB")
        metadata = await asyncio.to_thread(image_metadata, Path(file.filename or "").suffix.lower(), data)
        with store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            storage.editable(db, sid, expected_revision)
            storage.add_asset(
                store,
                db,
                sid,
                data=data,
                kind="image",
                mime=metadata["mime_type"],
                name=name,
                description=description,
                selected=True,
            )
            storage.next_sequence(db, sid)
        store.notify_media(sid)
        return store.session(sid)

    @app.post("/api/sessions/{sid}/media/card-reference")
    def select_card_image(sid: str, edit: CardReference):
        images = card_images(store, sid)
        if edit.key not in images:
            raise ValueError("图片不属于此对话的角色卡")
        path = store.root / "blobs" / images[edit.key]
        with path.open("rb") as source:
            data = source.read(MAX_IMAGE_BYTES + 1)
        if len(data) > MAX_IMAGE_BYTES:
            raise ValueError("参考图不能超过 32 MiB")
        metadata = image_metadata(Path(edit.key).suffix.lower(), data)
        with store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            storage.editable(db, sid, edit.expected_revision)
            storage.add_asset(
                store,
                db,
                sid,
                data=data,
                kind="image",
                mime=metadata["mime_type"],
                name=edit.name,
                description=edit.description,
                selected=True,
            )
            storage.next_sequence(db, sid)
        store.notify_media(sid)
        return store.session(sid)

    @app.put("/api/sessions/{sid}/media/references/{aid}")
    def edit_reference(sid: str, aid: str, edit: ReferenceEdit):
        with store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            storage.editable(db, sid, edit.expected_revision)
            changed = db.execute(
                "UPDATE media_assets SET name=?,description=?,selected=? WHERE id=? AND session=? AND kind='image'",
                (edit.name, edit.description, int(edit.selected), aid, sid),
            )
            if changed.rowcount != 1:
                raise NotFound("图片不存在")
            storage.next_sequence(db, sid)
        store.notify_media(sid)
        return store.session(sid)

    @app.get("/api/sessions/{sid}/media/events")
    async def events(sid: str, request: Request):
        storage.config(store, sid)

        async def stream():
            queue = asyncio.Queue(maxsize=64)
            service.listeners[sid].add(queue)
            try:
                with store.connection() as db:
                    seq = db.execute("SELECT media_sequence FROM sessions WHERE id=?", (sid,)).fetchone()[0]
                yield "data: " + json.dumps({"seq": seq, "reset": True}) + "\n\n"
                while not service.closed and not await request.is_disconnected():
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=15)
                    except TimeoutError:
                        yield ": keepalive\n\n"
                        continue
                    if event is None:
                        break
                    yield f"id: {event['seq']}\ndata: " + json.dumps(event, ensure_ascii=False) + "\n\n"
            finally:
                service.listeners[sid].discard(queue)
                if not service.listeners[sid]:
                    service.listeners.pop(sid, None)

        return StreamingResponse(
            stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"}
        )
