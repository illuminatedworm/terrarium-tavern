"""Verify source installs or native portable releases in an isolated directory."""

import argparse
import hashlib
import json
import os
import socket
import subprocess
import tarfile
import tempfile
import time
import zipfile
from pathlib import Path, PurePosixPath

import httpx


def installation_environment(target: Path) -> dict[str, str]:
    # cwd alone does not isolate uv: inherited project/environment selectors can
    # redirect sync into the developer's live .venv. Python import overrides can
    # also make the check load the checkout instead of the extracted release.
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"VIRTUAL_ENV", "PYTHONHOME", "PYTHONPATH", "UV_PYTHON"}
        and not key.startswith("TAVERN_")
    }
    env.update(
        UV_PROJECT=str(target),
        UV_WORKING_DIR=str(target),
        UV_PROJECT_ENVIRONMENT=str(target / ".venv"),
        UV_PYTHON_INSTALL_DIR=str(target / "desktop-dev" / "python"),
        NO_PROXY="127.0.0.1,localhost",
        PYTHONIOENCODING="utf-8",
    )
    return env


def verify(archive_path):
    root = Path(__file__).resolve().parents[1]
    output = root / ".cache" / "release-validation"
    output.mkdir(parents=True, exist_ok=True)
    target = Path(tempfile.mkdtemp(prefix="release-install-", dir=root / ".cache"))
    zipped = zipfile.is_zipfile(archive_path)
    with zipfile.ZipFile(archive_path) if zipped else tarfile.open(archive_path) as archive:

        def read(name):
            return archive.read(name) if zipped else archive.extractfile(name).read()

        manifest = json.loads(read("release-manifest.json"))
        links = manifest.get("links", {})
        names = archive.namelist() if zipped else [m.name for m in archive.getmembers() if not m.isdir()]
        expected = set(manifest["files"]) | set(links) | {"release-manifest.json"}
        assert set(names) == expected and len(names) == len(expected)
        for name, digest in manifest["files"].items():
            path = PurePosixPath(name)
            assert not path.is_absolute() and ".." not in path.parts and ":" not in name and "\\" not in name
            assert path.parts[0] not in {"data", ".cache", ".venv", "node_modules"}
            assert not name.endswith((".sqlite3", ".db", ".log", ".env", ".safetensors", ".kohakutr")), name
            assert hashlib.sha256(read(name)).hexdigest() == digest
        for name, link in links.items():
            path = PurePosixPath(name)
            assert not path.is_absolute() and ".." not in path.parts and ":" not in name and "\\" not in name
            assert not Path(link).is_absolute()
            assert (target / name).parent.joinpath(link).resolve().is_relative_to(target.resolve())
            member = archive.getmember(name)
            assert member.issym() and member.linkname == link
        if zipped:
            archive.extractall(target)
        else:
            archive.extractall(target, filter="data")
    env = installation_environment(target)
    portable = manifest.get("kind") == "portable"
    windows = os.name == "nt"
    if portable:
        native = "windows-x64" if windows else "linux-x64"
        assert manifest["target"] == native, "请在对应平台验证便携包"
        node = target / "runtime/node" / ("" if windows else "bin")
        system_paths = [str(Path(os.environ["SystemRoot"]) / "System32")] if windows else ["/usr/bin", "/bin"]
        if windows:
            system_paths.append(str(Path(system_paths[0]) / "WindowsPowerShell/v1.0"))
        # No developer Node/uv/Python on PATH: startup must use the extracted bundle.
        env["PATH"] = os.pathsep.join([str(node), *system_paths])
        env["PLAYWRIGHT_BROWSERS_PATH"] = str(target / "runtime/browsers")
        env["PYTHONNOUSERSITE"] = "1"
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    with (output / "install.log").open("w", encoding="utf-8") as log:

        def run(*command):
            subprocess.run(
                command, cwd=target, env=env, stdout=log, stderr=log, check=True, creationflags=flags
            )

        if portable:
            python = str(target / "runtime/python" / ("python.exe" if windows else "bin/python3"))
            run(str(node / ("node.exe" if windows else "node")), "--version")
            run(
                python,
                "-c",
                "from pathlib import Path; import tavern; "
                "assert Path(tavern.__file__).resolve().is_relative_to(Path('runtime').resolve())",
            )
            run(
                python,
                "-c",
                "from playwright.sync_api import sync_playwright\n"
                "with sync_playwright() as p:\n"
                "    browser = p.chromium.launch(headless=True, chromium_sandbox=True)\n"
                "    try:\n"
                "        page = browser.new_page()\n"
                "        assert page.evaluate('1 + 1') == 2\n"
                "    finally:\n"
                "        browser.close()\n",
            )
        else:
            run("uv", "sync", "--frozen", "--no-dev")
            npm = "npm.cmd" if windows else "npm"
            run(npm, "ci", "--registry=https://registry.npmjs.org")
            run(npm, "run", "build")
            python = str(target / (".venv/Scripts/python.exe" if windows else ".venv/bin/python"))
        if not manifest.get("retrieval", False):
            run(python, "-c", "import importlib.util; assert importlib.util.find_spec('torch') is None")
        run(
            python,
            "-c",
            "import importlib.metadata as metadata,json,tomllib; from pathlib import Path; "
            "expected=next(p for p in tomllib.loads(Path('uv.lock').read_text(encoding='utf-8'))['package'] "
            "if p['name']=='kohakuterrarium'); installed=metadata.distribution('kohakuterrarium'); "
            "assert installed.version==expected['version']; "
            "assert json.loads(installed.read_text('direct_url.json'))['url']==expected['source']['url']",
        )
        # Exercise Windows PowerShell 5 as shipped with Windows, including UTF-8
        # script decoding, rather than bypassing the launcher with python -m.
        if windows:
            run(
                "powershell",
                "-NoProfile",
                "-Command",
                "$bad=$false; foreach($p in 'start.ps1','stop.ps1','tools/desktop-env.ps1') { "
                "$tokens=$null; $errors=$null; "
                "[void][System.Management.Automation.Language.Parser]::ParseFile("
                "(Join-Path $PWD $p),[ref]$tokens,[ref]$errors); if($errors.Count) {$bad=$true} }; if($bad){exit 1}",
            )
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        base = f"http://127.0.0.1:{port}"
        launcher = (
            [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(target / "start.ps1"),
                "-Port",
                str(port),
            ]
            if windows
            else ["bash", str(target / "start.sh"), "--port", str(port)]
        )
        process = subprocess.Popen(
            launcher,
            cwd=target,
            env=env,
            stdout=log,
            stderr=log,
            creationflags=flags,
        )
        try:
            for _ in range(100):
                try:
                    if process.poll() is not None:
                        raise RuntimeError("Fresh installation did not start")
                    response = httpx.get(f"{base}/api/health", trust_env=False, timeout=1)
                    response.raise_for_status()
                    break
                except httpx.HTTPError:
                    if process.poll() is not None:
                        raise RuntimeError("Fresh installation did not start")
                    time.sleep(0.1)
            else:
                raise RuntimeError("Fresh installation startup timed out")
            assert httpx.get(f"{base}/", trust_env=False).status_code == 200
            settings = httpx.get(f"{base}/api/settings", trust_env=False).json()
            assert not settings["shared"]["providers"][0]["has_api_key"]
            second = subprocess.run(
                launcher,
                cwd=target,
                env=env,
                capture_output=True,
                creationflags=flags,
            )
            assert second.returncode != 0 and b"TAVERN_PORT_IN_USE" in second.stderr
        finally:
            try:
                httpx.post(f"{base}/api/shutdown", json={"confirm": True}, trust_env=False, timeout=3)
                process.wait(timeout=10)
            except (httpx.HTTPError, subprocess.TimeoutExpired):
                if windows:
                    subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], check=False)
                else:
                    process.terminate()
                process.wait(timeout=5)
        run(python, "-m", "tavern", "--data", "data", "--backup", "backup.zip")
        run(python, "-m", "tavern", "--data", "restored", "--restore", "backup.zip")
    report = {
        "archive": Path(archive_path).name,
        "files_verified": len(manifest["files"]),
        "portable": portable,
        "fresh_venv": not portable,
        "fresh_npm_ci": not portable,
        "production_build": not portable,
        "bundled_browser": portable,
        "no_torch_startup": not manifest.get("retrieval", False),
        "duplicate_start_rejected": True,
        "backup_restore": True,
        "proxy_configured": bool(env.get("HTTPS_PROXY") or env.get("HTTP_PROXY") or env.get("ALL_PROXY")),
        "directory": str(target),
        "scope": "Isolated directory on current native host; not a fresh OS VM",
    }
    (output / "install.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("archive")
    verify(parser.parse_args().archive)
