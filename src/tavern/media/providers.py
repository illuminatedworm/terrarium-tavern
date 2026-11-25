"""Provider adapters produce bytes; tools, story state and UI never depend on an SDK."""

import base64
import json
from typing import Protocol
from urllib.parse import urlencode, urlsplit

from websockets.asyncio.client import connect

from tavern.media.gemini import GeminiImages, GeminiSpeech
from tavern.media.images import read_image
from tavern.media.storage import check_media_key
from tavern.media.transport import (
    MAX_RESPONSE_BYTES,
    MediaResult,
    api_root,
    image_from_url,
    image_result,
    pcm_wav,
    post_json,
    post_media,
    single_image,
    speech_instructions,
    wav_result,
)
from tavern.media.volcengine import SeedreamImages, SeedSpeech
from tavern.scripting.http_errors import upstream_message


class MediaAdapter(Protocol):
    async def generate(
        self, request: dict, *, key: str, proxy: str | None, references: list
    ) -> MediaResult: ...


class OpenAIImages:
    async def generate(self, request, *, key, proxy, references):
        connection = request["connection"]
        # Jobs created before output format was configurable always requested PNG.
        fmt = connection.get("output_format", "png")
        parameters = {
            "model": connection["model"],
            "prompt": request["prompt"],
            "n": 1,
            "quality": connection["quality"],
            "size": request["size"],
            "output_format": fmt,
        }
        if fmt == "auto":
            parameters = {
                k: v
                for k, v in parameters.items()
                if k not in ("quality", "size", "output_format") or v != "auto"
            }
        if references:
            path = "/images/edits"
            payload = {
                "data": {k: str(v) for k, v in parameters.items()},
                "files": [
                    ("image[]", (f"reference-{i}." + mime.split("/")[1], data, mime))
                    for i, (data, mime) in enumerate(references, 1)
                ],
            }
        else:
            path, payload = "/images/generations", {"json": parameters}
        headers = {"Authorization": "Bearer " + key} if key else {}
        body = await post_json(
            connection,
            path,
            headers=headers,
            proxy=proxy,
            **payload,
        )
        mime = None if fmt == "auto" else f"image/{fmt}"
        outputs = body.get("data", [])
        if (
            len(outputs) == 1
            and not outputs[0].get("error")
            and not outputs[0].get("b64_json")
            and outputs[0].get("url")
        ):
            return await image_from_url(outputs[0]["url"], connection, proxy, mime, headers=headers)
        return single_image(body, mime)


class ChatCompletionsImages:
    async def generate(self, request, *, key, proxy, references):
        connection = request["connection"]
        prompt = request["prompt"]
        content: str | list[dict] = prompt
        if references:
            content = [{"type": "text", "text": prompt}]
            for data, mime in references:
                url = f"data:{mime};base64," + base64.b64encode(data).decode("ascii")
                content.append({"type": "image_url", "image_url": {"url": url}})
        headers = {"Authorization": "Bearer " + key} if key else {}
        body = await post_json(
            connection,
            "/chat/completions",
            headers=headers,
            proxy=proxy,
            json={
                "model": connection["model"],
                "messages": [{"role": "user", "content": content}],
                "modalities": ["text", "image"],
                "stream": False,
            },
        )
        match body.get("choices"):
            case [{"finish_reason": "stop", "message": {"images": [{"image_url": {"url": str() as url}}]}}]:
                if url.startswith(("https://", "http://")):
                    return await image_from_url(url, connection, proxy, headers=headers)
                media_type, separator, encoded = url.partition(";base64,")
                if separator and media_type.startswith("data:image/") and encoded:
                    return image_result(encoded, media_type.removeprefix("data:"))
                raise ValueError("Chat Completions 返回的图片地址或编码无效")
            case [{"finish_reason": "stop", "message": dict() as message}]:
                if (
                    not message.get("images")
                    and isinstance(message.get("content"), str)
                    and message["content"]
                ):
                    raise ValueError("Chat Completions 只返回了文字，请在提示词中明确要求生成图片")
                raise ValueError("Chat Completions 未返回单张完整图片")
            case _:
                raise ValueError(upstream_message("Chat Completions 未完成图片生成", body))


def openai_voice(request: dict) -> str | dict[str, str]:
    voice = request["voice"]
    conn = request["connection"]
    # The default ordinary API-key connection also points directly at OpenAI.
    if voice.startswith("voice_") and (
        conn["auth_mode"] == "openai_api_key" or urlsplit(conn["base_url"]).hostname == "api.openai.com"
    ):
        return {"id": voice}
    return voice


