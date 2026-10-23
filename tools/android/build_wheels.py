"""Cross-compile the three missing CPython 3.13 Android wheels from locked sources."""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import zipfile
from pathlib import Path
from urllib.parse import urlsplit

from recover_native import (
    DEFAULT_CACHE,
    ROOT,
    download,
    normalized,
    sha256,
    verify,
    wheel_info,
    wheel_record_valid,
    write_json,
)

VERSIONS = {"tiktoken": "0.14.0", "sudachipy": "0.6.11", "pydantic-core": "2.41.5"}
NDK_VERSION = "27.2.12479018"
RUST_VERSION = "1.99.1"
PREFIX_URL = "https://repo.maven.apache.org/maven2/com/chaquo/python/python/3.13.7/python-3.13.7-aarch64-linux-android.tar.gz"
PREFIX_SHA256 = "0d889ce4237b0e37f3c8b45e6094aec693bb5faaf5cbdfb7167e16fedbc1a40e"
SUDACHI_LOADER = "sudachi-lib/src/plugin/loader.rs"
SUDACHI_BEFORE = '#[cfg(any(target_os = "linux", target_os = "freebsd"))]'
SUDACHI_AFTER = '#[cfg(any(target_os = "linux", target_os = "freebsd", target_os = "android"))]'


def python_version() -> None:
    if sys.version_info[:2] != (3, 13):
        raise ValueError("Android 构建工具必须由 Python 3.13 执行")


def _maturin_config(original: str, name: str) -> str:
    if name == "tiktoken":
        body, count = re.subn(
            r"\[build-system\][\s\S]*?(?=\n\[)",
            '[build-system]\nrequires = ["maturin>=1.13"]\nbuild-backend = "maturin"\n',
            original,
            count=1,
        )
        if count != 1 or "[tool.maturin]" in original:
            raise ValueError("tiktoken 上游构建文件已变化，不能套用 Android 补丁")
        return (
            body
            + '\n[tool.maturin]\npython-source = "."\nmodule-name = "tiktoken._tiktoken"\nfeatures = ["python"]\npython-packages = ["tiktoken", "tiktoken_ext"]\n'
        )
    if name == "sudachipy":
        if tomllib.loads(original)["build-system"]["requires"] != ["setuptools", "wheel", "setuptools-rust"]:
            raise ValueError("SudachiPy 上游构建文件已变化，不能套用 Android 补丁")
        return """[build-system]
requires = ["maturin>=1.13"]
build-backend = "maturin"

[project]
name = "SudachiPy"
version = "0.6.11"
description = "Sudachi Japanese morphological analyzer"
requires-python = ">=3.9"
license = {text = "Apache-2.0"}

[project.scripts]
sudachipy = "sudachipy.command_line:main"

[tool.maturin]
python-source = "py_src"
module-name = "sudachipy.sudachipy"
include = ["py_src/sudachipy/resources/**", "LICENSE*", "NOTICE*"]
"""
    return original


def _unpack_source(archive: Path, destination: Path, name: str) -> tuple[Path, list[dict]]:
    """Only the documented patches may differ from the hash-verified upstream sdist."""
    with tarfile.open(archive) as source:
        roots = {member.name.split("/", 1)[0] for member in source.getmembers()}
        if len(roots) != 1:
            raise ValueError(f"源码归档需要单一顶层目录：{archive.name}")
        top = roots.pop()
        target = destination / top
        if not target.exists():
            destination.mkdir(parents=True, exist_ok=True)
            source.extractall(destination, filter="data")
        patched = {}
        original_config = source.extractfile(top + "/pyproject.toml").read().decode("utf-8")
        if name != "pydantic-core":
            patched["pyproject.toml"] = _maturin_config(original_config, name).encode()
        if name == "sudachipy":
            original = source.extractfile(top + "/" + SUDACHI_LOADER).read().decode("utf-8")
            if original.count(SUDACHI_BEFORE) != 1 or SUDACHI_AFTER in original:
                raise ValueError("Sudachi loader.rs 的 Linux/FreeBSD 条件不符合已核对补丁")
            patched[SUDACHI_LOADER] = original.replace(SUDACHI_BEFORE, SUDACHI_AFTER).encode()
        lock_path = Path(__file__).parent / "native-locks" / f"{name}-Cargo.lock"
        if lock_path.exists():
            patched["Cargo.lock"] = lock_path.read_bytes()
        changes = []
        seen = set()
        for member in source.getmembers():
            if not member.isfile():
                continue
            relative = member.name.removeprefix(top + "/")
            seen.add(relative)
            upstream = source.extractfile(member).read()
            current = target / relative
            desired = patched.get(relative, upstream)
            actual = current.read_bytes()
            # The trial scripts wrote platform line endings for their text-only patches.
            acceptable = {upstream, desired}
            if relative in patched:
                acceptable |= {value.replace(b"\n", b"\r\n") for value in acceptable}
            if actual not in acceptable:
                raise ValueError(f"源码缓存存在未声明的修改，未覆盖：{current}")
            if desired != upstream:
                current.write_bytes(desired)
                changes.append(
                    {
                        "path": relative,
                        "before_sha256": hashlib.sha256(upstream).hexdigest(),
                        "after_sha256": hashlib.sha256(desired).hexdigest(),
                    }
                )
        for relative, data in patched.items():
            if relative not in seen:
                current = target / relative
                if current.exists() and current.read_bytes() != data:
                    raise ValueError(f"缓存中新增文件与固定内容不一致：{current}")
                current.write_bytes(data)
                changes.append(
                    {
                        "path": relative,
                        "before_sha256": None,
                        "after_sha256": hashlib.sha256(data).hexdigest(),
                    }
                )
    return target, changes


