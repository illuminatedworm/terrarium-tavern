"""Measure pinned Q8 retrieval models on Windows and an ADB-connected ARM64 device."""

import argparse
import hashlib
import json
import math
import os
import platform
import re
import secrets
import shlex
import subprocess
import tarfile
import threading
import time
import urllib.error
import urllib.request
import zipfile
from contextlib import contextmanager
from pathlib import Path

from dev import Device
from model_reference import CACHE, ROOT, inputs, sources
from recover_native import sha256, verify, write_json

REMOTE = "/data/local/tmp/terrarium-model-probe"
CONTEXT = 512
THREADS = 4
LIMITS = {
    "cross_platform_embedding_min_cosine": 0.9999,
    "cross_platform_reranker_max_abs": 0.001,
    "bf16_embedding_min_cosine": 0.995,
    "bf16_reranker_max_abs": 0.02,
}


def assets(cache: Path, pins: dict) -> dict:
    for target in ("windows", "android"):
        archive = cache / "downloads" / pins["engine"][target]["url"].rsplit("/", 1)[1]
        verify(archive, pins["engine"][target]["sha256"])
        # Checking only the archive would still allow stale extracted libraries to run.
        if target == "windows":
            with zipfile.ZipFile(archive) as bundle:
                for name in bundle.namelist():
                    if name.endswith(".dll") or name == "llama-server.exe":
                        with bundle.open(name) as stream:
                            verify(cache / target / name, hashlib.file_digest(stream, "sha256").hexdigest())
        else:
            with tarfile.open(archive) as bundle:
                for member in bundle.getmembers():
                    if member.name.endswith(".so") or member.name.endswith("/llama-server"):
                        with bundle.extractfile(member) as stream:
                            verify(
                                cache / target / member.name,
                                hashlib.file_digest(stream, "sha256").hexdigest(),
                            )
    conversion = json.loads((cache / "reranker-conversion.json").read_text("utf-8"))
    if (
        conversion["source"] != pins["reference"]["reranker"]
        or conversion["converter_commit"] != pins["engine"]["commit"]
        or conversion["outtype"] != "q8_0"
    ):
        raise ValueError("重排 GGUF 的来源记录与固定版本不符")
    models = {
        "embedding": cache / "models" / pins["embedding"]["filename"],
        "reranker": cache / "models" / conversion["filename"],
    }
    verify(models["embedding"], pins["embedding"]["sha256"])
    verify(models["reranker"], conversion["sha256"])
    return models


def deploy(device: Device, cache: Path, models: dict, tag: str) -> str:
    native = cache / "android" / ("llama-" + tag)
    remote_native = REMOTE + "/" + native.name
    device.call("shell", "mkdir", "-p", remote_native, REMOTE + "/models")
    files = [(native / "llama-server", remote_native + "/llama-server")]
    files.extend((path, remote_native + "/" + path.name) for path in sorted(native.glob("*.so")))
    files.extend((path, REMOTE + "/models/" + path.name) for path in models.values())
    # Verify cached transfers, including weights, before reusing them. No personal app data is read.
    remote_paths = " ".join(shlex.quote(remote) for _, remote in files)
    command = f'for f in {remote_paths}; do if [ -f "$f" ]; then sha256sum "$f"; fi; done'
    existing = dict(
        (path, digest)
        for digest, path in re.findall(r"^([a-f0-9]{64})\s+(.+)$", device.call("shell", command), re.M)
    )
    for path, remote in files:
        digest = sha256(path)
        if existing.get(remote) == digest:
            continue
        print("ADB 传输 " + path.name, flush=True)
        device.call("push", str(path), remote, timeout=60)
        actual = device.call("shell", "sha256sum", remote).split()[0]
        if actual != digest:
            raise ValueError("手机文件校验失败：" + remote)
    device.call("shell", "chmod", "755", remote_native + "/llama-server")
    return remote_native


def arguments(model: str, purpose: str, port: int, key: str) -> list[str]:
    # b11829 /embedding reads n_embd_out even for rank pooling, whose buffer has
    # only two classifier values. Declare that output size; weights/prompts stay intact.
    rank_output = ["--override-kv", "qwen3.embedding_length_out=int:2"] if purpose == "reranker" else []
    return [
        "-m",
        model,
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--api-key",
        key,
        "--embedding",
        "--pooling",
        "last" if purpose == "embedding" else "rank",
        "-ngl",
        "0",
        "-c",
        str(CONTEXT),
        "-b",
        str(CONTEXT),
        "-ub",
        str(CONTEXT),
        "-np",
        "1",
        "-t",
        str(THREADS),
        "-tb",
        str(THREADS),
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
        *rank_output,
    ]