class OpenAIHTTPSpeech:
    async def generate(self, request, *, key, proxy, references):
        conn = request["connection"]
        body = {
            "model": conn["model"],
            "input": request["text"],
            "voice": openai_voice(request),
            "response_format": "wav",
        }
        if conn["speed"] != 1:
            body["speed"] = conn["speed"]
        if conn["send_instructions"] and (instructions := speech_instructions(request)):
            body["instructions"] = instructions
        raw = await post_media(
            conn,
            "/audio/speech",
            headers={"Authorization": "Bearer " + key} if key else {},
            proxy=proxy,
            json=body,
        )
        return wav_result(bytes(raw))


class OpenAIRealtime:
    async def generate(self, request, *, key, proxy, references):
        connection = request["connection"]
        root = api_root(connection).replace("https://", "wss://", 1).replace("http://", "ws://", 1)
        url = root + "/realtime?" + urlencode({"model": connection["model"]})
        instructions = speech_instructions(request)
        pcm = bytearray()
        # A fresh session per clip permits a different fixed character voice. No
        # reconnect iterator: uncertain paid work must never be replayed automatically.
        async with connect(
            url,
            additional_headers={"Authorization": "Bearer " + key},
            proxy=proxy,
            open_timeout=30,
            max_size=MAX_RESPONSE_BYTES,
        ) as socket:
            await socket.send(
                json.dumps(
                    {
                        "type": "session.update",
                        "session": {
                            "type": "realtime",
                            "output_modalities": ["audio"],
                            "instructions": instructions,
                            "truncation": "disabled",
                            "audio": {
                                "output": {
                                    "format": {"type": "audio/pcm", "rate": 24000},
                                    "voice": openai_voice(request),
                                }
                            },
                        },
                    }
                )
            )
            async for raw in socket:
                event = json.loads(raw)
                kind = event["type"]
                if kind == "session.updated":
                    await socket.send(
                        json.dumps(
                            {
                                "type": "conversation.item.create",
                                "item": {
                                    "type": "message",
                                    "role": "user",
                                    "content": [{"type": "input_text", "text": request["text"]}],
                                },
                            },
                            ensure_ascii=False,
                        )
                    )
                    await socket.send(json.dumps({"type": "response.create"}))
                elif kind == "response.output_audio.delta":
                    pcm.extend(base64.b64decode(event["delta"], validate=True))
                    if len(pcm) > MAX_RESPONSE_BYTES:
                        raise ValueError("语音响应超过 64 MiB，未保存不完整产物")
                elif kind == "error":
                    raise ValueError("语音服务：" + event["error"]["message"])
                elif kind == "response.done":
                    response = event["response"]
                    if response["status"] != "completed":
                        raise ValueError(
                            "语音生成未完成："
                            + json.dumps(response.get("status_details"), ensure_ascii=False)
                        )
                    break
            else:
                raise ValueError("语音连接在完成之前关闭；结果不明，请按需手动重试")
        return pcm_wav(pcm, 24000)


ADAPTERS: dict[str, MediaAdapter] = {
    "openai_speech": OpenAIHTTPSpeech(),
    "openai_realtime": OpenAIRealtime(),
    "openai_images": OpenAIImages(),
    "chat_completions_image": ChatCompletionsImages(),
    "gemini_tts": GeminiSpeech(),
    "gemini_image": GeminiImages(),
    "ark_seedream": SeedreamImages(),
    "seed_tts": SeedSpeech(),
}


def connection(store, request):
    settings = store.settings()
    saved = request["connection"]
    provider = next((p for p in settings.providers if p.id == saved["provider_id"]), None)
    if provider is None or provider.base_url != saved["base_url"] or provider.auth_mode != saved["auth_mode"]:
        # Never send a newly edited credential to the old snapshotted endpoint.
        raise ValueError("原媒体连接已删除或地址/认证方式已改变；请使用当前设置重新生成")
    check_media_key(provider, saved["adapter"])
    proxy = (settings.network.proxy or None) if settings.network.api_proxy and provider.use_proxy else None
    return provider.api_key, proxy


def reference_bytes(store, sid, request):
    with store.connection() as db:
        result = []
        for identity in request.get("references", []):
            row = db.execute(
                "SELECT * FROM media_assets WHERE id=? AND session=? AND kind='image'", (identity, sid)
            ).fetchone()
            if row is None:
                raise ValueError("任务引用的图片已缺失，未发起生成")
            result.append((read_image(store, row), row["mime"]))
        return result
