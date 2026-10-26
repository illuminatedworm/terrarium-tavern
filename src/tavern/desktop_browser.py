"""Select an installed stable browser without downloading or sharing user profiles."""

import os
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

Channel = Literal["msedge", "chrome"]
NAMES = {"msedge": "Microsoft Edge", "chrome": "Google Chrome"}


@dataclass(frozen=True)
class SystemBrowser:
    channel: Channel
    executable: Path

    @property
    def launch_options(self) -> dict[str, str]:
        # Keep the branded channel's headless behavior, and launch exactly the
        # installation we reported rather than resolving it a second time in Node.
        return {"channel": self.channel, "executable_path": str(self.executable)}

    def __str__(self) -> str:
        return f"{NAMES[self.channel]}（系统安装）：{self.executable}"


def _locations(channel: Channel) -> Iterator[Path]:
    # These are the stable installation locations supported by Playwright.
    # Never inspect the browser's personal user-data directory or browser cache.
    if sys.platform == "win32":
        suffix = (
            "Microsoft/Edge/Application/msedge.exe"
            if channel == "msedge"
            else "Google/Chrome/Application/chrome.exe"
        )
        for key in ("LOCALAPPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)"):
            if root := os.environ.get(key):
                yield Path(root) / suffix
    elif sys.platform == "linux":
        yield Path("/opt/microsoft/msedge/msedge" if channel == "msedge" else "/opt/google/chrome/chrome")
    elif sys.platform == "darwin":
        name = NAMES[channel]
        yield Path(f"/Applications/{name}.app/Contents/MacOS/{name}")


def system_browser() -> SystemBrowser:
    """Resolve one visible preference for both the owned window and offline realms.

    Auto selection only skips missing installations. A browser that fails to launch
    (for example due to enterprise policy) must report its error, not switch engines.
    """
    preference = os.environ.get("TAVERN_BROWSER", "auto")
    if preference not in {"auto", "msedge", "chrome"}:
        raise RuntimeError("TAVERN_BROWSER 只接受 auto、msedge 或 chrome（稳定版）。")
    channels: tuple[Channel, ...] = ("msedge", "chrome") if sys.platform == "win32" else ("chrome", "msedge")
    for channel in channels:
        if preference not in {"auto", channel}:
            continue
        for path in _locations(channel):
            if path.is_file():
                return SystemBrowser(channel, path)
    requested = "Microsoft Edge 或 Google Chrome" if preference == "auto" else NAMES[preference]
    raise RuntimeError(
        f"未找到系统安装的 {requested} 稳定版（TAVERN_BROWSER={preference}）。"
        "请先安装对应浏览器，或用 TAVERN_BROWSER=auto 自动选择已安装的 Edge/Chrome。"
        "仅安装 WebView2 Runtime 不能替代浏览器；应用不会自动下载 Chromium。"
    )


def main() -> None:
    try:
        browser = system_browser()
    except RuntimeError as error:
        raise SystemExit(str(error)) from None
    print(f"脚本浏览器：{browser}；TAVERN_BROWSER={os.environ.get('TAVERN_BROWSER', 'auto')}", flush=True)


if __name__ == "__main__":
    main()
