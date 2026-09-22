"""Bounded CUDA workers. Only explicit download/warmup jobs may load models."""
import asyncio
import fnmatch
import json
import multiprocessing as mp
import os
import time
import uuid
from pathlib import Path

from tavern.search.retrieval import digest


def _worker(pipe, root, purpose, config):
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    try:
        import torch
        from transformers import AutoModel, AutoModelForCausalLM, AutoTokenizer
        if not torch.cuda.is_available():
            raise RuntimeError("CUDA 不可用；请安装 CUDA 版 PyTorch 并检查显卡驱动")
        device = config["device"]
        free, _ = torch.cuda.mem_get_info(device)
        required = (9 if config["model"].endswith("4B") else 2) * 1024 ** 3
        if free < required:
            raise RuntimeError("空闲显存不足，请选择 0.6B 或释放其他 GPU 应用")
        path = str(Path(root) / digest([config["model"], config["revision"]]))
        tokenizer = AutoTokenizer.from_pretrained(path, local_files_only=True, padding_side="left")
        cls = AutoModel if purpose == "embedding" else AutoModelForCausalLM
        model = cls.from_pretrained(path, local_files_only=True, dtype=torch.bfloat16).to(device).eval()
        pipe.send({"ready": True, "metrics": {"device": torch.cuda.get_device_name(device),
                                              "allocated_bytes": torch.cuda.memory_allocated(device)}})
        while True:
            request = pipe.recv()
            action, data = request["action"], request["data"]
            instruction = request["instruction_prompt"]
            results = []
            batch_size = config["batch_size"]
            with torch.inference_mode():
                if action == "embed":
                    texts = [f"Instruct: {instruction}\nQuery: {t}" if request.get("query") else t for t in data]
                    for offset in range(0, len(texts), batch_size):
                        inputs = tokenizer(texts[offset:offset + batch_size], padding=True, truncation=True,
                                           max_length=4096, return_tensors="pt").to(device)
                        output = model(**inputs).last_hidden_state[:, -1]
                        output = torch.nn.functional.normalize(output[:, :config["dimensions"]].float(), dim=1)
                        results.extend(output.cpu().tolist())
                elif action == "index":
                    size, overlap = request["chunk_tokens"], request["chunk_overlap"]
                    for entry in data:
                        title = " ".join([str(entry.get("comment", "")), str(entry.get("name", "")), *entry.get("keys", [])])
                        title_ids = tokenizer.encode(title, add_special_tokens=False)[:size // 4]
                        ids = tokenizer.encode(entry["content"], add_special_tokens=False)
                        capacity = size - len(title_ids)
                        stride = max(1, capacity - overlap)
                        texts = [tokenizer.decode(title_ids + ids[start:start + capacity])
                                 for start in range(0, max(1, len(ids)), stride)]
                        vectors = []
                        for offset in range(0, len(texts), batch_size):
                            inputs = tokenizer(texts[offset:offset + batch_size], padding=True, truncation=True,
                                               max_length=size, return_tensors="pt").to(device)
                            output = model(**inputs).last_hidden_state[:, -1]
                            vectors.extend(torch.nn.functional.normalize(
                                output[:, :config["dimensions"]].float(), dim=1).cpu().tolist())
                        results.append({"fragments": texts, "vectors": vectors})
                elif action == "rerank":
                    prefix = '<|im_start|>system\n' + request["reranker_prompt"] + '<|im_end|>\n<|im_start|>user\n'
                    suffix = '<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n'
                    yes, no = tokenizer.convert_tokens_to_ids("yes"), tokenizer.convert_tokens_to_ids("no")
                    for query, document in data:
                        if isinstance(document, list):
                            fragments = document
                        else:
                            ids = tokenizer.encode(document, add_special_tokens=False)
                            size = request["chunk_tokens"]
                            fragments = [tokenizer.decode(ids[i:i + size]) for i in range(
                                0, max(1, len(ids)), size - request["chunk_overlap"])]
                            from tavern.search.retrieval import grams
                            qgrams = set(grams(query))
                            fragments.sort(key=lambda t: -len(qgrams & set(grams(t))))
                        texts = [prefix + f"<Instruct>: {instruction}\n<Query>: {query}\n<Document>: {text}" + suffix
                                 for text in fragments[:request["rerank_fragments"]]]
                        inputs = tokenizer(texts, padding=True, truncation=True, max_length=4096,
                                           return_tensors="pt").to(device)
                        logits = model(**inputs).logits[:, -1, [no, yes]].float()
                        results.append(float(torch.softmax(logits, dim=1)[:, 1].max().cpu()))
                else:
                    raise ValueError("无效推理操作")
            pipe.send({"result": results, "metrics": {"device": torch.cuda.get_device_name(device),
                "allocated_bytes": torch.cuda.memory_allocated(device),
                "peak_allocated_bytes": torch.cuda.max_memory_allocated(device)}})
    except EOFError:
        pass
    except BaseException as error:
        try:
            pipe.send({"error": str(error)})
        except (BrokenPipeError, OSError):
            pass
    finally:
        pipe.close()


def _download(pipe, root, config, network):
    try:
        os.environ["HF_HUB_DISABLE_XET"] = "1"
        proxy = network["proxy"] if network["hf_proxy"] else ""
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
            os.environ[key] = proxy
        from huggingface_hub import HfApi, snapshot_download
        info = HfApi().model_info(config["model"], revision=config["revision"], files_metadata=True)
        revision = info.sha
        patterns = ["*.json", "*.safetensors", "*.txt", "*.model", "*.tiktoken"]
        total = sum(file.size or 0 for file in info.siblings
                    if any(fnmatch.fnmatch(file.rfilename, pattern) for pattern in patterns))
        pipe.send({"total_bytes": total, "phase": "downloading"})
        target = Path(root) / digest([config["model"], config["revision"]])
        snapshot_download(config["model"], revision=revision, local_dir=target,
                          allow_patterns=patterns)
        (target / "tavern-ready.json").write_text(json.dumps({"revision": revision}), encoding="utf-8")
        pipe.send({"result": {"revision": revision}})
    except BaseException as error:
        pipe.send({"error": str(error)})
    finally:
        pipe.close()


class LocalModelManager:
    def __init__(self, store):
        self.store = store
        self.startup_id = str(uuid.uuid4())
        self.root = store.root / "models"
        self.workers = {}
        self.locks = {name: asyncio.Lock() for name in ("embedding", "reranker")}
        self.jobs = {}
        self.tasks = {}
        self.indexing = set()
        self.ready = set()
        self.metrics = {}

    def path(self, config):
        return self.root / digest([config.model, config.revision])

    def revision(self, settings):
        manifest = self.path(settings.local_models.embedding) / "tavern-ready.json"
        return json.loads(manifest.read_text("utf-8"))["revision"] if manifest.exists() else "unavailable"

    def status(self, settings):
        result = {}
        for purpose in ("embedding", "reranker"):
            config = getattr(settings.local_models, purpose)
            worker = self.workers.get(purpose)
            result[purpose] = {"downloaded": (self.path(config) / "tavern-ready.json").exists(),
                               "ready": bool(purpose in self.ready and worker and worker[0].is_alive()
                                             and worker[2] == digest(config.model_dump())),
                               "model": config.model}
        return {"startup_id": self.startup_id, "models": result,
                "jobs": list(self.jobs.values())[-30:], "metrics": self.metrics}

    def stop_worker(self, purpose):
        self.ready.discard(purpose)
        worker = self.workers.pop(purpose, None)
        if worker:
            process, pipe, _ = worker
            if process.is_alive():
                process.terminate()
            process.join(timeout=1)
            pipe.close()

    async def receive(self, process, pipe, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if pipe.poll():
                value = pipe.recv()
                if "error" in value:
                    raise RuntimeError(value["error"])
                return value
            if not process.is_alive():
                raise RuntimeError("本地推理进程已退出")
            await asyncio.sleep(.01)
        raise TimeoutError("本地推理超时；工作进程已隔离终止，请重新预热")

    async def warmup(self, purpose, settings):
        async with self.locks[purpose]:
            if any(job["kind"] == "download:" + purpose and job["status"] in ("queued", "running") for job in self.jobs.values()):
                raise ValueError("模型仍在下载，请完成后再预热")
            config = getattr(settings.local_models, purpose)
            if not (self.path(config) / "tavern-ready.json").exists():
                raise ValueError("模型未下载，请先下载再预热")
            self.stop_worker(purpose)
            parent, child = mp.get_context("spawn").Pipe()
            process = mp.get_context("spawn").Process(target=_worker,
                args=(child, str(self.root), purpose, config.model_dump()), daemon=True)
            process.start()
            child.close()
            self.workers[purpose] = (process, parent, digest(config.model_dump()))
            try:
                response = await self.receive(process, parent, 180)
                self.metrics[purpose] = response.get("metrics", {})
                self.ready.add(purpose)
            except BaseException:
                self.stop_worker(purpose)
                raise

    async def infer(self, purpose, action, data, settings, *, timeout, query=False):
        if self.locks[purpose].locked():
            raise RuntimeError("本地模型忙，已跳过本次推理以避免排队延迟")
        async with self.locks[purpose]:
            config = getattr(settings.local_models, purpose)
            worker = self.workers.get(purpose)
            if purpose not in self.ready or not worker or worker[2] != digest(config.model_dump()):
                raise RuntimeError("模型尚未预热或配置已改变")
            process, pipe, _ = worker
            try:
                pipe.send({"action": action, "data": data, "query": query,
                           "instruction_prompt": settings.local_models.instruction_prompt,
                           "reranker_prompt": settings.local_models.reranker_prompt,
                           "chunk_tokens": settings.retrieval.chunk_tokens,
                           "chunk_overlap": settings.retrieval.chunk_overlap,
                           "rerank_fragments": settings.local_models.rerank_fragments})
                response = await self.receive(process, pipe, timeout)
                self.metrics[purpose] = response.get("metrics", {})
                return response["result"]
            except BaseException:
                self.stop_worker(purpose)
                raise

    def job(self, kind, action):
        # Keep the last 30 completed jobs for inspection, without retaining task
        # results and closures for the lifetime of the server.
        completed = [jid for jid, task in self.tasks.items() if task.done()]
        for old in completed[:-30]:
            self.tasks.pop(old, None)
            self.jobs.pop(old, None)
        for jid, task in self.tasks.items():
            if not task.done() and self.jobs[jid]["kind"] == kind and kind != "index":
                return self.jobs[jid]
        if sum(not task.done() for task in self.tasks.values()) >= 8:
            raise ValueError("后台任务队列已满，请等待现有任务完成")
        jid = uuid.uuid4().hex
        record = self.jobs[jid] = {"id": jid, "kind": kind, "status": "queued", "progress": 0}

        async def run():
            record["status"] = "running"
            try:
                await action(record)
                record.update(status="complete", progress=1)
            except asyncio.CancelledError:
                record["status"] = "cancelled"
            except Exception as error:
                record.update(status="error", error=str(error))
        self.tasks[jid] = asyncio.create_task(run())
        return record

    def download(self, purpose, settings):
        async def run(record):
            config = getattr(settings.local_models, purpose)
            manifest = self.path(config) / "tavern-ready.json"
            if manifest.exists():
                record.update(phase="cached", **json.loads(manifest.read_text("utf-8")))
                return
            if self.locks[purpose].locked():
                raise ValueError("模型正在运行，请稍后重新下载")
            self.stop_worker(purpose)
            parent, child = mp.get_context("spawn").Pipe()
            process = mp.get_context("spawn").Process(target=_download,
                args=(child, str(self.root), config.model_dump(), settings.network.model_dump()), daemon=True)
            process.start()
            child.close()
            try:
                record["phase"] = "metadata"
                started = time.monotonic()
                initial_bytes = None
                while True:
                    while parent.poll():
                        result = parent.recv()
                        if "error" in result:
                            raise RuntimeError(result["error"])
                        if "result" in result:
                            record.update(result["result"], phase="complete")
                            if record.get("total_bytes"):
                                record["downloaded_bytes"] = record["total_bytes"]
                            return
                        record.update(result)
                    folder = self.path(config)
                    downloaded = 0
                    for path in folder.rglob("*"):
                        try:
                            if path.is_file() and (".cache" not in path.parts or path.suffix == ".incomplete"):
                                downloaded += path.stat().st_size
                        except FileNotFoundError:
                            pass  # HF atomically renames completed temporary files.
                    record["downloaded_bytes"] = downloaded
                    if initial_bytes is None:
                        initial_bytes = downloaded
                    record["bytes_per_second"] = max(0, downloaded - initial_bytes) / max(.5, time.monotonic() - started)
                    if record.get("total_bytes"):
                        record["progress"] = min(.99, downloaded / record["total_bytes"])
                    if not process.is_alive() and not parent.poll():
                        raise RuntimeError("模型下载进程已退出")
                    await asyncio.sleep(.5)
            finally:
                if process.is_alive():
                    process.terminate()
                process.join(timeout=1)
                parent.close()
        return self.job("download:" + purpose, run)

    def schedule_index(self, engine, missing, settings):
        key = digest([k for k, _ in missing])
        if key in self.indexing or not self.status(settings)["models"]["embedding"]["ready"]:
            return
        self.indexing.add(key)

        async def run(record):
            try:
                record.update(total_entries=len(missing), completed_entries=0, phase="indexing")
                for i, (cache_key, entry) in enumerate(missing):
                    record.update(current_entry=str(entry.get("comment") or entry.get("name") or entry["id"]), entry_started_at=time.time())
                    while self.locks["embedding"].locked():
                        await asyncio.sleep(.05)
                    # Another overlapping preparation may have finished this entry while we waited.
                    with self.store.connection() as db:
                        cached = db.execute("SELECT 1 FROM retrieval_vectors WHERE key=?", (cache_key,)).fetchone()
                    if not cached:
                        value = await self.infer("embedding", "index", [entry], settings, timeout=60)
                        with self.store.connection() as db:
                            db.execute("INSERT OR REPLACE INTO retrieval_vectors VALUES (?,?)", (cache_key, json.dumps(value[0])))
                    record.update(progress=(i + 1) / len(missing), completed_entries=i + 1,
                                  last_entry_seconds=round(time.time() - record["entry_started_at"], 2))
                    engine.cache.clear()
                    await asyncio.sleep(.05)
                engine.cache.clear()
            finally:
                self.indexing.discard(key)
        try:
            return self.job("index", run)
        except BaseException:
            self.indexing.discard(key)
            raise

    async def close(self):
        for task in self.tasks.values():
            task.cancel()
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)
        for purpose in list(self.workers):
            self.stop_worker(purpose)
