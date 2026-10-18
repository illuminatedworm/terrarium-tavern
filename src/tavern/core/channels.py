"""A story's connection family is immutable; endpoints and model names are not identities."""

from typing import Literal

Channel = Literal["standard", "chatgpt"]
CHANNELS = ("standard", "chatgpt")
LABELS = {"standard": "普通 API", "chatgpt": "ChatGPT 官方"}


def provider_channel(provider):
    mode = provider.get("auth_mode", "api_key") if isinstance(provider, dict) else provider.auth_mode
    return "chatgpt" if mode == "chatgpt_subscription" else "standard"


def require_channel(settings, channel):
    if channel not in CHANNELS:
        raise ValueError("请先确认此旧对话的渠道归属")
    if provider_channel(settings) != channel:
        raise ValueError(f"此对话属于{LABELS[channel]}，不能使用另一渠道的连接")


def profile_key(role, channel):
    key = "model" if role == "full_agent" else role
    return "official_" + key if channel == "chatgpt" and role != "card_author" else key
