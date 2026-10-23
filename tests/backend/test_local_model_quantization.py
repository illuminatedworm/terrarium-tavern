import asyncio
import hashlib
import io
import json
import sys
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from tavern.core.models import Settings
from tavern.core.settings import SettingsDocument, migrate_settings
from tavern.search.local_models import LocalModelManager, _serve
from tavern.search.retrieval import digest


def test_quantization_roundtrip_reuses_weights_but_requires_new_worker(tmp_path):
    document = SettingsDocument()
    document.shared.local_models.embedding.quantization = "int8"
    restored = SettingsDocument.model_validate_json(document.model_dump_json())
    assert restored.runtime().local_models.embedding.quantization == "int8"
    with pytest.raises(ValidationError):
        Settings(local_models={"embedding": {"model": "Qwen/Qwen3-Embedding-4B", "quantization": "fp4"}})

    settings = Settings()
    config = settings.local_models.embedding
    manager = LocalModelManager(SimpleNamespace(root=tmp_path))
    path = manager.path(config)
    path.mkdir(parents=True)
    (path / "tavern-ready.json").write_text(json.dumps({"revision": "cached"}), encoding="utf-8")
    manager.host = (SimpleNamespace(is_alive=lambda: True), {})
    manager.loaded["embedding"] = digest(config.model_dump())
    manager.ready.add("embedding")
    assert manager.status(settings)["models"]["embedding"]["ready"]
    config.quantization = "int8"
    assert manager.path(config) == path
    status = manager.status(settings)["models"]["embedding"]
    assert status["downloaded"] and not status["ready"]

    # The format upgrade must preserve CUDA choices. CPU/Q8_0 is an explicit new choice.
    legacy = document.model_dump()
    legacy["version"] = 8
    upgraded = migrate_settings(legacy)
    assert upgraded.version == 9 and upgraded.shared.local_models == document.shared.local_models
    from tavern.android.local_models import mobile_config
    from tavern.search.retrieval_config import LocalModel

    upgraded.shared.local_models.embedding = LocalModel(**mobile_config("embedding"))
    assert (
        SettingsDocument.model_validate_json(upgraded.model_dump_json()).shared.local_models.embedding
        == upgraded.shared.local_models.embedding
    )


