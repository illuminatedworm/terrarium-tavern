"""Package isolated pinned CPU and Snapdragon helpers; weights never enter the APK."""

import argparse
import json
import shutil
import tarfile
from pathlib import Path

from dev import run
from model_reference import ROOT, sources
from recover_native import download, sha256, verify, write_json

LIBRARIES = (
    "libggml-base.so",
    "libggml.so",
    "libllama.so",
    "libmtmd.so",
    "libllama-common.so",
    "libllama-server-impl.so",
    "libggml-cpu-android_armv8.0_1.so",
    "libggml-cpu-android_armv8.2_1.so",
    "libggml-cpu-android_armv8.2_2.so",
    "libggml-cpu-android_armv8.6_1.so",
    "libggml-cpu-android_armv9.0_1.so",
    "libggml-cpu-android_armv9.2_1.so",
    "libggml-cpu-android_armv9.2_2.so",
)


def prepare_cpu(cache: Path, offline: bool):
    engine = sources()["engine"]
    source = engine["android"]
    archive = cache / "model-probe/downloads" / source["url"].rsplit("/", 1)[1]
    if offline and not archive.exists():
        raise ValueError("缺少固定原生包，请先运行 tools/android/prepare_llama.py 准备")
    download(source["url"], archive, source["sha256"])
    output = cache / "llama-jni/arm64-v8a"
    assets = cache / "distribution-assets/third-party/llama.cpp"
    assets.mkdir(parents=True, exist_ok=True)
    for name, license in engine["licenses"].items():
        target = assets / ("LICENSE-" + name)
        checkout_file = cache / "model-probe/llama.cpp" / license["path"]
        if target.exists():
            verify(target, license["sha256"])
        elif checkout_file.exists():
            verify(checkout_file, license["sha256"])
            shutil.copyfile(checkout_file, target)
        elif offline:
            raise ValueError("缺少原生库许可文件，请先运行 tools/android/prepare_llama.py")
        else:
            url = f"https://raw.githubusercontent.com/ggml-org/llama.cpp/{engine['commit']}/{license['path']}"
            download(url, target, license["sha256"])
    receipt_file = assets / "source.json"
    if receipt_file.exists():
        receipt = json.loads(receipt_file.read_text("utf-8"))
        if receipt["engine"] == engine and set(receipt["files"]) == {*LIBRARIES, "libtavern_llama_server.so"}:
            for name, value in receipt["files"].items():
                verify(output / name, value["sha256"])
            print("llama.cpp 原生库已校验", flush=True)
            return
    output.mkdir(parents=True, exist_ok=True)
    assets.mkdir(parents=True, exist_ok=True)
    strip = cache / "sdk/ndk/27.2.12479018/toolchains/llvm/prebuilt/windows-x86_64/bin/llvm-strip.exe"
    receipt = {
        "engine": engine,
        "adaptation": "llama-server renamed as libtavern_llama_server.so; llvm-strip --strip-unneeded; no source patches",
        "files": {},
    }
    with tarfile.open(archive) as bundle:
        prefix = "llama-" + engine["tag"] + "/"
        for name in (*LIBRARIES, "llama-server"):
            target = output / ("libtavern_llama_server.so" if name == "llama-server" else name)
            with bundle.extractfile(prefix + name) as stream, target.open("wb") as destination:
                shutil.copyfileobj(stream, destination)
            original = sha256(target)
            run([strip, "--strip-unneeded", target])
            receipt["files"][target.name] = {
                "source_sha256": original,
                "sha256": sha256(target),
                "bytes": target.stat().st_size,
            }
        with bundle.extractfile(prefix + "LICENSE") as stream:
            (assets / "LICENSE").write_bytes(stream.read())
    write_json(receipt_file, receipt)
    print("已准备 llama.cpp 原生库与许可记录（不含模型权重）", flush=True)


def prepare(cache: Path, offline: bool):
    from prepare_acceleration import prepare_acceleration

    prepare_cpu(cache, offline)
    prepare_acceleration(cache, sources()["engine"], offline)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", type=Path, default=ROOT / ".cache/android")
    parser.add_argument("--offline", action="store_true")
    args = parser.parse_args()
    prepare(args.cache.resolve(), args.offline)