def _prefix(cache: Path) -> Path:
    archive = download(
        PREFIX_URL, cache / "downloads" / "python-3.13.7-aarch64-linux-android.tar.gz", PREFIX_SHA256
    )
    target = cache / "python-prefix"
    if not target.exists():
        target.mkdir(parents=True)
        with tarfile.open(archive) as source:
            source.extractall(target, filter="data")
    # Validate the linker library against the pinned archive even for a pre-existing prefix.
    with tarfile.open(archive) as source:
        for member in source.getmembers():
            if member.isfile():
                current = target / member.name
                expected = hashlib.sha256(source.extractfile(member).read()).hexdigest()
                verify(current, expected)
    return target / "prefix" / "lib"


def _environment(cache: Path, ndk: Path, prefix: Path, jobs: int) -> dict:
    if (
        not (ndk / "source.properties").is_file()
        or f"Pkg.Revision = {NDK_VERSION}" not in (ndk / "source.properties").read_text()
    ):
        raise ValueError(f"请先安装 Android NDK {NDK_VERSION}，并用 --ndk 指向该目录")
    host = "windows-x86_64" if os.name == "nt" else "linux-x86_64"
    compiler = ndk / "toolchains" / "llvm" / "prebuilt" / host / "bin"
    suffix = ".cmd" if os.name == "nt" else ""
    executable = ".exe" if os.name == "nt" else ""
    linker = compiler / ("aarch64-linux-android24-clang" + suffix)
    archive_tool = compiler / ("llvm-ar" + executable)
    if not linker.is_file() or not archive_tool.is_file():
        raise ValueError(f"缺少当前宿主的 NDK LLVM 工具：{compiler}")
    env = os.environ.copy()
    cargo = Path(env.get("CARGO_HOME", str(cache / "cargo")))
    rustup = Path(env.get("RUSTUP_HOME", str(cache / "rustup")))
    env.update(
        {
            "CARGO_HOME": str(cargo),
            "RUSTUP_HOME": str(rustup),
            "RUSTUP_TOOLCHAIN": env.get("RUSTUP_TOOLCHAIN", RUST_VERSION),
            "RUSTUP_AUTO_INSTALL": "0",
            "CARGO_BUILD_JOBS": str(jobs),
            "ANDROID_API_VERSION": "24",
            "PYO3_CROSS_LIB_DIR": str(prefix),
            "PYO3_CROSS_PYTHON_VERSION": "3.13",
            "CARGO_TARGET_AARCH64_LINUX_ANDROID_LINKER": str(linker),
            "CC_aarch64-linux-android": str(linker),
            "AR_aarch64-linux-android": str(archive_tool),
            # Cargo's encoded form preserves paths containing spaces. The linker option
            # aligns these three new libraries; it does not certify the entire APK for 16 KiB pages.
            "CARGO_ENCODED_RUSTFLAGS": "\x1f".join(
                ["-L", f"native={prefix}", "-C", "link-arg=-Wl,-z,max-page-size=16384"]
            ),
            "PATH": str(cargo / "bin") + os.pathsep + str(compiler) + os.pathsep + env.get("PATH", ""),
        }
    )
    proxy = env.get("HTTPS_PROXY") or env.get("HTTP_PROXY") or env.get("https_proxy") or env.get("http_proxy")
    if proxy:
        env["CARGO_HTTP_PROXY"] = proxy
    rustc = cargo / "bin" / ("rustc" + executable)
    version = subprocess.check_output([str(rustc), "--version"], env=env, text=True).strip()
    if not version.startswith("rustc " + RUST_VERSION + " "):
        raise ValueError(f"需要 Rust {RUST_VERSION}，实际为 {version}")
    return env


def validate_payload(path: Path, name: str) -> None:
    wheel_record_valid(path)
    if name == "tiktoken":
        # The Rust extension can compile successfully while its sibling namespace
        # package is omitted. registry.py imports it even when using bundled BPE ranks.
        with zipfile.ZipFile(path) as wheel:
            if "tiktoken_ext/openai_public.py" not in wheel.namelist():
                raise ValueError("tiktoken wheel 缺少必需的 tiktoken_ext 插件包，不能用于 APK")