async def test_android_import_and_cancellation_preserve_weights_and_stop_only_owned_model(
    tmp_path, monkeypatch
):
    from copy import deepcopy

    from starlette.datastructures import UploadFile

    from tavern.android import local_models as android
    from tavern.android.local_model_server import EXECUTABLE
    from tavern.search.retrieval_config import LocalModel

    native = tmp_path / "native"
    native.mkdir()
    (native / EXECUTABLE).touch()
    monkeypatch.setenv("TAVERN_NATIVE_LIB_DIR", str(native))
    data = b"GGUF-fixture"
    sources = deepcopy(android.SOURCES)
    sources["embedding"].update(bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
    monkeypatch.setattr(android, "SOURCES", sources)
    manager = android.AndroidLocalModelManager(SimpleNamespace(root=tmp_path))
    settings = Settings()
    settings.local_models.embedding = LocalModel(**android.mobile_config("embedding"))
    config = settings.local_models.embedding
    folder = manager.path(config)
    folder.mkdir(parents=True)
    model = folder / "model.gguf"
    model.write_bytes(b"previous")
    with pytest.raises(ValueError, match="SHA-256"):
        await manager.import_model(
            "embedding", UploadFile(io.BytesIO(b"x" * len(data)), size=len(data)), settings
        )
    assert model.read_bytes() == b"previous" and not (folder / "model.gguf.part").exists()
    await manager.import_model("embedding", UploadFile(io.BytesIO(data), size=len(data)), settings)
    assert model.read_bytes() == data
    assert android.read_manifest(folder / "tavern-ready.json")["version"] == 1

    started = asyncio.Event()

    class Server:
        alive = True

        async def embeddings(self, texts, dimensions):
            started.set()
            await asyncio.Event().wait()

        async def close(self):
            self.alive = False

    embedding, reranker = Server(), Server()
    manager.servers.update(embedding=embedding, reranker=reranker)
    manager.loaded["embedding"] = digest(config.model_dump())
    manager.ready.update(("embedding", "reranker"))
    saved = settings.model_dump()
    request = asyncio.create_task(manager.infer("embedding", "embed", ["完整查询"], settings, timeout=10))
    await started.wait()
    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request
    assert not embedding.alive and reranker.alive
    assert "embedding" not in manager.ready and "reranker" in manager.ready
    assert settings.model_dump() == saved and model.read_bytes() == data
    await manager.close()


def test_int8_worker_stays_on_selected_gpu_and_batches_rerank_fragments(monkeypatch, tmp_path):
    torch = pytest.importorskip("torch")
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", "1")
    for name, function in {
        "is_available": lambda: True,
        "mem_get_info": lambda device: (6 * 2**30, 12 * 2**30),
        "get_device_name": lambda device: "fixture GPU",
        "memory_allocated": lambda device: 1024,
        "memory_reserved": lambda device: 2048,
        "max_memory_allocated": lambda device: 4096,
        "max_memory_reserved": lambda device: 8192,
        "reset_peak_memory_stats": lambda device: None,
    }.items():
        monkeypatch.setattr(torch.cuda, name, function)
    monkeypatch.setattr("tavern.search.local_models.find_spec", lambda name: object())
    loaded, forwarded, batches = [], [], []

    class Inputs(dict):
        def to(self, device):
            assert device == "cuda:1"
            return self

    class Tokenizer:
        def convert_tokens_to_ids(self, token):
            return {"no": 0, "yes": 1}[token]

        def __call__(self, texts, **kwargs):
            batches.append(texts)
            return Inputs(values=torch.tensor([[0.0, 2.0] if "positive" in t else [2.0, 0.0] for t in texts]))

    class Model:
        def eval(self):
            return self

        def to(self, device):
            raise AssertionError("INT8 must load directly on the selected GPU")

        def __call__(self, values, **kwargs):
            forwarded.append(kwargs)
            return SimpleNamespace(logits=values[:, None, :])

    def load(path, **kwargs):
        loaded.append(kwargs)
        return Model()

    factory = SimpleNamespace(from_pretrained=load)
    monkeypatch.setitem(
        sys.modules,
        "transformers",
        SimpleNamespace(
            AutoModel=factory,
            AutoModelForCausalLM=factory,
            AutoTokenizer=SimpleNamespace(from_pretrained=lambda *args, **kwargs: Tokenizer()),
            BitsAndBytesConfig=lambda **kwargs: kwargs,
        ),
    )
    messages = []
    request = {
        "action": "rerank",
        "data": [["question", ["negative", "positive", "negative"]]],
        "instruction_prompt": "retrieve",
        "reranker_prompt": "judge",
        "rerank_fragments": 3,
    }
    config = {
        "model": "Qwen/Qwen3-Reranker-4B",
        "revision": "main",
        "device": "cuda:1",
        "quantization": "int8",
        "batch_size": 1,
    }
    requests = iter([{"action": "load", "config": config}, request])

    def receive():
        try:
            return next(requests)
        except StopIteration:
            raise EOFError from None

    pipe = SimpleNamespace(recv=receive, send=messages.append, close=lambda: None)
    _serve(pipe, tmp_path, "reranker")
    assert not any("error" in message for message in messages), messages
    assert loaded[0]["device_map"] == {"": "cuda:1"}
    assert loaded[0]["local_files_only"] is True
    assert loaded[0]["quantization_config"]["llm_int8_enable_fp32_cpu_offload"] is False
    assert loaded[0]["quantization_config"]["llm_int8_threshold"] == 6.0
    assert loaded[0]["quantization_config"]["llm_int8_skip_modules"] == ["lm_head"]
    assert list(map(len, batches)) == [1, 1, 1]
    assert all(call == {"use_cache": False, "logits_to_keep": 1} for call in forwarded)
    assert messages[-1]["result"] == pytest.approx([0.880797], abs=1e-6)
