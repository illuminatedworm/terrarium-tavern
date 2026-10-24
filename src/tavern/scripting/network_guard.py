"""The same document-start guard is used by desktop Chromium and Android WebView."""

from pathlib import Path

NETWORK_GUARD = (Path(__file__).parents[1] / "static/security/card-network-guard.js").read_text("utf-8")
