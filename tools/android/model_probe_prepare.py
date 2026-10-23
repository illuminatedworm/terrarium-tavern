"""Prepare pinned probe binaries and Q8 models; leave the desktop model cache unchanged."""

import argparse
import importlib.metadata
import json
import os
import shutil
import sys
import tarfile
import zipfile
from pathlib import Path

from dev import run
from model_reference import CACHE, ROOT, cached_checkpoint, sources
from recover_native import download, sha256, verify, write_json


def prepare(cache: Path, models_root: Path):
    pins = sources()
    engine = pins["engine"]
    for target in ("windows", "android"):
        source = engine[target]
        filename = source["url"].rsplit("/", 1)[1]
        archive = download(source["url"], cache / "downloads" / filename, source["sha256"])
        destination = cache / target
        destination.mkdir(parents=True, exist_ok=True)
        if target == "android":
            with tarfile.open(archive) as bundle:
                bundle.extractall(destination, filter="data")
        else:
            with zipfile.ZipFile(archive) as bundle:
                bundle.extractall(destination)

    embedding = pins["embedding"]
    url = f"https://huggingface.co/{embedding['repository']}/resolve/{embedding['revision']}/{embedding['filename']}"
    download(url, cache / "models" / embedding["filename"], embedding["sha256"])

    receipt_path = cache / "reranker-conversion.json"
    if receipt_path.exists():
        receipt = json.loads(receipt_path.read_text("utf-8"))
        if (
            receipt["source"] != pins["reference"]["reranker"]
            or receipt["converter_commit"] != engine["commit"]
            or receipt["outtype"] != "q8_0"
            or receipt["sha256"] != pins["reranker"]["sha256"]
        ):
            raise ValueError("已有转换记录与固定来源不同，停止复用")
        verify(cache / "models" / receipt["filename"], receipt["sha256"])
        print("已校验并复用 Q8 重排模型", flush=True)
        return

    checkout = cache / "llama.cpp"
    if not checkout.exists():
        run(
            ["git", "clone", "--depth", "1", "--branch", engine["tag"], engine["project"], checkout],
            timeout=120,
        )
    if run(["git", "-C", checkout, "rev-parse", "HEAD"]) != engine["commit"]:
        raise ValueError("llama.cpp 源码不是固定提交")
    run(["git", "-C", checkout, "diff", "--exit-code", "HEAD"])

    checkpoint = cached_checkpoint(models_root, "reranker")
    # Upstream identifies this architecture by its directory/model name. The project's
    # hashed cache directory lacks that hint, so copy into an explicitly named staging folder.
    staged = cache / "hf/Qwen3-Reranker-0.6B"
    staged.mkdir(parents=True, exist_ok=True)
    for name in (
        "model.safetensors",
        "config.json",
        "tokenizer_config.json",
        "tokenizer.json",
        "merges.txt",
        "vocab.json",
    ):
        shutil.copyfile(checkpoint / name, staged / name)

    dependencies = cache / "python"
    installed = {
        dist.metadata["Name"]: dist.version
        for dist in importlib.metadata.distributions(path=[str(dependencies)])
    }
    if installed.get("sentencepiece") != "0.2.1":
        run(
            [
                "uv",
                "pip",
                "install",
                "--python",
                sys.executable,
                "--target",
                dependencies,
                "--no-deps",
                "--index-url",
                "https://pypi.org/simple",
                "sentencepiece==0.2.1",
            ],
            timeout=120,
        )
    environment = os.environ.copy()
    environment.update(
        PYTHONPATH=str(dependencies),
        OMP_NUM_THREADS="2",
        MKL_NUM_THREADS="2",
        HF_HUB_OFFLINE="1",
        TRANSFORMERS_OFFLINE="1",
        PYTHONUTF8="1",
    )
    output = cache / "models/Qwen3-Reranker-0.6B-Q8_0.gguf"
    run(
        [
            sys.executable,
            checkout / "convert_hf_to_gguf.py",
            staged,
            "--outtype",
            "q8_0",
            "--outfile",
            output,
        ],
        env=environment,
        log=cache / "convert-reranker.log",
        timeout=120,
    )
    verify(output, pins["reranker"]["sha256"])
    write_json(
        receipt_path,
        {
            "source": pins["reference"]["reranker"],
            "converter_commit": engine["commit"],
            "outtype": "q8_0",
            "filename": output.name,
            "sha256": sha256(output),
            "bytes": output.stat().st_size,
        },
    )
    print("Q8 重排模型转换完成，来源记录已保存", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", type=Path, default=CACHE)
    parser.add_argument("--models-root", type=Path, default=ROOT / "data/models")
    args = parser.parse_args()
    prepare(args.cache.resolve(), args.models_root.resolve())
