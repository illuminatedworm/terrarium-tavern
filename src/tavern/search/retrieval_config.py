"""Validated, independently configurable retrieval and provider profiles."""

import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from tavern.core.cache_policy import CacheSettings
from tavern.core.message_adapter import MessageAdapterConfig
from tavern.core.prompts import (
    DEFAULT_LOCAL_INSTRUCTION,
    DEFAULT_QUERY_PROMPT,
    DEFAULT_RERANKER_PROMPT,
    DEFAULT_RETRIEVAL_PROTOCOL,
)

ApiMode = Literal["chat_completions", "responses", "anthropic", "gemini"]


def api_root(base_url, api_mode, api_path=""):
    return base_url.rstrip("/") + "/" + (api_path or ("v1beta" if api_mode == "gemini" else "v1"))


class Provider(BaseModel):
    id: str = Field(min_length=1, max_length=100)
    name: str = "默认提供商"
    base_url: str = "https://api.openai.com"
    api_key: str = ""
    use_proxy: bool = True
    auth_mode: Literal["api_key", "chatgpt_subscription"] = "api_key"
    chatgpt_account_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")

    @model_validator(mode="after")
    def valid_url(self):
        from urllib.parse import urlparse

        if self.auth_mode == "chatgpt_subscription":
            self.base_url = "https://api.openai.com"
        parsed = urlparse(self.base_url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError("模型地址必须为 HTTP(S)")
        if parsed.query or parsed.fragment or parsed.username or parsed.password:
            raise ValueError("接口地址不能包含查询参数、片段或登录凭据")
        if parsed.path.strip("/"):
            raise ValueError("提供商只填写主域名，API 路径请在模型设置中填写")
        self.base_url = self.base_url.rstrip("/")
        return self


class ModelConnection(BaseModel):
    model: str = ""
    api_mode: ApiMode = "chat_completions"
    api_path: str = Field(default="", max_length=256)

    @field_validator("api_path")
    @classmethod
    def valid_api_path(cls, value):
        value = value.strip().strip("/")
        if value and (
            not re.fullmatch(r"[A-Za-z0-9._~-]+(?:/[A-Za-z0-9._~-]+)*", value)
            or any(part in (".", "..") for part in value.split("/"))
        ):
            raise ValueError("API 路径应为 v1、v1beta 等路径，不含域名或查询参数")
        return value


class ModelProfile(ModelConnection):
    provider_id: str = "default"
    # Only inactive providers live here. The visible fields above remain the
    # sole source for the selected provider, including imports and page undo.
    provider_connections: dict[str, ModelConnection] = Field(default_factory=dict)
    message_adapter: MessageAdapterConfig = Field(default_factory=MessageAdapterConfig)
    tokenizer: Literal["auto", "o200k_base", "cl100k_base"] = "auto"
    context_tokens: int = Field(32768, ge=2048, le=2000000)
    reply_tokens: int = Field(2048, ge=128, le=65536)
    temperature: float | None = Field(0.8, ge=0, le=2)
    top_p: float | None = Field(None, ge=0, le=1)
    top_k: int | None = Field(None, ge=0)
    frequency_penalty: float | None = Field(None, ge=-2, le=2)
    presence_penalty: float | None = Field(None, ge=-2, le=2)
    cache: CacheSettings = Field(default_factory=CacheSettings)
    # Accepted only for importing old profiles; new saved documents use cache.
    prompt_cache_key: str | None = Field(None, max_length=256, exclude=True)
    prompt_cache_mode: Literal["default", "no_write", "instructions"] = Field("default", exclude=True)
    prompt_cache_retention: Literal["default", "in_memory", "24h"] = Field("default", exclude=True)
    reasoning_effort: Literal["default", "none", "minimal", "low", "medium", "high", "xhigh", "max"] = (
        "default"
    )
    reasoning_format: Literal["reasoning_effort", "reasoning"] = "reasoning_effort"
    capability: Literal["unknown", "accepted", "unsupported"] = "unknown"
    capability_key: str = ""

    def for_provider(self, provider_id: str) -> "ModelProfile":
        result = self.model_copy(deep=True)
        if provider_id == self.provider_id:
            return result
        connection = result.provider_connections.pop(provider_id, ModelConnection())
        if self.provider_id:
            result.provider_connections[self.provider_id] = ModelConnection(
                model=self.model, api_mode=self.api_mode, api_path=self.api_path
            )
        return result.model_copy(update={"provider_id": provider_id, **connection.model_dump()})

    @model_validator(mode="after")
    def budget(self):
        if self.provider_id in self.provider_connections:
            raise ValueError("当前提供商的连接参数重复，请保留模型设置中显示的参数并移除重复记录")
        if self.context_tokens <= self.reply_tokens + 2048:
            raise ValueError("模型上下文窗口必须大于输出预算加 2048 tokens")
        return self


class Network(BaseModel):
    proxy: str = ""
    api_proxy: bool = True
    hf_proxy: bool = True
    resources_proxy: bool = True


class RetrievalSettings(BaseModel):
    channels: list[Literal["keyword", "bm25", "embedding"]] = Field(
        default_factory=lambda: ["keyword", "bm25"]
    )
    candidate_count: int = Field(30, ge=1, le=1000)
    fusion_count: int = Field(20, ge=1, le=1000)
    rrf_k: int = Field(60, ge=1, le=1000)
    weights: dict[str, float] = Field(default_factory=lambda: {"keyword": 1, "bm25": 1, "embedding": 1})
    bm25_k1: float = Field(1.2, gt=0, le=10)
    bm25_b: float = Field(0.75, ge=0, le=1)
    gram_weight: float = Field(0.2, ge=0, le=1)
    bm25_min_coverage: float = Field(0.25, ge=0, le=1)
    bm25_min_score: float = Field(0.35, ge=0, le=1)
    embedding_min_similarity: float = Field(0.5, ge=-1, le=1)
    rerank_min_score: float = Field(0.5, ge=0, le=1)
    query_lookback: int = Field(1, ge=0, le=20)
    query_chars: int = Field(512, ge=32, le=4096)
    input_chars: int = Field(2000, ge=64, le=20000)
    chunk_tokens: int = Field(512, ge=64, le=4096)
    chunk_overlap: int = Field(64, ge=0, le=1024)
    # Title text of ordinary entries in BM25/vector fragments, rerank fallback and
    # subject headings. Keys and content are always matched.
    title_in_retrieval: bool = True
    embedding_timeout: float = Field(1.0, gt=0, le=120)
    index_timeout: int = Field(60, ge=1, le=3600, strict=True)
    rerank_timeout: float = Field(1.5, gt=0, le=120)
    query_prompt: str = Field(default=DEFAULT_QUERY_PROMPT, max_length=200000)
    protocol_prompt: str = Field(default=DEFAULT_RETRIEVAL_PROTOCOL, max_length=200000)

    @model_validator(mode="after")
    def valid(self):
        if not self.channels or len(set(self.channels)) != len(self.channels):
            raise ValueError("至少选择一种召回方案，且不能重复")
        if self.chunk_overlap >= self.chunk_tokens:
            raise ValueError("切片重叠必须小于切片长度")
        if any(
            k not in ("keyword", "bm25", "embedding") or not 0 < v <= 100 for k, v in self.weights.items()
        ):
            raise ValueError("召回权重必须为 0–100 之间的正数")
        return self


class LocalModel(BaseModel):
    model: str
    revision: str = "main"
    device: str = "cuda:0"
    quantization: Literal["none", "int8", "q8_0"] = "none"
    batch_size: int = Field(8, ge=1, le=128)
    dimensions: int = Field(1024, ge=32, le=2560)


class LocalModels(BaseModel):
    embedding: LocalModel = Field(default_factory=lambda: LocalModel(model="Qwen/Qwen3-Embedding-0.6B"))
    reranker: LocalModel = Field(default_factory=lambda: LocalModel(model="Qwen/Qwen3-Reranker-0.6B"))
    instruction_prompt: str = Field(default=DEFAULT_LOCAL_INSTRUCTION, max_length=200000)
    reranker_prompt: str = Field(default=DEFAULT_RERANKER_PROMPT, max_length=200000)
    reranker_enabled: bool = False
    rerank_fragments: int = Field(3, ge=1, le=8)

    @model_validator(mode="after")
    def supported(self):
        for purpose, prefix in (("embedding", "Embedding"), ("reranker", "Reranker")):
            config = getattr(self, purpose)
            if config.quantization == "q8_0":
                model = f"Qwen/Qwen3-{prefix}-0.6B" + ("-GGUF" if purpose == "embedding" else "")
                if config.model != model or config.device not in ("cpu", "opencl:0", "hexagon:0"):
                    raise ValueError("Q8_0 需要对应的 Qwen3 0.6B GGUF 模型与 cpu、opencl:0 或 hexagon:0 设备")
                continue
            if config.model not in [f"Qwen/Qwen3-{prefix}-{size}" for size in ("0.6B", "4B")]:
                raise ValueError("目前支持 Qwen3 的 0.6B 与 4B 检索模型")
            if not config.device.startswith("cuda:"):
                raise ValueError("本地检索模型需要 CUDA GPU，不会自动回退到 CPU")
        if self.embedding.dimensions > (1024 if "0.6B" in self.embedding.model else 2560):
            raise ValueError("向量维度超过模型原生维度")
        return self
