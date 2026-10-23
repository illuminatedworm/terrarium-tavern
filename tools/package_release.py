"""Produce a source distribution from an explicit allowlist, never the workspace."""

import hashlib
import importlib.metadata
import json
import subprocess
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FILES = (
    ".gitignore",
    ".python-version",
    ".prettierrc.json",
    "README.md",
    "pyproject.toml",
    "uv.lock",
    "package.json",
    "package-lock.json",
    "compatibility.lock.json",
    "index.html",
    "tsconfig.json",
    "vite.config.ts",
    "vitest.config.ts",
    "setup.ps1",
    "start.ps1",
    "stop.ps1",
    "run-dev.ps1",
    "build.ps1",
    "setup.sh",
    "start.sh",
    "stop.sh",
    "run-dev.sh",
    "build.sh",
    "clear-conversations.py",
    "clear-conversations.cmd",
)
DIRECTORIES = ("LICENSE", "src", "web", "compat", "vendor", "samples", "docs", "tests", "tools")


def publishable(relative):
    """Limit source archives to source/resources, even outside a Git checkout."""
    name = relative.name.lower()
    if any(part.lower() == ".agents" for part in relative.parts):
        return False
    # The desktop archive carries only desktop tooling; Android builds come from a checkout.
    if relative.parts[:2] == ("tools", "android"):
        return False
    if any(part.startswith(".") or part in {"__pycache__", "node_modules"} for part in relative.parts[1:]):
        return False
    if name.startswith(("credentials", "secrets")) or name == "settings.json" or ".local." in name:
        return False
    if relative.suffix.lower() == ".txt" and (
        relative.parts[0] == "LICENSE" or relative.parts[:4] == ("src", "tavern", "static", "licenses")
    ):
        return True
    if relative.as_posix() == "src/tavern/static/esbuild/esbuild.wasm":
        return True
    return relative.suffix.lower() in {
        ".py",
        ".ts",
        ".vue",
        ".css",
        ".js",
        ".mjs",
        ".html",
        ".md",
        ".json",
        ".ttf",
        ".svg",
        ".ico",
    } or name in {"license", "copying", "notice", "cl100k_base.tiktoken.gz", "o200k_base.tiktoken.gz"}


def package(destination=None):
    version = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))["version"]
    destination = destination or ROOT / "releases" / f"terrarium-tavern-{version}-windows-source.zip"
    destination.parent.mkdir(parents=True, exist_ok=True)
    contents = {name: (ROOT / name).read_bytes() for name in FILES}
    ignored = set()
    if (ROOT / ".git").exists():
        result = subprocess.run(
            ["git", "ls-files", "--others", "--ignored", "--exclude-standard", "-z"],
            cwd=ROOT,
            capture_output=True,
            check=True,
        )
        ignored = set(result.stdout.decode("utf-8").split("\0"))
    for folder in DIRECTORIES:
        for path in (ROOT / folder).rglob("*"):
            relative = path.relative_to(ROOT)
            generated = relative.parts[:3] == ("src", "tavern", "static")
            if (
                path.is_file()
                and not any(parent.is_symlink() for parent in (path, *path.parents))
                and publishable(relative)
                and (relative.as_posix() not in ignored or generated)
            ):
                contents[relative.as_posix()] = path.read_bytes()
    # Include actual dependency license files, not paths into the build machine.
    for distribution in importlib.metadata.distributions():
        name = distribution.metadata.get("Name", "unknown").replace("/", "_")
        for file in distribution.files or []:
            if file.name.upper().startswith(("LICENSE", "COPYING", "NOTICE")) and ".dist-info" in str(file):
                path = Path(distribution.locate_file(file))
                if path.is_file():
                    contents[f"third-party/python/{name}/{file.name}"] = path.read_bytes()
    for manifest in (ROOT / "node_modules").rglob("package.json"):
        for path in manifest.parent.iterdir():
            if path.is_file() and path.name.upper().startswith(("LICENSE", "COPYING", "NOTICE")):
                name = path.relative_to(ROOT / "node_modules").as_posix()
                contents[f"third-party/npm/{name}"] = path.read_bytes()
    assert "src/tavern/static/web/index.html" in contents, "Run npm run build first"
    for name in ("PROJECT_LICENSE.md", "KohakuTerrarium.txt", "THIRD_PARTY_NOTICES.md", "README.md"):
        assert f"LICENSE/{name}" in contents, f"Missing license file: {name}"
    assert any("kohakuterrarium" in name.lower() and "license" in name.lower() for name in contents)
    manifest = {name: hashlib.sha256(data).hexdigest() for name, data in sorted(contents.items())}
    contents["release-manifest.json"] = json.dumps(
        {"version": version, "files": manifest}, ensure_ascii=False, indent=2
    ).encode()
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(contents.items()):
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            if name.endswith(".sh"):
                info.create_system = 3
                info.external_attr = 0o100755 << 16
            archive.writestr(info, data)
    digest = hashlib.sha256(destination.read_bytes()).hexdigest()
    destination.with_suffix(".zip.sha256").write_text(f"{digest}  {destination.name}\n", encoding="ascii")
    print(
        json.dumps(
            {
                "file": str(destination),
                "files": len(contents),
                "bytes": destination.stat().st_size,
                "sha256": digest,
            }
        )
    )
    return destination


if __name__ == "__main__":
    package()
