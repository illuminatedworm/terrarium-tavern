"""Isolate the pinned Snapdragon libraries from the separately packaged CPU engine."""

import json
import shutil
import tarfile

from recover_native import download, sha256, verify, write_json

LIBRARIES = (
    "libggml-base.so",
    "libggml.so",
    "libggml-cpu.so",
    "libggml-rpc.so",
    "libggml-opencl.so",
    "libggml-hexagon.so",
    "libllama.so",
    "libllama-common.so",
    "libllama-server-impl.so",
    "libmtmd.so",
)
SKELS = tuple(f"libggml-htp-v{version}.so" for version in (73, 75, 79, 81, 85))
RENAMES = {name: "libtavern_accel_" + name[3:] for name in LIBRARIES}
RENAMES["llama-server"] = "libtavern_accel_server.so"
ADAPTATION = "LIEF 1.0.0: prefix ARM64 filenames, DT_SONAME and internal DT_NEEDED; preserve HTP skels and 16 KiB LOAD alignment; no source patches"


def prepare_acceleration(cache, engine, offline):
    source = engine["android_snapdragon"]
    archive = cache / "model-probe/downloads" / source["url"].rsplit("/", 1)[1]
    if offline and not archive.exists():
        raise ValueError("缺少固定 Snapdragon 原生包，请先联网准备")
    download(source["url"], archive, source["sha256"])
    output = cache / "llama-jni/arm64-v8a"
    receipt_file = cache / "distribution-assets/third-party/llama.cpp/snapdragon.json"
    if receipt_file.exists():
        receipt = json.loads(receipt_file.read_text("utf-8"))
        if (
            receipt["source"] == source
            and receipt["adaptation"] == ADAPTATION
            and set(receipt["files"]) == {*RENAMES.values(), *SKELS}
        ):
            for name, value in receipt["files"].items():
                verify(output / name, value["sha256"])
            print("Snapdragon 原生库已校验", flush=True)
            return
    import lief

    if lief.__version__.split("-")[0] != "1.0.0":
        raise ValueError("ELF 适配需要构建环境中固定的 lief==1.0.0")
    receipt = {"source": source, "adaptation": ADAPTATION, "files": {}}
    with tarfile.open(archive) as bundle:
        for name in (*LIBRARIES, "llama-server", *SKELS):
            member = "llama-" + engine["tag"] + "/" + ("bin/" if name == "llama-server" else "lib/") + name
            target = output / RENAMES.get(name, name)
            with bundle.extractfile(member) as stream, target.open("wb") as destination:
                shutil.copyfileobj(stream, destination)
            original = sha256(target)
            if name not in SKELS:
                binary = lief.ELF.parse(str(target))
                for entry in binary.dynamic_entries:
                    if entry.tag in (lief.ELF.DynamicEntry.TAG.NEEDED, lief.ELF.DynamicEntry.TAG.SONAME):
                        if entry.name in RENAMES:
                            entry.name = RENAMES[entry.name]
                binary.write(str(target))
                rewritten = lief.ELF.parse(str(target))
                if any(
                    s.alignment < 16384 for s in rewritten.segments if s.type == lief.ELF.Segment.TYPE.LOAD
                ):
                    raise ValueError("ELF 重写改变了 16 KiB LOAD 对齐：" + name)
                if any(name in RENAMES for name in rewritten.libraries):
                    raise ValueError("ELF 仍引用未隔离的库：" + name)
            receipt["files"][target.name] = {
                "source_sha256": original,
                "sha256": sha256(target),
                "bytes": target.stat().st_size,
            }
    write_json(receipt_file, receipt)
    print("已准备独立 OpenCL/Hexagon 引擎与逐文件适配记录", flush=True)
