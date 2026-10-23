"""Prepare a pinned, entirely local Android wheelhouse and distribution receipts."""

import argparse
import base64
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from packaging.markers import default_environment
from packaging.requirements import Requirement
from recover_native import (
    ANDROID_TAG,
    APK_PAYLOAD,
    APK_SHA256,
    APK_URL,
    DEFAULT_CACHE,
    PACKAGES,
    ROOT,
    _existing_payload_matches,
    download,
    normalized,
    recover,
    sha256,
    verify,
    wheel_info,
    wheel_record_valid,
    write_json,
)

PYPI_INDEX = "https://pypi.org/simple"
ANDROID_COMPATIBILITY = {
    "min_sdk": 28,
    "abi": "arm64-v8a",
    "supported_memory_page_size_bytes": 4096,
    "supports_16k_memory_pages": False,
    "known_4k_load_alignment": [
        "libfreetype",
        "libxml2",
        "libexslt",
        "libxslt",
        "jiter",
        "kohakuvault",
        "rpds",
    ],
    "note": "The three locally compiled wheels use 16 KiB alignment; the entire APK does not yet support 16 KiB devices.",
}
SUDACHI_LICENSE_BLOB = "261eeb9e9f8b2b4b0d119366dda99c6fd7d35c64"
SUDACHI_LICENSE_URL = (
    "https://api.github.com/repos/WorksApplications/sudachi.rs/git/blobs/" + SUDACHI_LICENSE_BLOB
)
SUDACHI_LICENSE_SHA256 = "c71d239df91726fc519c6eb72d318ec65820627232b2f796219e87dcf35d0ab4"
NATIVE_BUILDS = {"tiktoken": "0.14.0", "sudachipy": "0.6.11", "pydantic-core": "2.41.5"}
SOURCE_BUILDS = {"jieba", "kohakuterrarium"}
PYPI_OVERRIDES = {
    "libcst": "0.3.23",
    "trafilatura": "2.0.0",
    "mypy-extensions": "1.1.0",
    "typing-inspect": "0.9.0",
    "markupsafe": "3.0.4",
    "kohakuvault": "0.8.6",
}
DROP = {
    "bcrypt",
    "cloudpickle",
    "coloredlogs",
    "colorama",
    "ddgs",
    "dulwich",
    "flatbuffers",
    "gitdb",
    "gitpython",
    "greenlet",
    "hf-xet",
    "httptools",
    "humanfriendly",
    "lxml-html-clean",
    "magika",
    "markitdown",
    "model2vec",
    "mpmath",
    "onnxruntime",
    "pandas",
    "playwright",
    "primp",
    "protobuf",
    "pyee",
    "pymupdf",
    "pyreadline3",
    "pywin32",
    "pywinpty",
    "pyyaml-ft",
    "safetensors",
    "smmap",
    "sympy",
    "tokenizers",
    "uvloop",
    "watchfiles",
}


def locked_packages() -> dict:
    document = tomllib.loads((ROOT / "uv.lock").read_text("utf-8"))
    return {normalized(item["name"]): item for item in document["package"]}


def source_record(package: dict) -> dict:
    sdist = package["sdist"]
    return {
        "url": sdist.get("url") or package["source"]["url"],
        "sha256": sdist["hash"].removeprefix("sha256:"),
    }