def build(
    cache: Path,
    *,
    ndk: Path | None = None,
    jobs: int = 4,
    reuse_existing: bool = False,
    packages: list[str] | None = None,
) -> dict:
    python_version()
    cache = cache.resolve()
    wheelhouse = cache / "wheels"
    wheelhouse.mkdir(parents=True, exist_ok=True)
    lock = {normalized(p["name"]): p for p in tomllib.loads((ROOT / "uv.lock").read_text("utf-8"))["package"]}
    record_path = cache / "native-builds.json"
    previous = json.loads(record_path.read_text("utf-8"))["packages"] if record_path.exists() else {}
    output = {}
    env = None
    for name, version in VERSIONS.items():
        if packages and name not in packages:
            continue
        package = lock[name]
        if package["version"] != version:
            raise ValueError(f"uv.lock 的 {name} 已变化；先复核 Android 补丁与固定 Rust 锁文件")
        source_info = {
            "url": package["sdist"]["url"],
            "sha256": package["sdist"]["hash"].removeprefix("sha256:"),
        }
        path = wheelhouse / f"{name.replace('-', '_')}-{version}-cp313-cp313-android_24_arm64_v8a.whl"
        prior = previous.get(name)
        recipe = sha256(Path(__file__))
        rust_lock = Path(__file__).parent / "native-locks" / f"{name}-Cargo.lock"
        rust_lock_hash = sha256(rust_lock) if rust_lock.exists() else None
        if (
            prior
            and prior.get("recipe_sha256") == recipe
            and prior.get("rust_lock_sha256") == rust_lock_hash
            and prior["source"]["sha256"] == source_info["sha256"]
        ):
            verify(path, prior["sha256"])
            validate_payload(path, name)
            output[name] = prior
            print(f"复用已校验原生 wheel：{path.name}", flush=True)
            continue
        if path.exists() and reuse_existing:
            info = wheel_info(path)
            if info["name"] != name or info["version"] != version:
                raise ValueError(f"已有 wheel 元数据不匹配：{path}")
            validate_payload(path, name)
            output[name] = {
                "name": name,
                "version": version,
                "wheel": path.name,
                "sha256": sha256(path),
                "source": {
                    "kind": "existing-source-build",
                    **source_info,
                    "verification": "Existing locally compiled wheel explicitly reused; metadata and RECORD checked, compilation not repeated.",
                },
            }
            print(f"记录已有本地编译产物（不重编译）：{path.name}", flush=True)
            continue
        archive = download(
            source_info["url"],
            cache / "downloads" / Path(urlsplit(source_info["url"]).path).name,
            source_info["sha256"],
        )
        source, changes = _unpack_source(archive, cache / "native-sources", name)
        if env is None:
            prefix = _prefix(cache)
            env = _environment(cache, (ndk or cache / "sdk" / "ndk" / NDK_VERSION).resolve(), prefix, jobs)
        log = cache / f"build-{name}.log"
        with tempfile.TemporaryDirectory(prefix="native-build-", dir=cache) as stage:
            command = [
                sys.executable,
                "-m",
                "maturin",
                "build",
                "--release",
                "--locked",
                "--target",
                "aarch64-linux-android",
                "--interpreter",
                sys.executable,
                "--out",
                stage,
                "--compatibility",
                "off",
            ]
            print(f"编译 {name}；日志：{log}", flush=True)
            with log.open("w", encoding="utf-8") as stream:
                process = subprocess.run(
                    command, cwd=source, env=env, stdout=stream, stderr=subprocess.STDOUT
                )
            if process.returncode:
                raise RuntimeError(f"{name} 编译失败（{process.returncode}）；查看 {log}")
            produced = list(Path(stage).glob("*.whl"))
            if len(produced) != 1 or produced[0].name != path.name:
                raise ValueError(f"{name} 生成的 wheel 标签不符合 cp313 Android ARM64")
            validate_payload(produced[0], name)
            produced[0].replace(path)
        origin = {
            "kind": "local-native-build",
            **source_info,
            "rust": RUST_VERSION,
            "ndk": NDK_VERSION,
            "python_sysroot": {"url": PREFIX_URL, "sha256": PREFIX_SHA256},
            "linker_max_page_size": 16384,
            "patches": changes,
        }
        output[name] = {
            "name": name,
            "version": version,
            "wheel": path.name,
            "sha256": sha256(path),
            "source": origin,
            "recipe_sha256": recipe,
            "rust_lock_sha256": rust_lock_hash,
        }
        write_json(source / "ANDROID_PATCHES.json", origin)
        write_json(record_path, {"format": 1, "packages": {**previous, **output}})
    write_json(record_path, {"format": 1, "packages": {**previous, **output}})
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--ndk", type=Path, help=f"NDK {NDK_VERSION} 根目录")
    parser.add_argument("--jobs", type=int, default=4, choices=range(1, 65), metavar="1..64")
    parser.add_argument("--package", action="append", choices=VERSIONS, help="仅重建指定扩展，可重复指定")
    parser.add_argument(
        "--reuse-existing",
        action="store_true",
        help="显式复用已另行验证的三个本地编译 wheel，仅检查元数据与 RECORD",
    )
    args = parser.parse_args()
    build(
        args.cache,
        ndk=args.ndk,
        jobs=args.jobs,
        reuse_existing=args.reuse_existing,
        packages=args.package,
    )
