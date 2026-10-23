"""Explicit GGUF import and CPU inference; saved CUDA settings are never rewritten."""

import asyncio
import hashlib
import json
import os
import threading
from pathlib import Path

from tavern.android.local_model_server import (
    CPU_THREADS,
    EXECUTABLE,
    ModelRequestRejected,
    NativeModelServer,
)
from tavern.character.cards import lore_title
from tavern.search.local_models import PURPOSES, LocalModelManager
from tavern.search.retrieval import digest, grams

SOURCES = json.loads(Path(__file__).with_name("model_sources.json").read_text("utf-8"))


def read_manifest(path):
    manifest = json.loads(path.read_text("utf-8"))
    if manifest.get("version") != 1:
        raise ValueError("不支持的 GGUF 缓存格式，请重新导入对应模型文件")
    return manifest


def mobile_config(purpose):
    source = SOURCES[purpose]
    return {
        "model": source["repository"],
        "revision": source["revision"],
        "device": "cpu",
        "quantization": "q8_0",
        "batch_size": 1,
        "dimensions": 1024,
    }


def configuration_matches(purpose, config):
    expected = mobile_config(purpose)
    return all(
        getattr(config, key) == expected[key] for key in ("model", "revision", "device", "quantization")
    )


async def infer_native(server, config, request):
    action, data = request["action"], request["data"]
    instruction = request["instruction_prompt"]
    batch_size = config.batch_size

    async def encode(texts):
        results = []
        for start in range(0, len(texts), batch_size):
            results.extend(await server.embeddings(texts[start : start + batch_size], config.dimensions))
        return results

    if action == "embed":
        return await encode(
            [f"Instruct: {instruction}\nQuery: {text}" if request["query"] else text for text in data]
        )
    if action == "index":
        result = []
        size, overlap = request["chunk_tokens"], request["chunk_overlap"]
        for entry in data:
            title = " ".join(
                [lore_title(entry) if request["title_in_retrieval"] else "", *entry.get("keys", [])]
            )
            title_ids = (await server.tokenize(title))[: size // 4]
            tokens = await server.tokenize(entry["content"])
            capacity = size - len(title_ids)
            # This is the existing retrieval-fragment contract. Runtime still injects
            # the complete admitted entry; these fragments are only index candidates.
            texts = [
                await server.detokenize(title_ids + tokens[start : start + capacity])
                for start in range(0, max(1, len(tokens)), max(1, capacity - overlap))
            ]
            result.append({"fragments": texts, "vectors": await encode(texts)})
        return result
    if action == "rerank":
        prefix = "<|im_start|>system\n" + request["reranker_prompt"] + "<|im_end|>\n<|im_start|>user\n"
        suffix = "<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n"
        result = []
        for query, document in data:
            if isinstance(document, list):
                fragments = document
            else:
                tokens = await server.tokenize(document)
                size = request["chunk_tokens"]
                fragments = [
                    await server.detokenize(tokens[start : start + size])
                    for start in range(0, max(1, len(tokens)), size - request["chunk_overlap"])
                ]
                query_grams = set(grams(query))
                fragments.sort(key=lambda text: -len(query_grams & set(grams(text))))
            texts = [
                prefix + f"<Instruct>: {instruction}\n<Query>: {query}\n<Document>: {text}" + suffix
                for text in fragments[: request["rerank_fragments"]]
            ]
            result.append(max(await encode(texts)))
        return result
    raise ValueError("无效推理操作")


class AndroidLocalModelManager(LocalModelManager):
    def __init__(self, store):
        super().__init__(store)
        directory = os.environ.get("TAVERN_NATIVE_LIB_DIR")
        self.native_directory = Path(directory) if directory else None
        self.servers = {}

    def path(self, config):
        return self.root / "gguf" / digest([config.model, config.revision, config.quantization])

    def revision(self, settings):
        manifest = self.path(settings.local_models.embedding) / "tavern-ready.json"
        if not manifest.exists():
            return "unavailable"
        saved = read_manifest(manifest)
        # GGUF/engine identity separates derived vectors from desktop BF16/INT8 caches.
        return digest([SOURCES["engine"]["commit"], saved["sha256"]])

    def require_available(self):
        if not self.native_directory or not (self.native_directory / EXECUTABLE).is_file():
            raise ValueError("Android 原生模型引擎缺失，请安装包含本地模型支持的 APK")

    def status(self, settings):
        available = bool(self.native_directory and (self.native_directory / EXECUTABLE).is_file())
        models = {}
        for purpose in PURPOSES:
            config = getattr(settings.local_models, purpose)
            matches = configuration_matches(purpose, config)
            server = self.servers.get(purpose)
            source = SOURCES[purpose]
            models[purpose] = {
                "model": config.model,
                "quantization": config.quantization,
                "downloaded": matches and (self.path(config) / "tavern-ready.json").is_file(),
                "ready": bool(
                    available
                    and matches
                    and server
                    and server.alive
                    and purpose in self.ready
                    and self.loaded.get(purpose) == digest(config.model_dump())
                ),
                "configuration_supported": matches,
                "configuration_error": ""
                if matches
                else "当前保存的模型配置不适用于 Android，请显式选择手机 CPU/Q8_0 配置。",
                "suggested_config": mobile_config(purpose),
                "filename": source["filename"],
                "bytes": source["bytes"],
                "source_url": f"https://huggingface.co/{source['repository']}/tree/{source['revision']}",
                "source_label": "Qwen 官方 GGUF"
                if purpose == "embedding"
                else "官方源权重经固定 llama.cpp 转换的 GGUF",
            }
        return {
            "startup_id": self.startup_id,
            "platform": "android",
            "available": available,
            "reason": "" if available else "APK 中缺少原生模型引擎。",
            "engine": "llama.cpp " + SOURCES["engine"]["tag"],
            "cpu_threads": CPU_THREADS,
            "models": models,
            "jobs": list(self.jobs.values())[-30:],
            "metrics": self.metrics,
        }

    def _config(self, purpose, settings):
        config = getattr(settings.local_models, purpose)
        if not configuration_matches(purpose, config):
            raise ValueError("请在本地资源中显式选择手机 CPU/Q8_0 配置；原 CUDA 设置未改变")
        return config

    def download(self, purpose, settings):
        self.require_available()
        raise ValueError("Android 请使用本地资源页的“导入 GGUF”选择对应模型文件")

    async def import_model(self, purpose, file, settings):
        self.require_available()
        config = self._config(purpose, settings)
        source = SOURCES[purpose]
        if file.size is not None and file.size != source["bytes"]:
            raise ValueError("模型大小不符，请选择 " + source["filename"])
        if self.locks[purpose].locked():
            raise ValueError("模型正在运行，请稍后导入")
        async with self.locks[purpose]:
            folder = self.path(config)
            folder.mkdir(parents=True, exist_ok=True)
            pending = folder / "model.gguf.part"
            cancelled = threading.Event()

            def copy_and_verify():
                total = 0
                checksum = hashlib.sha256()
                # UploadFile is already spooled before this handler. One worker avoids
                # a threadpool round trip for every MiB on embedded Android Python.
                with pending.open("wb") as output:
                    while not cancelled.is_set():
                        block = file.file.read(1024 * 1024)
                        if not block:
                            break
                        total += len(block)
                        if total > source["bytes"]:
                            raise ValueError("模型文件超过预期大小")
                        checksum.update(block)
                        output.write(block)
                if cancelled.is_set():
                    return total
                if total != source["bytes"] or checksum.hexdigest() != source["sha256"]:
                    raise ValueError("模型 SHA-256 不匹配，请选择随此 APK 提供的 " + source["filename"])
                return total

            copying = asyncio.create_task(asyncio.to_thread(copy_and_verify))
            try:
                try:
                    total = await asyncio.shield(copying)
                except asyncio.CancelledError:
                    cancelled.set()
                    await copying
                    raise
                await self._stop(purpose)
                pending.replace(folder / "model.gguf")
                manifest = folder / "tavern-ready.json.part"
                manifest.write_text(
                    json.dumps({"version": 1, "revision": source["revision"], "sha256": source["sha256"]}),
                    "utf-8",
                )
                manifest.replace(folder / "tavern-ready.json")
            finally:
                pending.unlink(missing_ok=True)
        return {"imported": True, "filename": source["filename"], "bytes": total}

    async def _stop(self, purpose):
        self.ready.discard(purpose)
        self.loaded.pop(purpose, None)
        server = self.servers.pop(purpose, None)
        if server is not None:
            await server.close()

    async def unload(self, purpose):
        async with self.locks[purpose]:
            await self._stop(purpose)

    async def warmup(self, purpose, settings):
        self.require_available()
        config = self._config(purpose, settings)
        async with self.locks[purpose]:
            folder = self.path(config)
            if not (folder / "tavern-ready.json").is_file():
                raise ValueError("请先导入对应 GGUF 模型")
            manifest = read_manifest(folder / "tavern-ready.json")
            if manifest["sha256"] != SOURCES[purpose]["sha256"]:
                raise ValueError("GGUF 缓存与此版本支持的模型不同，请重新导入对应文件")
            await self._stop(purpose)
            server = NativeModelServer(self.native_directory, folder / "model.gguf", purpose)
            self.servers[purpose] = server
            try:
                await server.start()
            except BaseException:
                await self._stop(purpose)
                raise
            self.metrics[purpose] = {"device": f"Android CPU（{CPU_THREADS} 线程）"}
            self.loaded[purpose] = digest(config.model_dump())
            self.ready.add(purpose)

    async def infer(self, purpose, action, data, settings, *, timeout, query=False):
        self.require_available()
        config = self._config(purpose, settings)
        if self.locks[purpose].locked():
            raise RuntimeError("本地模型忙，已跳过本次推理以避免排队延迟")
        async with self.locks[purpose]:
            if purpose not in self.ready or self.loaded.get(purpose) != digest(config.model_dump()):
                raise RuntimeError("模型尚未预热或配置已改变")
            request = {
                "action": action,
                "data": data,
                "query": query,
                "instruction_prompt": settings.local_models.instruction_prompt,
                "reranker_prompt": settings.local_models.reranker_prompt,
                "chunk_tokens": settings.retrieval.chunk_tokens,
                "chunk_overlap": settings.retrieval.chunk_overlap,
                "title_in_retrieval": settings.retrieval.title_in_retrieval,
                "rerank_fragments": settings.local_models.rerank_fragments,
            }
            try:
                async with asyncio.timeout(timeout):
                    return await infer_native(self.servers[purpose], config, request)
            except TimeoutError as error:
                await self._stop(purpose)
                raise TimeoutError("Android 本地推理超时，模型已卸载；请调整检索超时并重新预热") from error
            except ModelRequestRejected:
                # Requests are awaited one at a time, so a rejected one (for example an input
                # beyond the context window) leaves the engine idle; keep the model loaded.
                raise
            except BaseException:
                # A cancelled HTTP request may still be computing. Stop its owned process
                # before accepting another request; never consume a stale result.
                await self._stop(purpose)
                raise

    async def close(self):
        await super().close()
        await asyncio.gather(*(self.unload(purpose) for purpose in PURPOSES))
