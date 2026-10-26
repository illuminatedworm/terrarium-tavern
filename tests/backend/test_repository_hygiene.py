"""Publishing must not sweep local secrets or runtime data into source archives."""

from pathlib import Path, PurePosixPath

import pytest

from tools.package_release import publishable


@pytest.mark.parametrize(
    "name",
    [
        "web/.env.production",
        "docs/credentials.json",
        "tests/story.sqlite3-wal",
        "docs/backup.zip",
        "src/tavern/__pycache__/api.pyc",
        "src/tavern/private.key",
        ".agents/skills/example/SKILL.md",
        "src/tavern/static/.agents/notes.json",
        "tools/android/prepare.py",
    ],
)
def test_archive_rejects_private_artifacts(name):
    assert not publishable(PurePosixPath(name))


@pytest.mark.parametrize(
    "name",
    [
        "src/tavern/application/api.py",
        "src/tavern/tokenizer_data/o200k_base.tiktoken.gz",
        "src/tavern/static/web/index.html",
        "web/assets/quote-marks.ttf",
        "vendor/mvu/LICENSE",
        "samples/雾港来信.json",
        "tests/fixtures/retrieval_multilingual.json",
        # Desktop modules import the platform switch.
        "src/tavern/android/platform.py",
    ],
)
def test_archive_keeps_required_sources_and_resources(name):
    assert publishable(PurePosixPath(name))


def test_clean_install_does_not_inherit_live_environment_paths(monkeypatch):
    import os

    from tools.package_portable import build_environment
    from tools.verify_release import installation_environment

    for key in (
        "UV_PROJECT",
        "UV_WORKING_DIR",
        "UV_PROJECT_ENVIRONMENT",
        "UV_PYTHON_INSTALL_DIR",
        "UV_PYTHON",
        "VIRTUAL_ENV",
        "PYTHONHOME",
        "PYTHONPATH",
        "PLAYWRIGHT_NODEJS_PATH",
        "TAVERN_PLATFORM",
        "TAVERN_DATA",
        "TAVERN_STARTUP_TOKEN",
    ):
        monkeypatch.setenv(key, "existing-user-environment")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:8080")
    before = dict(os.environ)
    target = Path("isolated-release").resolve()
    env = installation_environment(target)
    for key in ("UV_PROJECT", "UV_WORKING_DIR", "UV_PROJECT_ENVIRONMENT", "UV_PYTHON_INSTALL_DIR"):
        assert Path(env[key]).is_relative_to(target)
    assert Path(env["UV_PROJECT_ENVIRONMENT"]) == target / ".venv"
    assert not {"VIRTUAL_ENV", "PYTHONHOME", "PYTHONPATH", "UV_PYTHON", "PLAYWRIGHT_NODEJS_PATH"} & env.keys()
    assert not any(key.startswith("TAVERN_") for key in env)
    assert env["HTTPS_PROXY"] == before["HTTPS_PROXY"]
    build_env = build_environment()
    assert (
        not {"VIRTUAL_ENV", "PYTHONHOME", "PYTHONPATH", "UV_PYTHON", "PLAYWRIGHT_NODEJS_PATH"}
        & build_env.keys()
    )
    assert not any(key.startswith(("TAVERN_", "UV_PROJECT", "UV_WORKING_DIR")) for key in build_env)
    assert build_env["HTTPS_PROXY"] == before["HTTPS_PROXY"]
    assert dict(os.environ) == before


def test_runtime_cache_rejects_changed_binary_without_replacing_it(tmp_path):
    import hashlib

    from tools.desktop_runtime import download

    artifact = {
        "url": "https://runtime.invalid/python.zip",
        "sha256": hashlib.sha256(b"verified binary").hexdigest(),
    }
    cached = tmp_path / "python.zip"
    cached.write_bytes(b"changed binary")
    with pytest.raises(ValueError, match="缓存校验失败"):
        download(artifact, tmp_path)
    assert cached.read_bytes() == b"changed binary"


