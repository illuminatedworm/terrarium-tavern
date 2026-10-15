"""Visible web service settings and per-agent permission, independent of model vendors."""
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

Service = Literal["exa", "tavily", "searxng", "duckduckgo"]
ORDER = ["exa", "tavily", "searxng", "duckduckgo"]


class AgentWebConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["off", "auto", "local"] = "auto"

    @property
    def enabled(self):
        return self.mode != "off"


class ServiceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = True


class KeyServiceConfig(ServiceConfig):
    auth_mode: Literal["keyless", "api_key"] = "keyless"
    api_key: str = Field(default="", repr=False)


class SearXNGConfig(ServiceConfig):
    endpoint: str = ""

    @model_validator(mode="after")
    def valid_endpoint(self):
        if self.endpoint:
            url = urlsplit(self.endpoint)
            if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password or url.query or url.fragment:
                raise ValueError("SearXNG 地址必须为无凭据、查询参数或片段的 HTTP(S) 地址")
        return self


class WebConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    order: list[Service] = Field(default_factory=lambda: ORDER.copy())
    exa: KeyServiceConfig = Field(default_factory=KeyServiceConfig)
    tavily: KeyServiceConfig = Field(default_factory=KeyServiceConfig)
    searxng: SearXNGConfig = Field(default_factory=SearXNGConfig)
    duckduckgo: ServiceConfig = Field(default_factory=ServiceConfig)
    timeout_seconds: int = Field(default=25, ge=5, le=120, strict=True)

    @model_validator(mode="after")
    def all_services(self):
        if len(self.order) != len(ORDER) or set(self.order) != set(ORDER):
            raise ValueError("联网服务排序必须恰好包含 Exa、Tavily、SearXNG 和 DuckDuckGo 各一次")
        return self


def migrate_web_settings(shared):
    """Consume the old Advisor-owned service configuration exactly once."""
    old = shared.get("advisor", {}).get("web", {})
    if "mode" in old or not old:
        return False
    shared.setdefault("web", {
        "timeout_seconds": old.get("timeout_seconds", 25),
        "searxng": {"endpoint": old.get("endpoint", "")},
    })
    shared["advisor"]["web"] = {"mode": "auto" if old.get("enabled", True) else "off"}
    return True


def public_web(config):
    value = config.model_dump()
    for name in ("exa", "tavily"):
        value[name]["has_api_key"] = bool(value[name].pop("api_key"))
    return value


def merge_web_secrets(value, previous):
    """Omitted credentials preserve secrets; an explicit empty string clears them."""
    from copy import deepcopy
    result = deepcopy(value)
    for name in ("exa", "tavily"):
        if name in result and isinstance(result[name], dict):
            result[name].pop("has_api_key", None)
            result[name].setdefault("api_key", previous.get(name, {}).get("api_key", ""))
    return result
