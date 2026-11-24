"""Bounded wire responses and playable media shared by generation adapters."""

import base64
import io
import json
import wave
from dataclasses import dataclass

import httpx

from tavern.character.editor_images import image_metadata
from tavern.media.images import MAX_IMAGE_BYTES
from tavern.scripting.http_errors import upstream_message

MAX_RESPONSE_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True)
class MediaResult:
    data: bytes
    mime: str


def api_root(connection):
    return "/".join(filter(None, (connection["base_url"].rstrip("/"), connection["api_path"])))


async def post_media(connection, path, *, headers, proxy, **payload) -> bytearray:
    async with httpx.AsyncClient(
        proxy=proxy, trust_env=False, timeout=connection["timeout_seconds"]
    ) as client:
        async with client.stream("POST", api_root(connection) + path, headers=headers, **payload) as response:
            data = bytearray()
            async for chunk in response.aiter_bytes():
                if len(data) + len(chunk) > MAX_RESPONSE_BYTES:
                    raise ValueError("媒体服务响应超过 64 MiB，未保存不完整产物")
                data.extend(chunk)
            if not response.is_success:
                raise ValueError(
                    upstream_message(f"媒体服务 HTTP {response.status_code}", data.decode(errors="replace"))
                )
    return data


async def post_json(connection, path, **kwargs):
    data = await post_media(connection, path, **kwargs)
    try:
        body = json.loads(data)
    except ValueError:
        raise ValueError("媒体服务未返回有效 JSON，请检查连接、API 路径和协议") from None
    if not isinstance(body, dict):
        raise ValueError("媒体服务返回的 JSON 格式无效")
    if body.get("error"):
        raise ValueError(upstream_message("媒体生成失败", body))
    return body


def image_result(encoded: str, mime: str | None = None) -> MediaResult:
    if len(encoded) > 4 * ((MAX_IMAGE_BYTES + 2) // 3):
        raise ValueError("生成图片超过 32 MiB")
    data = base64.b64decode(encoded, validate=True)
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError("生成图片超过 32 MiB")
    actual = image_metadata(None, data)["mime_type"]
    if mime and mime != actual:
        raise ValueError("生图服务返回的图片格式与声明不一致")
    return MediaResult(data, actual)


def single_image(body, mime=None) -> MediaResult:
    outputs = body.get("data", [])
    if len(outputs) == 1 and outputs[0].get("error"):
        raise ValueError(upstream_message("生图失败", outputs[0]))
    if len(outputs) != 1 or not outputs[0].get("b64_json"):
        raise ValueError("生图服务未返回一张完整的 base64 图片")
    return image_result(outputs[0]["b64_json"], mime)


async def image_from_url(url, connection, proxy, mime=None, *, headers):
    from urllib.parse import urljoin, urlsplit

    from tavern.web.client import public_url
    from tavern.web.transport import PublicHTTPTransport

    origin = httpx.URL(connection["base_url"])
    target = urljoin(str(origin), url)
    for _ in range(5):
        parsed = urlsplit(target)
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            raise ValueError("生图服务返回的图片地址无效")
        remote = httpx.URL(target)
        same = (origin.scheme, origin.host, origin.port) == (remote.scheme, remote.host, remote.port)
        # A relay can protect its own result URLs. Check each redirect against
        # the configured origin before attaching the provider's headers.
        transport = None
        if not same:
            await public_url(target)
            if not proxy:
                transport = PublicHTTPTransport()
        async with httpx.AsyncClient(
            proxy=proxy, transport=transport, trust_env=False, timeout=connection["timeout_seconds"]
        ) as client:
            async with client.stream("GET", remote, headers=headers if same else {}) as response:
                if response.is_redirect and response.headers.get("location"):
                    target = urljoin(target, response.headers["location"])
                    continue
                if not response.is_success:
                    raise ValueError(f"生成图片下载失败（HTTP {response.status_code}）")
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(data) + len(chunk) > MAX_IMAGE_BYTES:
                        raise ValueError("生成图片超过 32 MiB")
                    data.extend(chunk)
        data = bytes(data)
        actual = image_metadata(None, data)["mime_type"]
        if mime and mime != actual:
            raise ValueError("生成图片格式与请求不符")
        return MediaResult(data, actual)
    raise ValueError("生成图片下载重定向过多")


def wav_result(data: bytes) -> MediaResult:
    try:
        with wave.open(io.BytesIO(data)) as audio:
            frames = audio.getnframes()
            width = audio.getsampwidth() * audio.getnchannels()
            if not frames or audio.getcomptype() != "NONE":
                raise ValueError("配音服务返回了空音频或不支持的 WAV 编码")
            audio.setpos(frames - 1)
            if len(audio.readframes(1)) == width:
                return MediaResult(data, "audio/wav")
            # Some streaming encoders leave RIFF and data lengths open-ended.
            # Finalize only that explicit format after the HTTP body completes.
            if data[4:8] == b"\xff" * 4 and b"data\xff\xff\xff\xff" in data:
                audio.rewind()
                pcm = audio.readframes(frames)
                if pcm and len(pcm) % width == 0:
                    output = io.BytesIO()
                    with wave.open(output, "wb") as result:
                        result.setparams(audio.getparams())
                        result.setnframes(len(pcm) // width)
                        result.writeframes(pcm)
                    return MediaResult(output.getvalue(), "audio/wav")
    except (EOFError, OSError, wave.Error):
        pass
    raise ValueError("配音服务未返回完整的 PCM WAV 音频")


def pcm_wav(pcm: bytes | bytearray, rate: int) -> MediaResult:
    if not pcm or len(pcm) % 2 or len(pcm) + 44 > MAX_RESPONSE_BYTES:
        raise ValueError("语音服务未返回完整的 16 位音频，或 WAV 超过 64 MiB")
    output = io.BytesIO()
    with wave.open(output, "wb") as recording:
        recording.setnchannels(1)
        recording.setsampwidth(2)
        recording.setframerate(rate)
        recording.writeframes(pcm)
    return MediaResult(output.getvalue(), "audio/wav")


def speech_instructions(request) -> str:
    return "\n\n".join(filter(None, (request["connection"]["instructions"], request["delivery"])))
