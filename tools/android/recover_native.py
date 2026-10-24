"""Recover pinned Android dependencies from an upstream APK, preserving their payloads.

This is a dependency source, not an upgrade of Tavern's fixed KohakuTerrarium source.
All network access uses the standard HTTP_PROXY/HTTPS_PROXY environment variables.
"""

import argparse
import base64
import csv
import hashlib
import io
import json
import re
import shutil
import urllib.request
import zipfile
from email.parser import BytesParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
# Persistent Android development assets must survive routine .cache cleanup.
DEFAULT_CACHE = ROOT / "android-dev"
APK_URL = (
    "https://github.com/Kohaku-Lab/KohakuTerrarium/releases/download/v2.1.7/kohakuterrarium-2.1.7-android.apk"
)
APK_SHA256 = "1234727d90444b841e947ddd0a6970d96b57cdc36b97b20e1073b79516996835"
APK_PAYLOAD = "assets/chaquopy/requirements-common.imy"
ANDROID_TAG = "android_24_arm64_v8a"
# Exact package roots in the pinned APK. Never discover arbitrary installed packages.
PACKAGES = {
    "cffi": ("1.17.1", ("cffi/", "_cffi_backend.so")),
    "cryptography": ("42.0.8", ("cryptography/",)),
    "numpy": ("1.26.2", ("numpy/",)),
    "lxml": ("5.3.0", ("lxml/",)),
    "pillow": ("11.0.0", ("PIL/",)),
    "pyyaml": ("6.0.3", ("yaml/",)),
    "regex": ("2024.9.11", ("regex/",)),
    "markupsafe": ("3.0.4", ("markupsafe/",)),
    "jiter": ("0.15.0", ("jiter/",)),
    "rpds_py": ("0.30.0", ("rpds/",)),
    "kohakuvault": ("0.8.6", ("kohakuvault/",)),
    "chaquopy_libcxx": ("180000", ("chaquopy/lib/libc++_shared.so",)),
    "chaquopy_openblas": ("0.2.20", ("chaquopy/lib/libopenblas.so",)),
    "chaquopy_libffi": ("3.3", ("chaquopy/lib/libffi.so",)),
    "chaquopy_libgfortran": ("4.9", ("chaquopy/lib/libgfortran.so.3",)),
    "chaquopy_freetype": ("2.9.1", ("chaquopy/lib/libfreetype.so",)),
    "chaquopy_libjpeg": ("1.5.3", ("chaquopy/lib/libjpeg_chaquopy.so",)),
    "chaquopy_libxml2": ("2.9.8", ("chaquopy/lib/libxml2.so",)),
    "chaquopy_libxslt": ("1.1.32", ("chaquopy/lib/libxslt.so", "chaquopy/lib/libexslt.so")),
    "chaquopy_libyaml": ("0.2.5", ("chaquopy/lib/libyaml-0.so",)),
}


