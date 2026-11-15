"""Materialize durable story image references only at the model request boundary."""

import base64
import copy
import hashlib

from tavern.character.cards import strip_card_png_data
from tavern.media.storage import asset_by_ref
from tavern.media.tools import IMAGE_REFS

MAX_IMAGE_BYTES = 32 * 1024 * 1024


def read_image(store, asset):
    path = store.root / "blobs" / asset["blob"]
    try:
        with path.open("rb") as stream:
            data = stream.read(MAX_IMAGE_BYTES + 1)
    except OSError:
        raise ValueError("图片文件无法读取，请恢复文件后重试") from None
    if len(data) != asset["size"] or len(data) > MAX_IMAGE_BYTES:
        raise ValueError("图片文件大小已改变或超过 32 MiB")
    if hashlib.sha256(data).hexdigest() != asset["blob"]:
        raise ValueError("图片文件内容已改变，请从完整备份恢复")
    # Previously saved assets and imported packages can still contain a PNG card.
    return strip_card_png_data(data) if asset["mime"] == "image/png" else data


def materialize(messages, store, sid):
    if not any(m.get(IMAGE_REFS) for m in messages):
        return messages
    loaded, result = {}, []
    for original in messages:
        refs = original.get(IMAGE_REFS, [])
        if not refs:
            result.append(original)
            continue
        item = copy.deepcopy(original)
        if item["role"] != "user" or not isinstance(item["content"], str):
            raise ValueError("故事图片参考必须保留在用户参考消息中")
        content = [{"type": "text", "text": item["content"]}]
        for ref in refs:
            if ref not in loaded:
                asset = asset_by_ref(store, sid, ref)
                # A persisted view receipt is the original authorization. Removing a
                # gallery selection cannot erase an already-sent historical exchange.
                loaded[ref] = (
                    "data:"
                    + asset["mime"]
                    + ";base64,"
                    + base64.b64encode(read_image(store, asset)).decode("ascii")
                )
            content.append({"type": "image_url", "image_url": {"url": loaded[ref]}})
        item["content"] = content
        if "runtime_content" in item:
            item["runtime_content"] = copy.deepcopy(content)
        result.append(item)
    return result