def pinned_requirements(path: Path) -> dict[str, str]:
    result = {}
    for line in path.read_text("utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith(("#", "--")):
            continue
        requirement = Requirement(line.split(" --hash=", 1)[0])
        versions = [item.version for item in requirement.specifier if item.operator == "=="]
        if len(versions) != 1 or len(requirement.specifier) != 1:
            raise ValueError(f"Android 清单必须是精确版本：{line}")
        result[normalized(requirement.name)] = versions[0]
    return result


def sudachi_license(cache: Path, *, offline: bool) -> Path:
    path = cache / "downloads" / "Sudachi-Apache-2.0.txt"
    if not path.exists():
        if offline:
            raise ValueError("缺少已校验的 Sudachi 许可证；先运行完整 prepare.py，或放入指定缓存文件。")
        with urllib.request.urlopen(SUDACHI_LICENSE_URL, timeout=60) as response:
            blob = json.load(response)
        if blob["sha"] != SUDACHI_LICENSE_BLOB or blob["encoding"] != "base64":
            raise ValueError("Sudachi 许可证 Git blob 不匹配")
        path.parent.mkdir(parents=True, exist_ok=True)
        pending = path.with_name(path.name + ".part")
        pending.write_bytes(base64.b64decode(blob["content"]))
        verify(pending, SUDACHI_LICENSE_SHA256)
        pending.replace(path)
    verify(path, SUDACHI_LICENSE_SHA256)
    return path


def _existing_origin(path: Path, info: dict, digest: str, lock: dict, apk_archive) -> dict:
    """Import the first prototype's evidence without pretending to repeat compilation."""
    name, version = info["name"], info["version"]
    package = lock.get(name, {})
    if package.get("version") == version:
        for wheel in package.get("wheels", []):
            if Path(urlsplit(wheel["url"]).path).name == path.name:
                if digest != wheel["hash"].removeprefix("sha256:"):
                    raise ValueError(f"wheel 与 uv.lock 的哈希不匹配：{path.name}")
                return {"kind": "pypi-wheel", "url": wheel["url"], "verified_by": "uv.lock"}
    with zipfile.ZipFile(path) as wheel:
        notes = [member for member in wheel.namelist() if member.endswith("/ANDROID_SOURCE.txt")]
        if notes:
            key = next(key for key in PACKAGES if normalized(key) == name)
            expected_version, prefixes = PACKAGES[key]
            if expected_version != version or apk_archive is None:
                raise ValueError(f"恢复依赖需要对应的固定 APK：{path.name}")
            dist = info["dist_info"]
            entries = [
                member
                for member in apk_archive.namelist()
                if not member.endswith("/")
                and member not in {dist + "/WHEEL", dist + "/RECORD"}
                and (
                    member.startswith(dist + "/")
                    or any(member.startswith(p) if p.endswith("/") else member == p for p in prefixes)
                )
            ]
            if not _existing_payload_matches(path, apk_archive, entries, dist):
                raise ValueError(f"已有恢复 wheel 与 APK 内容不一致：{path.name}")
            return {"kind": "apk-recovery", "url": APK_URL, "sha256": APK_SHA256, "member": APK_PAYLOAD}
    wheel_record_valid(path)
    if name in NATIVE_BUILDS or name in SOURCE_BUILDS:
        if package.get("version") != version:
            raise ValueError(f"已有源码构建 wheel 与 uv.lock 版本不同：{path.name}")
        return {
            "kind": "existing-source-build",
            **source_record(package),
            "verification": "Existing wheel metadata and RECORD checked; compilation was not repeated.",
        }
    # A receipts-only pass performs no downloads. These few Android overrides have
    # different versions from uv.lock; retain that limitation in the visible receipt.
    expected = PYPI_OVERRIDES.get(name)
    if expected != version:
        raise ValueError(f"已有 wheel 没有锁文件或已知来源：{path.name}")
    return {
        "kind": "existing-pypi-override",
        "release": f"https://pypi.org/pypi/{name}/{version}/json",
        "verification": "Existing wheel metadata and RECORD checked; no network hash lookup in receipts-only mode.",
    }


def _license_member(name: str) -> bool:
    return any(
        part.lower().startswith(("license", "licence", "copying", "notice")) for part in name.split("/")
    )


def _asset_path(root: Path, relative: str) -> Path:
    path = PurePosixPath(relative)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"wheel 内资源路径越界：{relative}")
    return root.joinpath(*path.parts)


