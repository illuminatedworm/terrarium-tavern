"""Pinned desktop runtimes; development Python stays a normal uv environment."""

import argparse
import hashlib
import json
import platform
import shutil
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / "packaging/desktop/runtimes.json"
DOWNLOADS = ROOT / "desktop-dev/downloads"


def host_target():
    if platform.machine().lower() not in {"amd64", "x86_64"} or sys.platform not in {"win32", "linux"}:
        raise RuntimeError("内置桌面运行时目前支持 Windows x64 与 Linux x64；其他平台开发请使用系统 Node")
    return "windows-x64" if sys.platform == "win32" else "linux-x64"


def runtime_lock():
    return json.loads(LOCK.read_text(encoding="utf-8"))


def sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def download(artifact, directory=DOWNLOADS):
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / unquote(urlsplit(artifact["url"]).path.rsplit("/", 1)[1])
    if not target.exists():
        temporary = target.with_name(target.name + ".part")
        print("下载 " + artifact["url"], flush=True)
        # urllib respects the caller's HTTP(S)_PROXY; never embed a machine-specific proxy.
        request = urllib.request.Request(artifact["url"], headers={"User-Agent": "Terrarium-Tavern-build"})
        with urllib.request.urlopen(request, timeout=60) as response, temporary.open("wb") as stream:
            shutil.copyfileobj(response, stream)
        if sha256(temporary) != artifact["sha256"]:
            raise ValueError(f"运行时下载校验失败：{temporary}")
        temporary.replace(target)
    if sha256(target) != artifact["sha256"]:
        raise ValueError(f"运行时缓存校验失败，保留文件供检查：{target}")
    return target


def unpack(archive, destination, prefix):
    """Extract trusted, hash-checked archives without admitting external paths."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise FileExistsError(f"运行时目标已存在：{destination}")
    with tempfile.TemporaryDirectory(prefix="runtime-extract-", dir=destination.parent) as temporary:
        extracted = Path(temporary)
        if zipfile.is_zipfile(archive):
            with zipfile.ZipFile(archive) as bundle:
                for member in bundle.infolist():
                    name = PurePosixPath(member.filename)
                    if (
                        name.is_absolute()
                        or ".." in name.parts
                        or ":" in member.filename
                        or "\\" in member.filename
                    ):
                        raise ValueError(f"运行时归档路径越界：{member.filename}")
                bundle.extractall(extracted)
        else:
            with tarfile.open(archive) as bundle:
                # The data filter also constrains symlink/hardlink targets. Preserve
                # upstream executable bits and relative links for Linux relocatability.
                bundle.extractall(extracted, filter="data")
        source = extracted / prefix if prefix else extracted
        shutil.move(str(source), str(destination))


def prepare_node():
    target = host_target()
    artifact = runtime_lock()["node"][target]
    archive = download(artifact)
    destination = ROOT / "desktop-dev/node" / artifact["prefix"]
    if not destination.exists():
        unpack(archive, destination, artifact["prefix"])
    print(destination)
    return destination


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("node",))
    parser.parse_args()
    prepare_node()
