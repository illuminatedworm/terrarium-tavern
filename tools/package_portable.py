"""Assemble native Windows/Linux portable releases from locked, fresh dependencies."""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path

from tavern.search.japanese_dictionary import DIC_SHA256, URL, VERSION, WHEEL_SHA256

if __package__:
    from .desktop_runtime import ROOT, download, host_target, runtime_lock, sha256, unpack
    from .package_release import publishable
    from .portable_resources import prune_resources
else:
    from desktop_runtime import ROOT, download, host_target, runtime_lock, sha256, unpack
    from package_release import publishable
    from portable_resources import prune_resources


def build_environment():
    # A release must not inherit the developer's live environment or Android host.
    env = {
        key: value
        for key, value in os.environ.items()
        if key
        not in {
            "PYTHONHOME",
            "PYTHONPATH",
            "VIRTUAL_ENV",
            "PLAYWRIGHT_NODEJS_PATH",
            "PLAYWRIGHT_BROWSERS_PATH",
        }
        and not key.startswith(("TAVERN_", "UV_PROJECT", "UV_WORKING_DIR", "UV_PYTHON"))
    }
    env.update(PYTHONUTF8="1", PYTHONNOUSERSITE="1")
    return env


def write_manifest(root, metadata):
    files, links = {}, {}
    for path in sorted(root.rglob("*")):
        name = path.relative_to(root).as_posix()
        if path.is_symlink():
            link = os.readlink(path)
            # Standalone Python's relative links must survive moving
            # the extracted app. A link into the builder would produce a broken bundle.
            if Path(link).is_absolute() or not path.resolve().is_relative_to(root.resolve()):
                raise ValueError(f"发行文件链接指向包外：{name}")
            links[name] = link
        elif path.is_file():
            files[name] = sha256(path)
    manifest = {**metadata, "files": files, "links": links}
    (root / "release-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def archive_tree(root, destination, target):
    if target == "windows-x64":
        with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(root.rglob("*")):
                if path.is_file():
                    info = zipfile.ZipInfo(path.relative_to(root).as_posix(), (2026, 1, 1, 0, 0, 0))
                    info.compress_type = zipfile.ZIP_DEFLATED
                    with path.open("rb") as source, archive.open(info, "w", force_zip64=True) as stream:
                        shutil.copyfileobj(source, stream)
    else:
        # Tar retains native executable modes and Python's relative symlinks.
        def reproducible(info):
            info.uid = info.gid = info.mtime = 0
            info.uname = info.gname = ""
            return info

        with tarfile.open(destination, "w:gz") as archive:
            for path in sorted(root.rglob("*")):
                archive.add(
                    path, arcname=path.relative_to(root).as_posix(), recursive=False, filter=reproducible
                )


def finish_package(app, requirements, *, target, destination, lock, version, retrieval, env):
    runtime = app / "runtime"
    windows = target == "windows-x64"
    python = runtime / "python" / ("python.exe" if windows else "bin/python3")
    for directory in ("LICENSE", "samples"):
        for path in (ROOT / directory).rglob("*"):
            relative = path.relative_to(ROOT)
            if path.is_file() and not path.is_symlink() and publishable(relative):
                (app / relative).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, app / relative)
    scripts = (
        ("start.ps1", "stop.ps1", "点击开始.cmd", "clear-conversations.cmd")
        if windows
        else (
            "start.sh",
            "stop.sh",
        )
    )
    helper = "tools/desktop-env.ps1" if windows else "tools/desktop-env.sh"
    (app / "tools").mkdir()
    for name in (*scripts, "clear-conversations.py", helper):
        shutil.copy2(ROOT / name, app / name)
        if name.endswith(".sh"):
            (app / name).chmod(0o755)
    shutil.copy2(ROOT / "packaging/desktop/README.md", app / "README.md")
    shutil.copy2(requirements, app / "requirements.txt")
    shutil.copy2(ROOT / "uv.lock", app / "uv.lock")
    # Node is supplied by the locked Playwright wheel, not a second runtime archive.
    (runtime / "sources.json").write_text(
        json.dumps(
            {
                "python": lock["python"],
                "on_demand_resources": {
                    "sudachi_core": {
                        "version": VERSION,
                        "url": URL,
                        "wheel_sha256": WHEEL_SHA256,
                        "dictionary_sha256": DIC_SHA256,
                    }
                },
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    # Assert importability without importing any developer source directory.
    subprocess.run(
        [
            str(python),
            "-c",
            "import tavern,fastapi,numpy,tiktoken,playwright,sudachipy; "
            "from importlib.util import find_spec; "
            "assert find_spec('sudachidict_core') is None; print(tavern.__file__)",
        ],
        env=env,
        check=True,
    )
    manifest = write_manifest(
        app,
        {
            "kind": "portable",
            "version": version,
            "target": target,
            "retrieval": retrieval,
            "python": lock["python"]["version"],
            "browsers": {"source": "system", "channels": ["msedge", "chrome"]},
        },
    )
    pending = destination.with_name(destination.name + ".part")
    archive_tree(app, pending, target)
    pending.replace(destination)
    digest = sha256(destination)
    destination.with_name(destination.name + ".sha256").write_text(
        f"{digest}  {destination.name}\n", encoding="ascii"
    )
    print(json.dumps({"file": str(destination), "files": len(manifest["files"]), "sha256": digest}))
    return destination


def package(*, target=None, destination=None, retrieval=False):
    native = host_target()
    target = target or native
    if target != native:
        raise ValueError(f"请在 {target} 主机构建对应便携包；当前为 {native}，不能混用原生依赖")
    if sys.version_info[:2] != (3, 13):
        raise RuntimeError("请使用项目的 Python 3.13 开发环境构建")
    if not (ROOT / "src/tavern/static/web/index.html").is_file():
        raise RuntimeError("请先运行 build.ps1 或 build.sh 构建前端与兼容 SDK")
    uv = shutil.which("uv")
    if uv is None:
        raise RuntimeError("需要 uv；请先准备项目开发环境")
    lock = runtime_lock()
    version = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))["version"]
    suffix = ".zip" if target == "windows-x64" else ".tar.gz"
    flavor = "-retrieval" if retrieval else ""
    destination = Path(
        destination or ROOT / "releases" / f"terrarium-tavern-{version}-{target}{flavor}{suffix}"
    )
    if not str(destination).endswith(suffix):
        raise ValueError(f"{target} 的发行包后缀应为 {suffix}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    cache = ROOT / ".cache"
    cache.mkdir(exist_ok=True)
    env = build_environment()

    def run(*args):
        subprocess.run([str(arg) for arg in args], cwd=ROOT, env=env, check=True)

    with tempfile.TemporaryDirectory(prefix=f"portable-{target}-", dir=cache) as temporary:
        work = Path(temporary)
        app = work / "app"
        runtime = app / "runtime"
        artifact = lock["python"][target]
        unpack(download(artifact), runtime / "python", artifact["prefix"])
        windows = target == "windows-x64"
        site = runtime / "python" / ("Lib/site-packages" if windows else "lib/python3.13/site-packages")
        site.mkdir(parents=True, exist_ok=True)
        if windows:
            # Embeddable Python deliberately ignores registry/PYTHONPATH. All
            # dependencies are vendored at build time; pip is not installed in it.
            (runtime / "python/python313._pth").write_text(
                "python313.zip\n.\nLib/site-packages\nimport site\n", encoding="ascii"
            )
        requirements = work / "requirements.txt"
        run(
            uv,
            "export",
            "--project",
            ROOT,
            "--frozen",
            "--no-dev",
            "--no-emit-project",
            "--format",
            "requirements-txt",
            "--output-file",
            requirements,
            *(["--extra", "retrieval"] if retrieval else []),
        )
        # --target is essential: never mutate or copy the live development venv.
        run(
            uv,
            "pip",
            "install",
            "--python",
            sys.executable,
            "--target",
            site,
            "--require-hashes",
            "--no-compile-bytecode",
            "--requirements",
            requirements,
        )
        wheels = work / "wheels"
        run(uv, "build", "--project", ROOT, "--wheel", "--out-dir", wheels)
        (wheel,) = wheels.glob("terrarium_tavern-*.whl")
        # Our Hatch wheel contains only package files and dist-info. Extracting
        # it avoids a direct_url.json with the builder's private filesystem path.
        with zipfile.ZipFile(wheel) as archive:
            for name in archive.namelist():
                if (
                    name.startswith("tavern/")
                    and not name.endswith("/")
                    and not publishable(Path("src") / name)
                ):
                    raise ValueError(f"应用 wheel 包含不允许发行的文件：{name}")
            archive.extractall(site)

        print(json.dumps({"pruned_resources": prune_resources(site)}), flush=True)
        return finish_package(
            app,
            requirements,
            target=target,
            destination=destination,
            lock=lock,
            version=version,
            retrieval=retrieval,
            env=env,
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", choices=("windows-x64", "linux-x64"))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--retrieval", action="store_true", help="随包安装锁定的 CUDA 检索依赖（体积较大）")
    args = parser.parse_args()
    package(
        target=args.target,
        destination=args.output,
        retrieval=args.retrieval,
    )