class Server:
    def __init__(self, command: list[str], log: Path, key: str, device: Device | None):
        self.key = key
        self.device = device
        self.pid = None
        self.ready = False
        self.event = threading.Event()
        self.log = log
        environment = {key: value for key, value in os.environ.items() if not key.startswith("LLAMA_")}
        self.started = time.perf_counter()
        self.process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=subprocess.CREATE_NO_WINDOW,
            env=environment,
        )
        self.reader = threading.Thread(target=self.read_log, daemon=True)
        self.reader.start()

    def read_log(self):
        try:
            with self.log.open("w", encoding="utf-8") as output:
                for line in self.process.stdout:
                    output.write(line)
                    output.flush()
                    if line.startswith("TAVERN_PROBE_PID="):
                        self.pid = int(line.split("=", 1)[1])
                    if "listening on " in line:
                        self.ready = True
                        self.startup_ms = (time.perf_counter() - self.started) * 1000
                        self.event.set()
        finally:
            self.event.set()

    def wait_ready(self):
        if not self.event.wait(30) or not self.ready:
            raise RuntimeError("模型服务未就绪；查看完整日志：" + str(self.log))

    def signal_remote(self, signal: str):
        if self.pid is None:
            return
        command = f"if [ -r /proc/{self.pid}/cmdline ]; then cat /proc/{self.pid}/cmdline; fi"
        command_line = self.device.call("shell", command)
        if REMOTE + "/" in command_line and self.key in command_line:
            self.device.call("shell", "kill", "-" + signal, str(self.pid))
        elif command_line:
            raise RuntimeError("手机 PID 已不属于本次探针，停止自动清理")

    def close(self):
        try:
            if self.process.poll() is None:
                if self.device:
                    self.signal_remote("TERM")
                else:
                    self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    if self.device:
                        self.signal_remote("KILL")
                    self.process.kill()
                    self.process.wait(timeout=5)
        finally:
            self.reader.join(timeout=5)
            self.process.stdout.close()

    def post(self, path: str, body: dict):
        request = urllib.request.Request(
            self.url + path,
            data=json.dumps(body, ensure_ascii=False).encode(),
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + self.key},
        )
        # Loopback must not be routed through the terminal's download proxy.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(request, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            raise RuntimeError(f"{path} HTTP {error.code}: {error.read().decode('utf-8')}") from error

    def memory(self) -> dict:
        if not self.device:
            return {}
        status = self.device.call("shell", "cat", f"/proc/{self.pid}/status")
        return {
            name: int(re.search(r"^" + field + r":\s+(\d+)", status, re.M)[1]) * 1024
            for name, field in (("peak_rss_bytes", "VmHWM"), ("rss_bytes", "VmRSS"))
        }


@contextmanager
def server(cache: Path, model: Path, purpose: str, device: Device | None, native: str | None):
    key = secrets.token_hex(16)
    port = 18387 if device else 18388
    if device:
        argv = [native + "/llama-server", *arguments(REMOTE + "/models/" + model.name, purpose, port, key)]
        shell = "printf 'TAVERN_PROBE_PID=%s\\n' \"$$\"; exec env "
        shell += "LD_LIBRARY_PATH=" + shlex.quote(native) + " " + shlex.join(argv)
        command = [str(device.adb), "-s", device.serial, "shell", shell]
    else:
        command = [str(cache / "windows/llama-server.exe"), *arguments(str(model), purpose, port, key)]
    target = "android" if device else "windows"
    running = Server(command, cache / f"{target}-{purpose}.log", key, device)
    forwarded = None
    try:
        running.wait_ready()
        if device:
            forwarded = device.call("forward", "tcp:0", "tcp:" + str(port))
            port = int(forwarded)
        running.url = "http://127.0.0.1:" + str(port)
        yield running
    finally:
        try:
            running.close()
        finally:
            if forwarded:
                device.call("forward", "--remove", "tcp:" + forwarded)


def infer(running: Server, purpose: str, text: str) -> tuple[list[float], float]:
    started = time.perf_counter()
    response = running.post(
        "/embedding", {"content": text, "embd_normalize": 2 if purpose == "embedding" else -1}
    )
    elapsed = (time.perf_counter() - started) * 1000
    # Upstream rank pooling returns [P(yes), P(no)], already softmax-normalized.
    values = response[0]["embedding"][0]
    dimensions = 1024 if purpose == "embedding" else 2
    if len(values) != dimensions or not all(math.isfinite(value) for value in values):
        raise ValueError(f"{purpose} 返回维度或有限数值检查失败：{len(values)}")
    return values, elapsed


def measure(running: Server, purpose: str, reference: dict) -> dict:
    texts = reference["inputs"][purpose + "_texts"]
    values, tokens, times = [], [], []
    for index, text in enumerate(texts):
        encoded = running.post("/tokenize", {"content": text, "add_special": True, "parse_special": True})[
            "tokens"
        ]
        if encoded != reference[purpose]["tokens"][index]:
            raise ValueError(f"{purpose} 样本 {index + 1} 与 BF16 分词不同，停止比较")
        if len(encoded) > CONTEXT:
            raise ValueError("样本超过探针上下文，不会截断输入")
        value, elapsed = infer(running, purpose, text)
        tokens.append(encoded)
        values.append(value if purpose == "embedding" else value[0])
        times.append(elapsed)
        print(f"  {purpose} 样本 {index + 1}: {len(encoded)} tokens, {elapsed:.1f} ms", flush=True)
    repeated, elapsed = infer(running, purpose, texts[0])
    repeated = repeated if purpose == "embedding" else repeated[0]
    return {
        "rank_output_dimensions": 2 if purpose == "reranker" else None,
        "startup_ms": running.startup_ms,
        "request_ms": times,
        "tokens": tokens,
        "values": values,
        "repeat_first": {"value": repeated, "request_ms": elapsed},
        **running.memory(),
    }


