"""Each native protocol owns its image block shape; inputs use canonical chat parts."""
import copy
import re


def parts(content):
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    if content is None:
        return []
    if not isinstance(content, list) or any(not isinstance(part, dict) for part in content):
        raise ValueError("无效的模型消息内容块")
    return content


def image_url(part):
    image = part.get("image_url")
    if not isinstance(image, dict) or not isinstance(image.get("url"), str):
        raise ValueError("图片内容块缺少有效的 image_url.url")
    return image["url"]


def inline_image(part):
    url = image_url(part)
    match = re.fullmatch(r"data:(image/(?:png|jpeg|webp));base64,([A-Za-z0-9+/]+={0,2})", url)
    if not match:
        raise ValueError("此图片输入须为 PNG、JPEG 或 WebP 的 Base64 data URL，不能忽略或转为文本")
    return match.groups()


def response_content(content, role):
    if isinstance(content, str) or content is None:
        return content
    result = []
    for part in parts(content):
        kind = part.get("type", "text")
        if kind in ("text", "input_text", "output_text"):
            result.append({**copy.deepcopy(part), "type": "output_text" if role == "assistant" else "input_text"})
        elif kind == "image_url" and role == "user":
            image = {"type": "input_image", "image_url": image_url(part)}
            if "detail" in part["image_url"]:
                image["detail"] = part["image_url"]["detail"]
            result.append(image)
        else:
            raise ValueError("Responses 不支持此消息内容块，不能丢弃非文本内容")
    return result


def anthropic_content(content):
    result = []
    for part in parts(content):
        kind = part.get("type", "text")
        if kind in ("text", "input_text", "output_text"):
            result.append({"type": "text", "text": part.get("text", "")})
        elif kind == "image_url":
            mime, data = inline_image(part)
            result.append({"type": "image", "source": {"type": "base64", "media_type": mime, "data": data}})
        else:
            raise ValueError("Anthropic 不支持此消息内容块，不能丢弃非文本内容")
    return result


def gemini_content(content):
    result = []
    for part in parts(content):
        kind = part.get("type", "text")
        if kind in ("text", "input_text", "output_text"):
            result.append({"text": part.get("text", "")})
        elif kind == "image_url":
            mime, data = inline_image(part)
            result.append({"inlineData": {"mimeType": mime, "data": data}})
        else:
            raise ValueError("Gemini 不支持此消息内容块，不能丢弃非文本内容")
    return result
