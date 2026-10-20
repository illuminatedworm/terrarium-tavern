"""Publishing must not sweep local secrets or runtime data into source archives."""

from pathlib import PurePosixPath

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
    ],
)
def test_archive_keeps_required_sources_and_resources(name):
    assert publishable(PurePosixPath(name))