def test_browser_cache_excludes_runtime_logs_and_unrequested_components(tmp_path, monkeypatch):
    import subprocess

    from tools.package_portable import copy_browser_cache

    cache = tmp_path / "cache with spaces"
    required = cache / "chromium-1"
    required.mkdir(parents=True)
    (required / "INSTALLATION_COMPLETE").touch()
    (required / "chrome.exe").write_bytes(b"browser binary")
    (required / "debug.log").write_text("private runtime activity", encoding="utf-8")
    unrelated = cache / "chromium_headless_shell-1"
    unrelated.mkdir()
    (unrelated / "profile.json").write_text("private profile", encoding="utf-8")

    def probe(command, **kwargs):
        assert command == ["python", "-m", "playwright", "install", "--dry-run", "--no-shell", "chromium"]
        return subprocess.CompletedProcess(command, 0, stdout=f"  Install location: {required}\n")

    monkeypatch.setattr(subprocess, "run", probe)
    output = tmp_path / "bundle"
    copy_browser_cache(Path("python"), cache, output, {})
    assert (output / "chromium-1/chrome.exe").read_bytes() == b"browser binary"
    assert not (output / "chromium-1/debug.log").exists()
    assert not (output / "chromium_headless_shell-1").exists()
    assert (required / "debug.log").is_file()


def test_portable_pruning_preserves_runtime_licenses_and_matching_records(tmp_path):
    import base64
    import csv
    import hashlib
    import os

    from tools.portable_resources import prune_resources

    site = tmp_path / "site"
    files = {
        "numpy/__init__.py": b"runtime",
        "numpy/testing/__init__.py": b"public runtime helper",
        "numpy/tests/test_array.py": b"development test",
        "numpy/tests/LICENSE.txt": b"original license",
        "jieba/dict.txt": b"dictionary",
        "jieba/finalseg/prob_emit.py": b"runtime HMM probabilities",
        "jieba/finalseg/prob_emit.p": b"Jython copy",
        "jieba/lac_small/model_baseline/word_emb": b"unused Paddle weights",
        "bin/magika.exe": b"CLI",
        "magika/models/standard_v3_3/model.onnx": b"Python classifier model",
        "pymupdf/mupdf-devel/include/mupdf.h": b"C++ header",
        "pymupdf/mupdfcpp64.dll": b"runtime DLL",
        "unreviewed_plugin/tests/runtime.py": b"unknown package stays intact",
    }
    rows = []
    for name, data in files.items():
        path = site / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode().rstrip("=")
        rows.append([name, f"sha256={digest}", str(len(data))])
    record = site / "fixture-1.dist-info/RECORD"
    record.parent.mkdir()
    rows.append(["fixture-1.dist-info/RECORD", "", ""])
    with record.open("w", encoding="utf-8", newline="") as stream:
        csv.writer(stream).writerows(rows)
    cached_record = tmp_path / "upstream-record"
    os.link(record, cached_record)
    original_record = cached_record.read_bytes()

    result = prune_resources(site)
    assert result["files"] == 5
    assert cached_record.read_bytes() == original_record
    with record.open(encoding="utf-8", newline="") as stream:
        retained = list(csv.reader(stream))
    assert {row[0] for row in retained} == {
        path.relative_to(site).as_posix() for path in site.rglob("*") if path.is_file()
    }
    for name in (
        "numpy/__init__.py",
        "numpy/testing/__init__.py",
        "numpy/tests/LICENSE.txt",
        "jieba/dict.txt",
        "jieba/finalseg/prob_emit.py",
        "magika/models/standard_v3_3/model.onnx",
        "pymupdf/mupdfcpp64.dll",
        "unreviewed_plugin/tests/runtime.py",
    ):
        assert (site / name).read_bytes() == files[name]
    assert retained == [row for row in rows if (site / row[0]).is_file()]


def test_portable_pruning_rejects_escaping_record_before_any_deletion(tmp_path):
    from tools.portable_resources import prune_resources

    site = tmp_path / "site"
    record = site / "fixture-1.dist-info/RECORD"
    record.parent.mkdir(parents=True)
    outside = tmp_path / "outside.txt"
    outside.write_text("keep", encoding="utf-8")
    candidate = site / "numpy/tests/test_array.py"
    candidate.parent.mkdir(parents=True)
    candidate.write_text("keep until the inventory is checked", encoding="utf-8")
    record.write_text("numpy/tests/test_array.py,,\nnumpy/tests/../../../outside.txt,,\n", encoding="utf-8")
    original = record.read_bytes()
    with pytest.raises(ValueError, match="路径越界"):
        prune_resources(site)
    assert outside.read_text(encoding="utf-8") == "keep"
    assert candidate.is_file() and record.read_bytes() == original
