"""Provider adapters produce bytes; tools, story state and UI never depend on an SDK."""

import base64
import io
import json
import wave
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlencode

import httpx
from websockets.asyncio.client import connect

from tavern.character.editor_images import image_metadata
from tavern.media.images import read_image

MAX_RESPONSE_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True)
class MediaResult:
    data: bytes
    mime: str


class MediaAdapter(Protocol):
    async def generate(
        self, request: dict, *, key: str, proxy: str | None, references: list
    ) -> MediaResult: ...


class OpenAIImages:
    async def generate(self, request, *, key, proxy, references):
        connection = request["connection"]
        root = connection["base_url"].rstrip("/")
        if connection["api_path"]:
            root += "/" + connection["api_path"]
        parameters = {
            "model": connection["model"],
            "prompt": request["prompt"],
            "n": 1,
            "quality": connection["quality"],
            "size": request["size"],
            "output_format": "png",
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
        async with httpx.AsyncClient(
            proxy=proxy, trust_env=False, timeout=connection["timeout_seconds"]
        ) as client:
            async with client.stream(
                "POST", root + path, headers={"Authorization": "Bearer " + key}, **payload
            ) as response:
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > MAX_RESPONSE_BYTES:
                        raise ValueError("生图服务响应超过 64 MiB，未保存不完整产物")
                body = json.loads(data)
                if response.is_error:
                    error = body.get("error", {})
                    raise ValueError(
                        f"生图服务 HTTP {response.status_code}：{error.get('message', response.reason_phrase)}"
                    )
        outputs = body.get("data", [])
        if len(outputs) != 1 or not outputs[0].get("b64_json"):
            raise ValueError("生图服务未返回一张完整的 base64 图片")
        data = base64.b64decode(outputs[0]["b64_json"], validate=True)
        if len(data) > 32 * 1024 * 1024:
            raise ValueError("生成图片超过 32 MiB，未保存不完整产物")
        image_metadata(".png", data)
        return MediaResult(data, "image/png")


class OpenAIRealtime:
    async def generate(self, request, *, key, proxy, references):
        connection = request["connection"]
        root = connection["base_url"].replace("https://", "wss://", 1).replace("http://", "ws://", 1)
        root = root.rstrip("/") + ("/" + connection["api_path"] if connection["api_path"] else "")
        url = root + "/realtime?" + urlencode({"model": connection["model"]})
        instructions = "\n\n".join(filter(None, (connection["instructions"], request["delivery"])))
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
                                    "voice": request["voice"],
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
        if not pcm:
            raise ValueError("语音服务没有返回音频")
        output = io.BytesIO()
        with wave.open(output, "wb") as recording:
            recording.setnchannels(1)
            recording.setsampwidth(2)
            recording.setframerate(24000)
            recording.writeframes(pcm)
        return MediaResult(output.getvalue(), "audio/wav")


ADAPTERS: dict[str, MediaAdapter] = {"openai_realtime": OpenAIRealtime(), "openai_images": OpenAIImages()}


def connection(store, request):
    settings = store.settings()
    saved = request["connection"]
    provider = next((p for p in settings.providers if p.id == saved["provider_id"]), None)
    if provider is None or provider.base_url != saved["base_url"] or provider.auth_mode != saved["auth_mode"]:
        # Never send a newly edited credential to the old snapshotted endpoint.
        raise ValueError("原媒体连接已删除或地址/认证方式已改变；请使用当前设置重新生成")
    if not provider.api_key or provider.auth_mode == "chatgpt_subscription":
        raise ValueError("媒体连接需要有效的 API Key")
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
