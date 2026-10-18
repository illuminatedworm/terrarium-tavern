"""Validated local images; durable editor messages carry references, never base64."""
import base64
import copy
import warnings
from io import BytesIO
from typing import TypedDict

from PIL import Image, UnidentifiedImageError

from tavern.character.editor_files import MAX_FILE_BYTES

IMAGE_FORMATS = {".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG", ".webp": "WEBP"}
MIME_TYPES = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}
IMAGE_REFS = "_editor_image_refs"


class ImageMetadata(TypedDict):
    mime_type: str
    width: int
    height: int


def image_metadata(extension: str, data: bytes) -> ImageMetadata:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(data)) as image:
                if image.format != IMAGE_FORMATS.get(extension):
                    raise ValueError("图片实际格式与扩展名不符，请选择 PNG、JPEG 或静态 WebP。")
                if getattr(image, "n_frames", 1) != 1:
                    raise ValueError("暂不支持动图，请选择静态 PNG、JPEG 或 WebP 图片。")
                metadata = {"mime_type": MIME_TYPES[image.format], "width": image.width, "height": image.height}
                image.verify()
            # verify() alone does not decode JPEG pixels or detect all truncation.
            with Image.open(BytesIO(data)) as image:
                image.load()
            return metadata
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise ValueError("图片像素过多，超过安全解码上限，请先缩小图片。") from None
    except (UnidentifiedImageError, OSError, SyntaxError):
        raise ValueError("图片损坏或无法完整解码，请重新选择有效图片。") from None


def image_refs(files: list[dict]) -> list[str]:
    return [item["ref"] for item in files if item["extension"] in IMAGE_FORMATS]


def image_stats(messages: list[dict]) -> dict:
    count = sum(len(message.get(IMAGE_REFS, [])) for message in messages)
    return {"image_count": count, "image_tokens_included": False, "estimated_tokens_complete": count == 0}


def request_preview(value):
    """Request diagnostics keep image positions/types without duplicating blobs."""
    if isinstance(value, list):
        return [request_preview(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {key: request_preview(item) for key, item in value.items()}
    kind = value.get("type")
    omitted = "[图片二进制已省略，实际请求发送原图]"
    if kind == "image_url" and isinstance(value.get("image_url"), dict):
        result["image_url"]["url"] = omitted
    elif kind == "input_image":
        result["image_url"] = omitted
    elif kind == "image" and isinstance(value.get("source"), dict) and value["source"].get("type") == "base64":
        result["source"]["data"] = omitted
    if isinstance(value.get("inlineData"), dict):
        result["inlineData"]["data"] = omitted
    return result


def materialize_images(messages: list[dict], store, wid: str, files: list[dict]) -> list[dict]:
    """Hydrate a request copy only, scoped to the run's already-sent attachments."""
    from tavern.character.editor_attachments import attachment_path

    attached = {item["ref"]: item for item in files if item.get("message_ref")}
    loaded = {}
    result = []
    for message in messages:
        item = copy.deepcopy(message)
        refs = item.pop(IMAGE_REFS, [])
        if refs:
            if item["role"] != "user" or not isinstance(item.get("content"), str):
                raise ValueError("图片附件只能属于用户文本消息。")
            content = [{"type": "text", "text": item["content"]}]
            for ref in refs:
                file = attached.get(ref)
                if file is None or file["extension"] not in IMAGE_FORMATS:
                    raise ValueError(f"图片附件 {ref} 不存在或不属于当前已发送消息，请重新选择。")
                if ref not in loaded:
                    try:
                        with attachment_path(store, wid, file).open("rb") as stream:
                            data = stream.read(MAX_FILE_BYTES + 1)
                    except OSError:
                        raise ValueError(f"图片附件 {ref} 无法读取，请恢复附件文件后重试。") from None
                    if len(data) > MAX_FILE_BYTES or len(data) != file["size_bytes"]:
                        raise ValueError(f"图片附件 {ref} 大小已改变或超过 32 MiB，请重新选择。")
                    metadata = image_metadata(file["extension"], data)
                    if any(file.get(key) != value for key, value in metadata.items()):
                        raise ValueError(f"图片附件 {ref} 格式或尺寸已改变，请重新选择。")
                    loaded[ref] = "data:" + metadata["mime_type"] + ";base64," + base64.b64encode(data).decode("ascii")
                content.append({"type": "image_url", "image_url": {"url": loaded[ref]}})
            item["content"] = content
            if "runtime_content" in item:
                item["runtime_content"] = copy.deepcopy(content)
        result.append(item)
    return result


def portrait_png(store, wid: str, file: dict) -> str:
    """Store an attached image as a card portrait blob.

    Portraits use the standard PNG card container, so JPEG/WebP pixels are re-encoded
    as PNG once; export later embeds the card JSON into this image.
    """
    from tavern.character.editor_attachments import attachment_path

    if file["extension"] not in IMAGE_FORMATS:
        raise ValueError("头像只能使用本对话中的 PNG、JPEG 或静态 WebP 图片附件。")
    try:
        data = attachment_path(store, wid, file).read_bytes()
    except OSError:
        raise ValueError(f"图片附件 {file['ref']} 无法读取，请恢复附件文件后重试。") from None
    image_metadata(file["extension"], data)
    if file["extension"] != ".png":
        buffer = BytesIO()
        with Image.open(BytesIO(data)) as image:
            (image if image.mode in ("RGB", "RGBA", "L", "LA", "P") else image.convert("RGBA")).save(buffer, "PNG")
        data = buffer.getvalue()
    return store.blob(data)


def apply_portrait(item: dict, digest: str | None) -> None:
    """A draft portrait lives in its container source, exactly as saved library cards do."""
    source = item.get("source", {})
    if source.get("container", "json") == "charx":
        raise ValueError("CHARX 卡的头像来自卡内资源声明，暂不支持在制卡对话中更换。")
    item["source"] = {**source, "container": "png" if digest else "json", "original": digest}
    item["kind"] = "draft"
