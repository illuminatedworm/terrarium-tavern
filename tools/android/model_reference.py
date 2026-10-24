"""Produce a small BF16 reference using the existing desktop retrieval implementation."""

import argparse
import gc
import hashlib
import json
import sys
from pathlib import Path

from recover_native import DEFAULT_CACHE, ROOT

CACHE = DEFAULT_CACHE / "model-probe"
QUERY = "北岸港口的守卫住在哪里？"
DOCUMENTS = [
    "北岸港口的守卫阿舟住在灯塔二层，每晚巡视码头。",
    "南山茶园的采茶季从四月开始，雨后需要及时晾晒茶叶。",
    "北岸港口的铁匠铺靠近东门，铁匠白天修理船锚。",
]


def sources():
    return json.loads((ROOT / "src/tavern/android/model_sources.json").read_text("utf-8"))


def cached_checkpoint(root, purpose):
    source = sources()["reference"][purpose]
    key = hashlib.sha256(json.dumps([source["repository"], "main"]).encode()).hexdigest()
    checkpoint = root / key
    revision = json.loads((checkpoint / "tavern-ready.json").read_text("utf-8"))["revision"]
    if revision != source["revision"]:
        raise ValueError(f"{purpose} 本地源版本与探针固定版本不同，停止比较")
    with (checkpoint / "model.safetensors").open("rb") as stream:
        actual = hashlib.file_digest(stream, "sha256").hexdigest()
    if actual != source["weights_sha256"]:
        raise ValueError(f"{purpose} 源权重校验失败，原文件保持不变")
    return checkpoint


def inputs():
    sys.path.insert(0, str(ROOT / "src"))
    from tavern.core.prompts import DEFAULT_LOCAL_INSTRUCTION, DEFAULT_RERANKER_PROMPT

    query = f"Instruct: {DEFAULT_LOCAL_INSTRUCTION}\nQuery: {QUERY}"
    prefix = f"<|im_start|>system\n{DEFAULT_RERANKER_PROMPT}<|im_end|>\n<|im_start|>user\n"
    suffix = "<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n"
    return {
        "instruction": DEFAULT_LOCAL_INSTRUCTION,
        "reranker_prompt": DEFAULT_RERANKER_PROMPT,
        "query": QUERY,
        "documents": DOCUMENTS,
        "embedding_texts": [query, *DOCUMENTS],
        "reranker_texts": [
            prefix + f"<Instruct>: {DEFAULT_LOCAL_INSTRUCTION}\n<Query>: {QUERY}\n<Document>: {text}" + suffix
            for text in DOCUMENTS
        ],
    }


def reference(models_root):
    import torch
    from transformers import AutoModel, AutoModelForCausalLM, AutoTokenizer

    sys.path.insert(0, str(ROOT / "src"))
    from tavern.search.local_models import _infer

    if not torch.cuda.is_available() or torch.cuda.mem_get_info()[0] < 2 * 1024**3:
        raise RuntimeError("BF16 对照需要至少 2 GiB 空闲 CUDA 显存")
    torch.set_num_threads(2)
    data = inputs()
    report = {"inputs": data, "sources": sources()["reference"], "quantization": "BF16"}
    for purpose in ("embedding", "reranker"):
        checkpoint = cached_checkpoint(models_root, purpose)
        tokenizer = AutoTokenizer.from_pretrained(checkpoint, local_files_only=True, padding_side="left")
        model_type = AutoModel if purpose == "embedding" else AutoModelForCausalLM
        model = model_type.from_pretrained(checkpoint, local_files_only=True, dtype=torch.bfloat16).to(
            "cuda:0"
        )
        model.eval()
        config = {"device": "cuda:0", "batch_size": 1, "dimensions": 1024}
        request = {"instruction_prompt": data["instruction"], "reranker_prompt": data["reranker_prompt"]}
        try:
            texts = data[purpose + "_texts"]
            tokens = [tokenizer.encode(text, add_special_tokens=True) for text in texts]
            if max(map(len, tokens)) > 512:
                raise ValueError("探针样本超过 512 tokens；不会截断输入")
            if purpose == "embedding":
                query = _infer(
                    torch,
                    tokenizer,
                    model,
                    config,
                    {**request, "action": "embed", "query": True, "data": [QUERY]},
                )
                documents = _infer(
                    torch, tokenizer, model, config, {**request, "action": "embed", "data": DOCUMENTS}
                )
                values = [*query, *documents]
            else:
                values = _infer(
                    torch,
                    tokenizer,
                    model,
                    config,
                    {
                        **request,
                        "action": "rerank",
                        "data": [[QUERY, [text]] for text in DOCUMENTS],
                        "rerank_fragments": 1,
                    },
                )
            report[purpose] = {"tokens": tokens, "values": values}
            print(f"BF16 {purpose} 对照完成，tokens={list(map(len, tokens))}", flush=True)
        finally:
            del model, tokenizer
            gc.collect()
            torch.cuda.empty_cache()
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models-root", type=Path, default=ROOT / "data/models")
    parser.add_argument("--output", type=Path, default=CACHE / "reference.json")
    args = parser.parse_args()
    result = reference(args.models_root)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), "utf-8")