def cosine(left: list[float], right: list[float]) -> float:
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    return dot / math.sqrt(sum(a * a for a in left) * sum(b * b for b in right))


def compare(result: dict, reference: dict) -> dict:
    comparisons = {}
    for target, baseline_name, baseline in (
        ("android", "windows", result["windows"]),
        ("android", "bf16", reference),
        ("windows", "bf16", reference),
    ):
        actual = result[target]
        similarity = min(
            cosine(a, b)
            for a, b in zip(actual["embedding"]["values"], baseline["embedding"]["values"], strict=True)
        )
        difference = max(
            abs(a - b)
            for a, b in zip(actual["reranker"]["values"], baseline["reranker"]["values"], strict=True)
        )
        prefix = "bf16" if baseline_name == "bf16" else "cross_platform"
        comparisons[target + "_vs_" + baseline_name] = {
            "embedding_min_cosine": similarity,
            "reranker_max_abs": difference,
            "within_reference_range": similarity >= LIMITS[prefix + "_embedding_min_cosine"]
            and difference <= LIMITS[prefix + "_reranker_max_abs"],
        }
    for target in ("android", "windows"):
        vectors = result[target]["embedding"]["values"]
        scores = [cosine(vectors[0], vector) for vector in vectors[1:]]
        rank = result[target]["reranker"]["values"]
        comparisons[target + "_retrieval"] = {
            "embedding_scores": scores,
            "reranker_scores": rank,
            "expected_first": scores.index(max(scores)) == rank.index(max(rank)) == 0,
        }
    return comparisons


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", type=Path, default=CACHE)
    parser.add_argument("--serial")
    parser.add_argument("--resume", action="store_true", help="复用同设备、同来源和同输入的已完成测量")
    args = parser.parse_args()
    cache = args.cache.resolve()
    pins = sources()
    reference = json.loads((cache / "reference.json").read_text("utf-8"))
    if reference["inputs"] != inputs() or reference["sources"] != pins["reference"]:
        raise ValueError("参考输入或模型版本已经变化，请重新生成 BF16 对照")
    models = assets(cache, pins)
    device = Device(ROOT / ".cache/android/sdk/platform-tools/adb.exe", args.serial)
    description = device.describe()
    if "arm64-v8a" not in description["abi"].split(","):
        raise ValueError("固定的 Android 原生包仅支持 arm64-v8a")
    print("准备手机端固定模型与原生库…", flush=True)
    native = deploy(device, cache, models, pins["engine"]["tag"])
    report = {
        "device": description,
        "host": platform.platform(),
        "sources": pins,
        "reranker_conversion": json.loads((cache / "reranker-conversion.json").read_text("utf-8")),
        "configuration": {
            "threads": THREADS,
            "context_tokens": CONTEXT,
            "gpu_layers": 0,
            "quantization": "Q8_0",
            "warmup": False,
            "prompt_cache": False,
        },
        "limits": LIMITS,
        "numerical_comparisons": "Observational only: hardware/framework differences are accepted; numeric tolerance is not a packaging gate.",
        "inputs": reference["inputs"],
        "measurement": "Process startup; filesystem cache may be warm. Request wall time includes HTTP/ADB. Android VmHWM includes mapped weights; excludes APK/Python/WebView.",
    }
    if args.resume:
        previous = json.loads((cache / "report.json").read_text("utf-8"))
        if any(previous[key] != report[key] for key in report):
            raise ValueError("报告的设备、输入、来源或运行参数已改变，不能续测")
        for target in ("windows", "android"):
            report[target] = previous.get(target, {})
    for target, selected_device in (("windows", None), ("android", device)):
        report.setdefault(target, {})
        for purpose, model in models.items():
            if purpose in report[target]:
                print(f"保留已完成的 {target} {purpose} 测量", flush=True)
                continue
            print(f"执行 {target} {purpose}（CPU，{THREADS} 线程）…", flush=True)
            with server(cache, model, purpose, selected_device, native) as running:
                report[target][purpose] = measure(running, purpose, reference)
            write_json(cache / "report.json", report)
    report["comparisons"] = compare(report, reference)
    write_json(cache / "report.json", report)
    print(json.dumps(report["comparisons"], ensure_ascii=False, indent=2), flush=True)
    if any(value.get("expected_first") is False for value in report["comparisons"].values()):
        raise SystemExit("首选文档与固定样本预期不符；完整数据已保存")


if __name__ == "__main__":
    main()
