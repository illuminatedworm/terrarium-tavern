"""Owned llama.cpp processes launched from Android's installed native-library directory."""

import asyncio
import logging
import math
import secrets
from pathlib import Path

import httpx

LOGGER = logging.getLogger(__name__)
CONTEXT_TOKENS = 4096
CPU_THREADS = 4
EXECUTABLE = "libtavern_llama_server.so"


class ModelRequestRejected(RuntimeError):
    """The engine answered with an error, so no computation is left in flight."""


class NativeModelServer:
    def __init__(self, native_directory: Path, model: Path, purpose: str):
        self.native_directory = native_directory
        self.model = model
        self.purpose = purpose
        self.process = None
        self.client = None
        self.url = None
        self.ready = None
        self.key = secrets.token_hex(32)

    @property
    def alive(self):
        return self.process is not None and bool(self.process.isAlive())

    def _launch(self, on_ready):
        from java import jarray, jclass

        command = [
            str(self.native_directory / EXECUTABLE),
            "-m",
            str(self.model),
            "--host",
            "127.0.0.1",
            "--port",
            "0",
            "--api-key",
            self.key,
            "--embedding",
            "--pooling",
            "last" if self.purpose == "embedding" else "rank",
            "-ngl",
            "0",
            "-c",
            str(CONTEXT_TOKENS),
            "-b",
            str(CONTEXT_TOKENS),
            "-ub",
            "512",
            "-np",
            "1",
            "-t",
            str(CPU_THREADS),
            "-tb",
            str(CPU_THREADS),
            "-fa",
            "off",
            "--fit",
            "off",
            "--no-warmup",
            "--no-cache-prompt",
            "--no-webui",
            "--no-slots",
            "--threads-http",
            "1",
            "--poll",
            "0",
        ]
        if self.purpose == "reranker":
            # b11829's /embedding reader otherwise reads 1024 values from a two-value
            # rank buffer. This changes the declared output size, not the weights/prompt.
            command += ["--override-kv", "qwen3.embedding_length_out=int:2"]
        self.process = jclass("com.terrarium.tavern.LocalModelProcess").start(
            jarray(jclass("java.lang.String"))(command), str(self.native_directory), on_ready
        )

    async def start(self):
        loop = asyncio.get_running_loop()
        self.ready = loop.create_future()

        def complete(address):
            if not self.ready.done():
                self.ready.set_result(str(address) if address is not None else None)

        def on_ready(address):
            loop.call_soon_threadsafe(complete, address)

        try:
            # ProcessBuilder is used instead of multiprocessing/fork in embedded Android Python.
            # Starting the child is short and synchronous so cancellation cannot race
            # a background launch and leave a process created after close() returns.
            self._launch(on_ready)
            self.url = await asyncio.wait_for(self.ready, 45)
            if not self.url or not self.alive:
                LOGGER.error("Android %s load failed:\n%s", self.purpose, self.process.getLogTail())
                raise RuntimeError("Android 本地模型加载失败，请查看运行日志")
            self.client = httpx.AsyncClient(
                base_url=self.url,
                headers={"Authorization": "Bearer " + self.key},
                trust_env=False,
                timeout=None,
            )
        except BaseException:
            await self.close()
            raise

    async def close(self):
        try:
            if self.process is not None:
                await asyncio.to_thread(self.process.close)
                self.process = None
        finally:
            # A failed native shutdown must not also leave its HTTP pool open.
            if self.client is not None:
                await self.client.aclose()
                self.client = None

    async def post(self, route, payload):
        if not self.alive:
            LOGGER.error("Android %s exited:\n%s", self.purpose, self.process.getLogTail())
            raise RuntimeError("Android 本地模型进程已退出，请重新预热")
        response = await self.client.post(route, json=payload)
        if not response.is_success:
            LOGGER.error("Android model HTTP %s: %s", response.status_code, response.text)
            raise ModelRequestRejected(
                f"Android 本地模型拒绝请求（HTTP {response.status_code}），请查看运行日志"
            )
        return response.json()

    async def tokenize(self, text):
        result = await self.post("/tokenize", {"content": text, "add_special": False, "parse_special": True})
        return result["tokens"]

    async def detokenize(self, tokens):
        return (await self.post("/detokenize", {"tokens": tokens}))["content"]

    async def embeddings(self, texts, dimensions):
        result = await self.post("/embedding", {"content": texts, "embd_normalize": -1})
        if len(result) != len(texts):
            raise RuntimeError("原生模型返回的条目数不符")
        rows = []
        native_dimensions = 1024 if self.purpose == "embedding" else 2
        for index, item in enumerate(result):
            values = item["embedding"][0]
            if (
                item["index"] != index
                or len(values) != native_dimensions
                or not all(map(math.isfinite, values))
            ):
                raise RuntimeError("原生模型返回的向量顺序、维度或数值无效")
            if self.purpose == "reranker":
                if not (0 <= values[0] <= 1 and abs(sum(values) - 1) < 0.0001):
                    raise RuntimeError("原生重排模型返回的概率无效")
                rows.append(values[0])
            else:
                # Match desktop Matryoshka dimensions: slice first, then normalize.
                vector = values[:dimensions]
                norm = math.sqrt(sum(value * value for value in vector))
                if norm == 0:
                    raise RuntimeError("原生向量模型返回了零向量")
                rows.append([value / norm for value in vector])
        return rows