def receipts(
    cache: Path, *, records: dict | None = None, offline: bool = False, required: dict | None = None
) -> dict:
    cache = cache.resolve()
    required = required if required is not None else pinned_requirements(cache / "requirements.txt")
    lock = locked_packages()
    wheels = {}
    for path in sorted((cache / "wheels").glob("*.whl")):
        info = wheel_info(path)
        if required.get(info["name"]) == info["version"]:
            if info["name"] in wheels:
                raise ValueError(f"同一依赖有多个候选 wheel：{info['name']}")
            wheels[info["name"]] = (path, info)
    missing = required.keys() - wheels.keys()
    if missing:
        raise ValueError("缺少 Android wheel：" + ", ".join(sorted(missing)))
    assets = cache / "distribution-assets" / "third-party"
    assets.mkdir(parents=True, exist_ok=True)
    manifest = {
        "format": 1,
        "uv_lock_sha256": sha256(ROOT / "uv.lock"),
        "android_compatibility": ANDROID_COMPATIBILITY,
        "packages": [],
    }
    with contextlib.ExitStack() as stack:
        apk_archive = None
        apk_path = cache / "downloads" / "upstream-2.1.7.apk"
        if records is None and apk_path.exists():
            verify(apk_path, APK_SHA256)
            apk = stack.enter_context(zipfile.ZipFile(apk_path))
            apk_archive = stack.enter_context(zipfile.ZipFile(io.BytesIO(apk.read(APK_PAYLOAD))))
        for name, (path, info) in wheels.items():
            digest = sha256(path)
            if records is not None:
                record = records[name]
                if record["sha256"] != digest or record["wheel"] != path.name:
                    raise ValueError(f"准备后 wheel 内容已改变：{path.name}")
                origin = record["source"]
            else:
                origin = _existing_origin(path, info, digest, lock, apk_archive)
            relative = f"{name}-{info['version']}"
            license_files = []
            with zipfile.ZipFile(path) as wheel:
                members = [
                    member
                    for member in wheel.namelist()
                    if not member.endswith("/")
                    and (
                        _license_member(member)
                        or member
                        in {
                            info["dist_info"] + "/METADATA",
                            info["dist_info"] + "/WHEEL",
                            info["dist_info"] + "/ANDROID_SOURCE.txt",
                        }
                    )
                ]
                for member in members:
                    destination = _asset_path(assets, relative + "/" + member)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(wheel.read(member))
                    if _license_member(member):
                        license_files.append(relative + "/" + member)
            manifest["packages"].append(
                {
                    "name": name,
                    "version": info["version"],
                    "wheel": path.name,
                    "sha256": digest,
                    "source": origin,
                    "license_files": license_files,
                }
            )
    license_path = sudachi_license(cache, offline=offline)
    target = assets / f"sudachipy-{required['sudachipy']}" / "LICENSE-Apache-2.0.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(license_path, target)
    sudachi = next(item for item in manifest["packages"] if item["name"] == "sudachipy")
    sudachi["license_files"].append(target.relative_to(assets).as_posix())
    sudachi["supplemental_license"] = {"url": SUDACHI_LICENSE_URL, "sha256": SUDACHI_LICENSE_SHA256}
    for source in sorted((ROOT / "LICENSE").rglob("*")):
        if source.is_file():
            target = assets / "project" / source.relative_to(ROOT / "LICENSE")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
    write_json(assets / "manifest.json", manifest)
    write_json(cache / "wheel-manifest.json", manifest)
    (assets / "SHA256SUMS.txt").write_text(
        "".join(f"{item['sha256']}  {item['wheel']}\n" for item in manifest["packages"]), "utf-8"
    )
    (assets / "README.txt").write_text(
        "Terrarium Tavern Android dependency receipts.\n"
        "METADATA and available license files are copied unchanged from the selected wheels.\n"
        "manifest.json records source evidence and any receipts-only verification limits.\n"
        "The fixed v2.1.7 APK supplies dependency binaries, not the app's KohakuTerrarium source.\n"
        "KohakuTerrarium remains pinned to 2.1.5 / e4fb517939ba07086481251717e888cb74dc19a4.\n"
        "SudachiPy's sdist omits a standalone license; its upstream Apache-2.0 text is included separately.\n"
        "These records describe technical provenance and do not resolve the licensing issues documented by the project.\n",
        "utf-8",
    )
    # Gradle already provides the absolute --find-links directory. No index or ambient
    # pip configuration can replace a pinned native wheel with a same-name remote one.
    lines = ["--no-index", "--only-binary=:all:", "--require-hashes"]
    lines.extend(
        f"{item['name']}=={item['version']} --hash=sha256:{item['sha256']}" for item in manifest["packages"]
    )
    (cache / "requirements.txt").write_text("\n".join(lines) + "\n", "utf-8")
    print(f"已生成 {len(wheels)} 个 wheel 的本地锁定清单与许可证资产：{assets}", flush=True)
    return manifest


