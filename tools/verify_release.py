"""Verify archive hashes and install into a new Windows directory with fresh deps."""
import argparse
import hashlib
import json
import os
import subprocess
import tempfile
import time
import zipfile
from pathlib import Path, PurePosixPath

import httpx


def verify(archive_path):
    root = Path(__file__).resolve().parents[1]
    output = root / ".cache" / "release-validation"
    output.mkdir(parents=True, exist_ok=True)
    target = Path(tempfile.mkdtemp(prefix="release-install-", dir=root / ".cache"))
    with zipfile.ZipFile(archive_path) as archive:
        manifest = json.loads(archive.read("release-manifest.json"))
        assert set(archive.namelist()) == set(manifest["files"]) | {"release-manifest.json"}
        for name, digest in manifest["files"].items():
            path = PurePosixPath(name)
            assert not path.is_absolute() and ".." not in path.parts and ":" not in name and "\\" not in name
            assert path.parts[0] not in {"data", ".cache", ".venv", "node_modules"}
            assert not name.endswith((".sqlite3", ".db", ".log", ".env", ".safetensors", ".kohakutr"))
            assert hashlib.sha256(archive.read(name)).hexdigest() == digest
        archive.extractall(target)
    env = {**os.environ,
           "NO_PROXY": "127.0.0.1,localhost", "PYTHONIOENCODING": "utf-8"}
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    with (output / "install.log").open("w", encoding="utf-8") as log:
        def run(*command):
            subprocess.run(command, cwd=target, env=env, stdout=log, stderr=log, check=True, creationflags=flags)

        run("uv", "sync", "--frozen", "--no-dev")
        run("npm.cmd", "ci", "--registry=https://registry.npmjs.org")
        run("npm.cmd", "run", "build")
        python = str(target / ".venv/Scripts/python.exe")
        run(python, "-c", "import importlib.util; assert importlib.util.find_spec('torch') is None")
        run(python, "-c",
            "import importlib.metadata as metadata,json,tomllib; from pathlib import Path; "
            "expected=next(p for p in tomllib.loads(Path('uv.lock').read_text(encoding='utf-8'))['package'] "
            "if p['name']=='kohakuterrarium'); installed=metadata.distribution('kohakuterrarium'); "
            "assert installed.version==expected['version']; "
            "assert json.loads(installed.read_text('direct_url.json'))['url']==expected['source']['url']")
        # Exercise Windows PowerShell 5 as shipped with Windows, including UTF-8
        # script decoding, rather than bypassing the launcher with python -m.
        run("powershell", "-NoProfile", "-Command",
            "$bad=$false; foreach($p in 'setup.ps1','start.ps1','stop.ps1') { "
            "$tokens=$null; $errors=$null; "
            "[void][System.Management.Automation.Language.Parser]::ParseFile("
            "(Join-Path $PWD $p),[ref]$tokens,[ref]$errors); if($errors.Count) {$bad=$true} }; if($bad){exit 1}")
        process = subprocess.Popen(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                                    str(target / "start.ps1"), "-Port", "8017"], cwd=target, env=env,
                                   stdout=log, stderr=log, creationflags=flags)
        try:
            for _ in range(100):
                try:
                    response = httpx.get("http://127.0.0.1:8017/api/health", trust_env=False, timeout=1)
                    response.raise_for_status()
                    break
                except httpx.HTTPError:
                    if process.poll() is not None:
                        raise RuntimeError("Fresh installation did not start")
                    time.sleep(.1)
            else:
                raise RuntimeError("Fresh installation startup timed out")
            assert httpx.get("http://127.0.0.1:8017/", trust_env=False).status_code == 200
            settings = httpx.get("http://127.0.0.1:8017/api/settings", trust_env=False).json()
            assert not settings["shared"]["providers"][0]["has_api_key"]
            second = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                                     str(target / "start.ps1"), "-Port", "8017"], cwd=target, env=env,
                                    capture_output=True, creationflags=flags)
            assert second.returncode != 0 and b"TAVERN_PORT_IN_USE" in second.stderr
        finally:
            try:
                httpx.post("http://127.0.0.1:8017/api/shutdown", json={"confirm": True}, trust_env=False, timeout=3)
                process.wait(timeout=10)
            except (httpx.HTTPError, subprocess.TimeoutExpired):
                process.terminate()
                process.wait(timeout=5)
        run(python, "-m", "tavern", "--data", "data", "--backup", "backup.zip")
        run(python, "-m", "tavern", "--data", "restored", "--restore", "backup.zip")
    report = {"archive": Path(archive_path).name, "files_verified": len(manifest["files"]),
              "fresh_venv": True, "fresh_npm_ci": True, "production_build": True,
              "no_torch_startup": True, "duplicate_start_rejected": True, "backup_restore": True,
              "proxy_configured": bool(env.get("HTTPS_PROXY") or env.get("HTTP_PROXY") or env.get("ALL_PROXY")), "directory": str(target),
              "scope": "Fresh project directory and dependencies on current Windows host; not a fresh OS VM"}
    (output / "install.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("archive")
    verify(parser.parse_args().archive)
