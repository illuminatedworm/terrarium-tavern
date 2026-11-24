"""Native Seedream image requests and Doubao Seed TTS HTTP chunked responses."""

import base64
import io
import json
import uuid

from tavern.media.config import canonical_voice
from tavern.media.transport import pcm_wav, post_json, post_media, single_image, speech_instructions
from tavern.scripting.http_errors import upstream_message


class SeedreamImages:
    async def generate(self, request, *, key, proxy, references):
        connection = request["connection"]
        parameters = {
            "model": connection["model"],
            "prompt": request["prompt"],
            "size": request["size"],
            "response_format": "b64_json",
            "watermark": connection["watermark"],
        }
        fmt = connection["output_format"]
        if fmt != "auto":
            parameters["output_format"] = fmt
        if references:
            parameters["image"] = [
                f"data:{mime};base64," + base64.b64encode(data).decode("ascii") for data, mime in references
            ]
        body = await post_json(
            connection,
            "/images/generations",
            json=parameters,
            headers={"Authorization": "Bearer " + key},
            proxy=proxy,
        )
        return single_image(body, None if fmt == "auto" else f"image/{fmt}")


class SeedSpeech:
    async def generate(self, request, *, key, proxy, references):
        connection = request["connection"]
        params = {
            "text": request["text"],
            "speaker": canonical_voice("seed_tts", request["voice"]),
            "audio_params": {
                "format": "pcm",
                "sample_rate": connection["sample_rate"],
                "speech_rate": connection["speech_rate"],
                "loudness_rate": connection["loudness_rate"],
            },
        }
        instructions = speech_instructions(request)
        if instructions:
            params["additions"] = json.dumps({"context_texts": [instructions]}, ensure_ascii=False)
        body = await post_media(
            connection,
            "/tts/unidirectional",
            json={"req_params": params},
            proxy=proxy,
            headers={
                "X-Api-Key": key,
                "X-Api-Resource-Id": connection["model"],
                "X-Api-Request-Id": str(uuid.uuid4()),
            },
        )
        pcm = bytearray()
        for line in io.BytesIO(body):
            if not line.strip():
                continue
            event = json.loads(line)
            code = event.get("code")
            if code == 20000000:
                return pcm_wav(pcm, connection["sample_rate"])
            if code != 0:
                raise ValueError(upstream_message("豆包语音合成失败", event))
            if event.get("data"):
                pcm.extend(base64.b64decode(event["data"], validate=True))
        raise ValueError("豆包语音在完成标记前断开，未保存不完整音频")