def _pip_environment() -> dict:
    # Honor standard networking environment while excluding user/system pip indexes
    # and find-links. These overrides are confined to this subprocess.
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith("PIP_")}
    env["PIP_CONFIG_FILE"] = os.devnull
    return env


def _pip(arguments: list[str], log: Path) -> None:
    with log.open("w", encoding="utf-8") as stream:
        process = subprocess.run(
            [sys.executable, "-m", "pip", *arguments, "--index-url", PYPI_INDEX, "--timeout", "60"],
            env=_pip_environment(),
            stdout=stream,
            stderr=subprocess.STDOUT,
        )
    if process.returncode:
        raise RuntimeError(f"pip 失败（{process.returncode}）；查看 {log}")


def _requirements_plan(cache: Path, native: dict) -> dict:
    output = cache / "desktop-requirements.txt"
    subprocess.run(
        [
            "uv",
            "export",
            "--frozen",
            "--no-dev",
            "--no-emit-project",
            "--no-hashes",
            "--output-file",
            str(output),
        ],
        cwd=ROOT,
        check=True,
        stdout=subprocess.DEVNULL,
    )
    environment = default_environment()
    environment.update(
        {
            "sys_platform": "android",
            "os_name": "posix",
            "platform_system": "Android",
            "platform_machine": "aarch64",
            "python_version": "3.13",
            "python_full_version": "3.13.7",
        }
    )
    selected = {}
    for line in output.read_text("utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        requirement = Requirement(line)
        name = normalized(requirement.name)
        if name in DROP or requirement.marker and not requirement.marker.evaluate(environment):
            continue
        if name == "kohakuterrarium":
            if (
                requirement.url
                != "https://github.com/Kohaku-Lab/KohakuTerrarium/archive/e4fb517939ba07086481251717e888cb74dc19a4.zip"
            ):
                raise ValueError("KohakuTerrarium 固定源码地址已变化；先人工复核 Android 适配")
            selected[name] = "2.1.5"
        else:
            versions = [item.version for item in requirement.specifier if item.operator == "=="]
            if len(versions) != 1 or len(requirement.specifier) != 1:
                raise ValueError(f"uv 导出项没有精确锁定：{line}")
            selected[name] = versions[0]
    selected.update(PYPI_OVERRIDES)
    selected.update({name: value["version"] for name, value in native.items()})
    (cache / "requirements-plan.txt").write_text(
        "".join(f"{name}=={version}\n" for name, version in sorted(selected.items())), "utf-8"
    )
    return selected


def _source_wheels(cache: Path, lock: dict, *, reuse_existing: bool) -> dict:
    record_path = cache / "source-builds.json"
    previous = json.loads(record_path.read_text("utf-8"))["packages"] if record_path.exists() else {}
    records = {}
    for name in sorted(SOURCE_BUILDS):
        package = lock[name]
        version = "0.42.1" if name == "jieba" else "2.1.5"
        if package["version"] != version:
            raise ValueError(f"{name} 的固定版本已变化，请复核构建方法")
        source = source_record(package)
        path = cache / "wheels" / f"{name}-{version}-py3-none-any.whl"
        prior = previous.get(name)
        if prior and prior["source"]["sha256"] == source["sha256"]:
            verify(path, prior["sha256"])
            records[name] = prior
            continue
        if path.exists() and reuse_existing:
            info = wheel_info(path)
            if info["name"] != name or info["version"] != version:
                raise ValueError(f"源码构建 wheel 元数据不匹配：{path.name}")
            wheel_record_valid(path)
            origin = {
                "kind": "existing-source-build",
                **source,
                "verification": "Existing wheel explicitly reused; metadata and RECORD checked, compilation not repeated.",
            }
        else:
            archive_name = Path(urlsplit(source["url"]).path).name
            archive = download(source["url"], cache / "downloads" / archive_name, source["sha256"])
            with tempfile.TemporaryDirectory(prefix="source-wheel-", dir=cache) as stage:
                _pip(
                    ["wheel", "--no-deps", "--no-build-isolation", "--wheel-dir", stage, str(archive)],
                    cache / f"build-{name}.log",
                )
                candidates = list(Path(stage).glob("*.whl"))
                if len(candidates) != 1 or candidates[0].name != path.name:
                    raise ValueError(f"{name} 没有生成预期的纯 Python wheel")
                wheel_record_valid(candidates[0])
                candidates[0].replace(path)
            origin = {"kind": "local-source-build", **source, "build_isolation": False}
        records[name] = {
            "name": name,
            "version": version,
            "wheel": path.name,
            "sha256": sha256(path),
            "source": origin,
        }
        write_json(record_path, {"format": 1, "packages": {**previous, **records}})
    return records


def _expected_pypi_wheel(cache: Path, lock: dict, name: str, version: str, filename: str) -> dict:
    package = lock.get(name, {})
    if package.get("version") == version:
        matches = [
            item for item in package.get("wheels", []) if Path(urlsplit(item["url"]).path).name == filename
        ]
        if matches:
            item = matches[0]
            return {
                "url": item["url"],
                "sha256": item["hash"].removeprefix("sha256:"),
                "verified_by": "uv.lock",
            }
    # Android's explicit version overrides have no matching lock entry. Resolve only
    # that immutable release and verify the selected filename against its official digest.
    url = f"https://pypi.org/pypi/{name}/{version}/json"
    metadata_path = cache / "pypi-metadata" / f"{name}-{version}.json"
    if metadata_path.exists():
        release = json.loads(metadata_path.read_text("utf-8"))
    else:
        with urllib.request.urlopen(url, timeout=60) as response:
            release = json.load(response)
        write_json(metadata_path, release)
    if normalized(release["info"]["name"]) != name or release["info"]["version"] != version:
        raise ValueError(f"PyPI 固定版本元数据不匹配：{name}=={version}")
    matches = [
        item
        for item in release["urls"]
        if item["filename"] == filename and item["packagetype"] == "bdist_wheel"
    ]
    if len(matches) != 1:
        raise ValueError(f"PyPI 固定版本没有唯一的目标 wheel：{filename}")
    item = matches[0]
    return {"url": item["url"], "sha256": item["digests"]["sha256"], "verified_by": url}


def _download_wheels(cache: Path, required: dict, records: dict, lock: dict) -> dict:
    wheelhouse = cache / "wheels"
    pending = {name: version for name, version in required.items() if name not in records}
    for path in sorted(wheelhouse.glob("*.whl")):
        info = wheel_info(path)
        name, version = info["name"], info["version"]
        if pending.get(name) != version:
            continue
        source = _expected_pypi_wheel(cache, lock, name, version, path.name)
        verify(path, source["sha256"])
        records[name] = {
            "name": name,
            "version": version,
            "wheel": path.name,
            "sha256": source["sha256"],
            "source": {"kind": "pypi-wheel", **source},
        }
        del pending[name]
    if not pending:
        return records
    request = cache / "download-requirements.txt"
    request.write_text("".join(f"{name}=={version}\n" for name, version in sorted(pending.items())), "utf-8")
    # Native/rebuilt packages are excluded from this request. --find-links alone
    # would let an index race a local wheel with the same package/version/filename.
    with tempfile.TemporaryDirectory(prefix="download-wheel-", dir=cache) as stage:
        _pip(
            [
                "download",
                "--no-deps",
                "--only-binary=:all:",
                "--platform",
                ANDROID_TAG,
                "--python-version",
                "3.13",
                "--abi",
                "cp313",
                "--implementation",
                "cp",
                "--find-links",
                str(wheelhouse),
                "--dest",
                stage,
                "-r",
                str(request),
            ],
            cache / "download-wheels.log",
        )
        for path in sorted(Path(stage).glob("*.whl")):
            info = wheel_info(path)
            name, version = info["name"], info["version"]
            if pending.get(name) != version:
                raise ValueError(f"pip 返回了未请求的依赖：{path.name}")
            source = _expected_pypi_wheel(cache, lock, name, version, path.name)
            verify(path, source["sha256"])
            destination = wheelhouse / path.name
            if destination.exists():
                verify(destination, source["sha256"])
            temporary = destination.with_name(destination.name + ".part")
            shutil.copyfile(path, temporary)
            temporary.replace(destination)
            records[name] = {
                "name": name,
                "version": version,
                "wheel": path.name,
                "sha256": source["sha256"],
                "source": {"kind": "pypi-wheel", **source},
            }
            del pending[name]
    if pending:
        raise ValueError("pip 未提供完整的 Android wheel 清单：" + ", ".join(sorted(pending)))
    return records


def prepare(
    cache: Path, *, ndk: Path | None, jobs: int, reuse_existing: bool, prefer_pypi_native: bool
) -> None:
    from build_wheels import build, python_version

    python_version()
    cache = cache.resolve()
    cache.mkdir(parents=True, exist_ok=True)
    alternative = {"markupsafe", "kohakuvault"} if prefer_pypi_native else set()
    records = recover(cache, exclude=alternative)
    records.update(build(cache, ndk=ndk, jobs=jobs, reuse_existing=reuse_existing))
    required = _requirements_plan(cache, records)
    lock = locked_packages()
    records.update(_source_wheels(cache, lock, reuse_existing=reuse_existing))
    records = _download_wheels(cache, required, records, lock)
    sudachi_license(cache, offline=False)
    receipts(cache, records=records, offline=True, required=required)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    parser.add_argument(
        "--receipts-only", action="store_true", help="仅核对已有 wheel 和生成许可证；不下载或编译"
    )
    parser.add_argument("--ndk", type=Path, help="NDK 27.2.12479018 根目录")
    parser.add_argument("--jobs", type=int, default=4, choices=range(1, 65), metavar="1..64")
    parser.add_argument(
        "--reuse-existing-builds", action="store_true", help="显式复用已另行验证的本地编译 wheel"
    )
    parser.add_argument(
        "--prefer-pypi-native",
        action="store_true",
        help="MarkupSafe 3.0.4 与 KohakuVault 0.8.6 使用官方 PyPI Android wheel，其余恢复依赖仍来自固定 APK",
    )
    args = parser.parse_args()
    if args.receipts_only:
        receipts(args.cache, offline=True)
    else:
        prepare(
            args.cache,
            ndk=args.ndk,
            jobs=args.jobs,
            reuse_existing=args.reuse_existing_builds,
            prefer_pypi_native=args.prefer_pypi_native,
        )
