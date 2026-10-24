"""Prepare the Windows Android toolchain under android-dev without changing system tools."""

import argparse
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from build_wheels import NDK_VERSION, RUST_VERSION
from recover_native import DEFAULT_CACHE, ROOT, download

PYTHON_VERSION = "3.13.15"
JDK_FOLDER = "jdk-17.0.17+10"
BUILD_PACKAGES = (
    "pip==26.2.2",
    "maturin==1.15.0",
    "setuptools-rust==1.13.0",
    "setuptools==84.2.1",
    "wheel==0.48.0",
    "packaging==26.3",
    "lief==1.0.0",
)
ARCHIVES = (
    (
        "https://github.com/adoptium/temurin17-binaries/releases/download/jdk-17.0.17%2B10/"
        "OpenJDK17U-jdk_x64_windows_hotspot_17.0.17_10.zip",
        "dcf0064efec7e515a5e3b56e7532f1a1c125510303c6c9e60af8878e3f7347fe",
        "jdk/" + JDK_FOLDER,
        JDK_FOLDER,
    ),
    (
        "https://services.gradle.org/distributions/gradle-8.13-bin.zip",
        "20f1b1176237254a6fc204d8434196fa11a4cfb387567519c61556e8710aed78",
        "gradle-8.13",
        "gradle-8.13",
    ),
    (
        "https://dl.google.com/android/repository/commandlinetools-win-13114758_latest.zip",
        "98b565cb657b012dae6794cefc0f66ae1efb4690c699b78a614b4a6a3505b003",
        "sdk/cmdline-tools/19.0",
        "cmdline-tools",
    ),
)
RUSTUP_URL = "https://static.rust-lang.org/rustup/archive/1.29.2/x86_64-pc-windows-msvc/rustup-init.exe"
RUSTUP_SHA256 = "00f139da614dced87d3c3d4121c43eebdac3c1bf8a5ee86b9d80dfd71c8d70fd"


def unpack(archive: Path, destination: Path, prefix: str) -> None:
    # Re-extract the verified archive on an explicit setup run, including after interruption.
    with zipfile.ZipFile(archive) as bundle:
        for member in bundle.infolist():
            relative = PurePosixPath(member.filename).relative_to(prefix)
            target = destination.joinpath(*relative.parts)
            if not target.resolve().is_relative_to(destination.resolve()):
                raise ValueError(f"工具链归档路径越界：{member.filename}")
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with bundle.open(member) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)


def setup(accept_sdk_licenses: bool) -> None:
    root = DEFAULT_CACHE
    root.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.update(
        UV_PYTHON_INSTALL_DIR=str(root / "python"),
        JAVA_HOME=str(root / "jdk" / JDK_FOLDER),
        ANDROID_HOME=str(root / "sdk"),
        CARGO_HOME=str(root / "cargo"),
        RUSTUP_HOME=str(root / "rustup"),
        RUSTUP_TOOLCHAIN=RUST_VERSION,
    )
    # sdkmanager's URLConnection otherwise waits indefinitely on a stalled download.
    env["JAVA_TOOL_OPTIONS"] = (
        env.get("JAVA_TOOL_OPTIONS", "")
        + " -Dsun.net.client.defaultConnectTimeout=30000 -Dsun.net.client.defaultReadTimeout=60000"
    ).strip()

    def run(command, **kwargs):
        subprocess.run(
            [str(part) for part in command],
            cwd=ROOT,
            env=env,
            check=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
            stdout=sys.stdout,
            stderr=sys.stderr,
            **kwargs,
        )

    run(["uv", "python", "install", PYTHON_VERSION, "--no-bin", "--no-registry"])
    python = root / "python" / f"cpython-{PYTHON_VERSION}-windows-x86_64-none/python.exe"
    build_python = root / "build-env/Scripts/python.exe"
    if not build_python.exists():
        run(["uv", "venv", "--python", python, root / "build-env"])
    run(
        [
            "uv",
            "pip",
            "install",
            "--python",
            build_python,
            "--index-url",
            "https://pypi.org/simple",
            *BUILD_PACKAGES,
        ]
    )

    for url, checksum, folder, prefix in ARCHIVES:
        archive = download(url, root / "downloads" / url.rsplit("/", 1)[1], checksum)
        print(f"解压工具链：{folder}", flush=True)
        unpack(archive, root / folder, prefix)

    sdkmanager = root / "sdk/cmdline-tools/19.0/bin/sdkmanager.bat"
    sdk_options = [f"--sdk_root={root / 'sdk'}"]
    proxy = env.get("HTTPS_PROXY") or env.get("https_proxy") or env.get("HTTP_PROXY") or env.get("http_proxy")
    if proxy:
        parsed = urlsplit(proxy)
        if parsed.scheme != "http" or not parsed.hostname or not parsed.port or parsed.username:
            raise ValueError("SDK 下载代理需要不含账号的 http://主机:端口 地址")
        sdk_options.extend(["--proxy=http", f"--proxy_host={parsed.hostname}", f"--proxy_port={parsed.port}"])
    run(
        [
            sdkmanager,
            *sdk_options,
            "platform-tools",
            "platforms;android-35",
            "build-tools;35.0.0",
            f"ndk;{NDK_VERSION}",
        ],
        input="y\n" * 20 if accept_sdk_licenses else None,
        text=True,
    )

    rustup = download(RUSTUP_URL, root / "downloads/rustup-init-1.29.2.exe", RUSTUP_SHA256)
    run([rustup, "-y", "--no-modify-path", "--profile", "minimal", "--default-toolchain", RUST_VERSION])
    run(
        [root / "cargo/bin/rustup.exe", "target", "add", "aarch64-linux-android", "--toolchain", RUST_VERSION]
    )
    print(
        f"Android 工具链已准备：{root}\n下一步：android-dev/build-env/Scripts/python.exe tools/android/prepare.py --prefer-pypi-native"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--accept-sdk-licenses", action="store_true", help="为此目录中的 SDK 接受 Android SDK 许可"
    )
    args = parser.parse_args()
    if os.name != "nt":
        parser.error("当前工具只安装 Windows Android 工具链")
    setup(args.accept_sdk_licenses)
