"""Gemini generateContent with native inline audio and reference images."""

import base64
import io
import wave
from email.message import Message
from urllib.parse import quote

from tavern.media.transport import MediaResult, image_result, pcm_wav, post_json, speech_instructions
from tavern.scripting.http_errors import upstream_message


async def generate_content(connection, parts, config, *, key, proxy):
    model = connection["model"].removeprefix("models/")
    if not model or "/" in model or model in (".", ".."):
        raise ValueError("Gemini 媒体模型须填写模型名称")
    body = await post_json(
        connection,
        "/models/" + quote(model, safe="") + ":generateContent",
        headers={"x-goog-api-key": key} if key else {},
        proxy=proxy,
        json={"contents": [{"role": "user", "parts": parts}], "generationConfig": config},
    )
    candidates = body.get("candidates", [])
    if len(candidates) != 1 or candidates[0].get("finishReason") != "STOP":
        raise ValueError(upstream_message("Gemini 未完成媒体生成", body))
    # Thought images are intermediate work. Publish only the final output.
    blobs = [
        part["inlineData"]
        for part in candidates[0].get("content", {}).get("parts", [])
        if not part.get("thought") and part.get("inlineData")
    ]
    if len(blobs) != 1 or not blobs[0].get("data"):
        raise ValueError("Gemini 未返回单个完整媒体产物")
    return blobs[0]


class GeminiSpeech:
    async def generate(self, request, *, key, proxy, references):
        connection = request["connection"]
        speech = {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": request["voice"]}}}
        if connection["language_code"]:
            speech["languageCode"] = connection["language_code"]
        config = {"responseModalities": ["AUDIO"], "speechConfig": speech}
        if connection["temperature"] is not None:
            config["temperature"] = connection["temperature"]
        instructions = speech_instructions(request)
        part = {"text": request["text"]}
        if instructions:
            if connection["instruction_format"] == "prompt":
                part["text"] = instructions + "\n\n" + request["text"]
            else:
                # Gemini 3.8 speaks the text part verbatim, including any stage
                # directions placed there. Style belongs on that same part's metadata.
                part["speechMetadata"] = {"style": instructions}
        audio = await generate_content(connection, [part], config, key=key, proxy=proxy)
        content_type = Message()
        content_type["Content-Type"] = audio.get("mimeType", "")
        data = base64.b64decode(audio["data"], validate=True)
        if content_type.get_content_type() in ("audio/wav", "audio/x-wav"):
            with wave.open(io.BytesIO(data)) as wav:
                if wav.getcomptype() != "NONE" or not wav.getnframes():
                    raise ValueError("Gemini 未返回可播放的 PCM WAV 音频")
                wav.setpos(wav.getnframes() - 1)
                if len(wav.readframes(1)) != wav.getsampwidth() * wav.getnchannels():
                    raise ValueError("Gemini 返回的 WAV 音频不完整")
            return MediaResult(data, "audio/wav")
        if content_type.get_content_type() != "audio/l16" or content_type.get_param("channels", "1") != "1":
            raise ValueError("Gemini 未返回单声道 16 位 PCM 音频")
        rate = int(content_type.get_param("rate", "24000"))
        if not 8000 <= rate <= 192000:
            raise ValueError("Gemini 返回的音频采样率无效")
        return pcm_wav(data, rate)


class GeminiImages:
    async def generate(self, request, *, key, proxy, references):
        connection = request["connection"]
        parts = [
            {"inlineData": {"mimeType": mime, "data": base64.b64encode(data).decode("ascii")}}
            for data, mime in references
        ]
        parts.append({"text": request["prompt"]})
        image = {}
        if request["size"] != "auto":
            image["aspectRatio"] = request["size"]
        if connection["resolution"] != "auto":
            image["imageSize"] = connection["resolution"]
        config = {"responseModalities": ["IMAGE"]}
        if image:
            config["imageConfig"] = image
        blob = await generate_content(connection, parts, config, key=key, proxy=proxy)
        return image_result(blob["data"], blob["mimeType"])
