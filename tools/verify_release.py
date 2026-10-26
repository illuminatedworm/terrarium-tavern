"""Verify source installs or native portable releases in an isolated directory."""

import argparse
import base64
import csv
import hashlib
import io
import json
import os
import posixpath
import socket
import subprocess
import tarfile
import tempfile
import time
import zipfile
from pathlib import Path, PurePosixPath

import httpx

PORTABLE_RESOURCE_PROBE = """
import io
import zipfile
import numpy as np
import pymupdf
from markitdown import StreamInfo
from markitdown.converters import DocxConverter, XlsxConverter
from openpyxl import Workbook
from tavern.search.retrieval import tokenize
from tavern.search.japanese_dictionary import DictionaryUnavailable
from importlib.util import find_spec

assert find_spec('sudachidict_core') is None
assert '港口' in tokenize('旅人前往港口')
assert 'lighthouse' in tokenize('The Lighthouse')
try:
    tokenize('冒険者が酒場へ向かう')
except DictionaryUnavailable:
    pass
else:
    raise AssertionError('Missing dictionary must fail explicitly without a download or fallback')
assert np.isclose(np.linalg.det([[1, 2], [3, 4]]), -2)

# Fixed, invented Office fixtures exercise the retained converters and their
# dependencies without reading user files or switching the desktop converter.
word = io.BytesIO()
with zipfile.ZipFile(word, 'w') as archive:
    archive.writestr('[Content_Types].xml',
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        '</Types>')
    archive.writestr('_rels/.rels',
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="r1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="word/document.xml"/></Relationships>')
    archive.writestr('word/document.xml',
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        '<w:body><w:p><w:r><w:t>portable DOCX reference</w:t></w:r></w:p></w:body></w:document>')
word.seek(0)
assert 'portable DOCX reference' in DocxConverter().convert(word, stream_info=StreamInfo(extension='.docx')).markdown
workbook = Workbook()
workbook.active.append(['name', 'value'])
workbook.active.append(['portable XLSX reference', 7])
sheet = io.BytesIO()
workbook.save(sheet)
workbook.close()
sheet.seek(0)
assert 'portable XLSX reference' in XlsxConverter().convert(sheet, stream_info=StreamInfo(extension='.xlsx')).markdown
with pymupdf.open() as document:
    page = document.new_page()
    page.insert_text((72, 72), 'portable PDF reference')
    pdf = document.tobytes()
with pymupdf.open(stream=pdf, filetype='pdf') as document:
    assert 'portable PDF reference' in document[0].get_text()
"""


def installation_environment(target: Path) -> dict[str, str]:
    # cwd alone does not isolate uv: inherited project/environment selectors can
    # redirect sync into the developer's live .venv. Python import overrides can
    # also make the check load the checkout instead of the extracted release.
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"VIRTUAL_ENV", "PYTHONHOME", "PYTHONPATH", "UV_PYTHON", "PLAYWRIGHT_NODEJS_PATH"}
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
        if manifest.get("kind") == "portable":
            assert not any("/sudachidict_core/" in name or "/sudachidict_core-" in name for name in names)
            for name in manifest["files"]:
                if not name.endswith(".dist-info/RECORD"):
                    continue
                parent = PurePosixPath(name).parent.parent
                for relative, digest, size in csv.reader(io.StringIO(read(name).decode("utf-8"))):
                    path = posixpath.normpath((parent / relative).as_posix())
                    assert path in manifest["files"], f"RECORD 引用缺失文件：{path}"
                    if digest:
                        algorithm, encoded = digest.split("=", 1)
                        assert algorithm == "sha256"
                        expected_digest = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).hex()
                        assert expected_digest == manifest["files"][path], path
                    if size:
                        member_size = (
                            archive.getinfo(path).file_size if zipped else archive.getmember(path).size
                        )
                        assert int(size) == member_size, path
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
        system_paths = [str(Path(os.environ["SystemRoot"]) / "System32")] if windows else ["/usr/bin", "/bin"]
        if windows:
            system_paths.append(str(Path(system_paths[0]) / "WindowsPowerShell/v1.0"))
        # No developer Node/uv/Python on PATH: startup must use the extracted bundle.
        env["PATH"] = os.pathsep.join(system_paths)
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
                "    browser = p.chromium.launch(channel='chromium', headless=True, chromium_sandbox=True)\n"
                "    try:\n"
                "        page = browser.new_page()\n"
                "        assert page.evaluate('1 + 1') == 2\n"
                "    finally:\n"
                "        browser.close()\n",
            )
            run(python, "-c", PORTABLE_RESOURCE_PROBE)
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
        "dependency_records": portable,
        "tokenizers_office_pdf": portable,
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
