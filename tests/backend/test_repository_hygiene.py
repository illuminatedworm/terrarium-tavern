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
    assert not {"VIRTUAL_ENV", "PYTHONHOME", "PYTHONPATH", "UV_PYTHON"} & env.keys()
    assert not any(key.startswith("TAVERN_") for key in env)
    assert env["HTTPS_PROXY"] == before["HTTPS_PROXY"]
    build_env = build_environment()
    assert not {"VIRTUAL_ENV", "PYTHONHOME", "PYTHONPATH", "UV_PYTHON"} & build_env.keys()
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
    unrelated = cache / "unrequested-browser"
    unrelated.mkdir()
    (unrelated / "profile.json").write_text("private profile", encoding="utf-8")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            [], 0, stdout=f"  Install location: {required}\n"
        ),
    )
    output = tmp_path / "bundle"
    copy_browser_cache(Path("python"), cache, output, {})
    assert (output / "chromium-1/chrome.exe").read_bytes() == b"browser binary"
    assert not (output / "chromium-1/debug.log").exists()
    assert not (output / "unrequested-browser").exists()
    assert (required / "debug.log").is_file()
