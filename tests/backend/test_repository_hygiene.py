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
    assert dict(os.environ) == before