def normalized(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def verify(path: Path, expected: str) -> None:
    if sha256(path) != expected:
        raise ValueError(f"SHA-256 不匹配，保留文件供核对：{path}")


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_name(path.name + ".part")
    pending.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", "utf-8")
    pending.replace(path)


def download(url: str, destination: Path, expected: str) -> Path:
    if destination.exists():
        verify(destination, expected)
        return destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    pending = destination.with_name(destination.name + ".part")
    print(f"下载 {destination.name}", flush=True)
    # urllib's default ProxyHandler consumes the caller's standard proxy environment.
    with urllib.request.urlopen(url, timeout=60) as response, pending.open("wb") as output:
        shutil.copyfileobj(response, output)
    verify(pending, expected)
    pending.replace(destination)
    return destination


def wheel_info(path: Path) -> dict:
    with zipfile.ZipFile(path) as archive:
        candidates = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
        if len(candidates) != 1:
            raise ValueError(f"wheel 需要唯一 METADATA：{path}")
        metadata = BytesParser().parsebytes(archive.read(candidates[0]))
        return {
            "name": normalized(metadata["Name"]),
            "version": metadata["Version"],
            "dist_info": candidates[0].rsplit("/", 1)[0],
        }


def wheel_record_valid(path: Path) -> None:
    """Check locally built/imported wheels before recording their content hash."""
    info = wheel_info(path)
    record = info["dist_info"] + "/RECORD"
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise ValueError(f"wheel 包含重复路径：{path}")
        rows = list(csv.reader(io.StringIO(archive.read(record).decode("utf-8"))))
        if {row[0] for row in rows} != {name for name in names if not name.endswith("/")}:
            raise ValueError(f"wheel RECORD 文件清单不完整：{path}")
        for name, recorded_hash, size in rows:
            if name == record:
                continue
            body = archive.read(name)
            encoded = base64.urlsafe_b64encode(hashlib.sha256(body).digest()).rstrip(b"=").decode()
            if recorded_hash != "sha256=" + encoded or size != str(len(body)):
                raise ValueError(f"wheel RECORD 校验失败：{path.name}: {name}")


def _put(archive: zipfile.ZipFile, name: str, data: bytes) -> None:
    info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o644 << 16
    archive.writestr(info, data)


def _existing_payload_matches(path: Path, source: zipfile.ZipFile, entries: list[str], dist: str) -> bool:
    """Recognize the earlier recovery script's equivalent wheels without rewriting timestamps."""
    wheel_record_valid(path)
    with zipfile.ZipFile(path) as wheel:
        extra = {dist + "/WHEEL", dist + "/RECORD", dist + "/ANDROID_SOURCE.txt"}
        if set(wheel.namelist()) - extra != set(entries):
            return False
        return all(wheel.read(name) == source.read(name) for name in entries)


def recover(cache: Path, *, apk: Path | None = None, exclude: set[str] | None = None) -> dict:
    cache = cache.resolve()
    wheelhouse = cache / "wheels"
    wheelhouse.mkdir(parents=True, exist_ok=True)
    source = apk or cache / "downloads" / "upstream-2.1.7.apk"
    if apk:
        verify(source, APK_SHA256)
    else:
        download(APK_URL, source, APK_SHA256)
    provenance = {"kind": "apk-recovery", "url": APK_URL, "sha256": APK_SHA256, "member": APK_PAYLOAD}
    output = {}
    with (
        zipfile.ZipFile(source) as apk_zip,
        zipfile.ZipFile(io.BytesIO(apk_zip.read(APK_PAYLOAD))) as archive,
    ):
        names = archive.namelist()
        for name, (version, prefixes) in PACKAGES.items():
            canonical = normalized(name)
            if canonical in (exclude or set()):
                continue
            dist = f"{name}-{version}.dist-info"
            metadata = BytesParser().parsebytes(archive.read(dist + "/METADATA"))
            if normalized(metadata["Name"]) != canonical or metadata["Version"] != version:
                raise ValueError(f"固定 APK 的包元数据不匹配：{name}")
            tag = ("py3-none-" if name.startswith("chaquopy_") else "cp313-cp313-") + ANDROID_TAG
            path = wheelhouse / f"{name}-{version}-{tag}.whl"
            replaced = {dist + "/WHEEL", dist + "/RECORD"}
            entries = sorted(
                entry
                for entry in names
                if not entry.endswith("/")
                and entry not in replaced
                and (
                    entry.startswith(dist + "/")
                    or any(
                        entry.startswith(prefix) if prefix.endswith("/") else entry == prefix
                        for prefix in prefixes
                    )
                )
            )
            if path.exists():
                if not _existing_payload_matches(path, archive, entries, dist):
                    raise ValueError(
                        f"已有 wheel 与固定 APK 内容不同，未覆盖：{path}\n"
                        "若是官方 PyPI 的 MarkupSafe/KohakuVault wheel，使用 prepare.py --prefer-pypi-native。"
                    )
            else:
                pending = path.with_name(path.name + ".part")
                records = []
                with zipfile.ZipFile(pending, "w") as wheel:

                    def add(member: str, data: bytes) -> None:
                        _put(wheel, member, data)
                        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
                        records.append((member, "sha256=" + digest, len(data)))

                    for entry in entries:
                        add(entry, archive.read(entry))
                    add(
                        dist + "/WHEEL",
                        f"Wheel-Version: 1.0\nGenerator: terrarium-android-recovery\nRoot-Is-Purelib: false\nTag: {tag}\n".encode(),
                    )
                    add(
                        dist + "/ANDROID_SOURCE.txt",
                        (
                            f"Source: {APK_URL}\nAPK SHA-256: {APK_SHA256}\nMember: {APK_PAYLOAD}\n"
                            "Original metadata, licenses, Python bytecode and native libraries preserved.\n"
                            "Only WHEEL and RECORD are rebuilt; this provenance note is added.\n"
                            "Tavern's KohakuTerrarium source remains the separately locked 2.1.5 commit.\n"
                        ).encode(),
                    )
                    record = io.StringIO()
                    writer = csv.writer(record, lineterminator="\n")
                    writer.writerows(records)
                    writer.writerow((dist + "/RECORD", "", ""))
                    _put(wheel, dist + "/RECORD", record.getvalue().encode())
                pending.replace(path)
            output[canonical] = {
                "name": canonical,
                "version": version,
                "wheel": path.name,
                "sha256": sha256(path),
                "source": provenance,
                "preserved_files": len(entries),
            }
            print(f"已校验 APK 依赖：{path.name}", flush=True)
    write_json(cache / "native-recovery.json", {"format": 1, "source": provenance, "packages": output})
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--apk", type=Path, help="使用已有的同哈希 APK，避免下载")
    parser.add_argument(
        "--skip-pypi-native",
        action="store_true",
        help="跳过 MarkupSafe/KohakuVault，交给 prepare.py --prefer-pypi-native 从官方 PyPI 准备",
    )
    args = parser.parse_args()
    recover(
        args.cache, apk=args.apk, exclude={"markupsafe", "kohakuvault"} if args.skip_pypi_native else set()
    )
