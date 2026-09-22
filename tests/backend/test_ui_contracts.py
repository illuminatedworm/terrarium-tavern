import re
from pathlib import Path


def test_app_ui_does_not_use_native_blocking_dialogs():
    pattern = re.compile(r"\b(?:window\s*\.\s*)?(?:alert|confirm|prompt)\s*\(")
    for path in Path("web").rglob("*"):
        if path.suffix in {".vue", ".ts", ".js"}:
            assert not pattern.search(path.read_text(encoding="utf-8")), path
