"""Prune audited development resources from a freshly installed portable runtime."""

import csv
from pathlib import Path, PurePosixPath

# Only these audited dependency roots may lose tests/benchmarks. Runtime helpers
# such as numpy.testing and sympy.testing are deliberately outside this selection.
TEST_PACKAGES = frozenset(
    (
        "adodbapi",
        "annotated_types",
        "certifi",
        "colorama",
        "cryptography",
        "dulwich",
        "fsspec",
        "gitdb",
        "greenlet",
        "isapi",
        "joblib",
        "jsonschema",
        "jsonschema_specifications",
        "libcst",
        "mpmath",
        "numpy",
        "pandas",
        "referencing",
        "regex",
        "smmap",
        "sniffio",
        "sympy",
        "tld",
        "win32",
        "win32com",
        "win32comext",
        "winpty",
    )
)


def _development_resource(name: PurePosixPath) -> bool:
    parts = name.parts
    # Keep original notices even when they accompany development-only files.
    if name.name.upper().startswith(("LICENSE", "COPYING", "NOTICE", "COPYRIGHT")) or any(
        part.lower() == "licenses" or part.lower().endswith(".dist-info") for part in parts
    ):
        return False
    if parts[:2] in {
        ("jieba", "analyse"),
        ("jieba", "posseg"),
        ("jieba", "lac_small"),
        ("pymupdf", "mupdf-devel"),
    }:
        return True
    if parts[:2] == ("jieba", "finalseg") and name.name in {"prob_start.p", "prob_trans.p", "prob_emit.p"}:
        # Windows/Linux CPython imports the identical probabilities from .py;
        # only Jieba's Jython branch reads these redundant pickle files.
        return True
    if parts == ("bin", "magika") or parts == ("bin", "magika.exe"):
        # MarkItDown imports the Python classifier; it never invokes its Rust CLI.
        return True
    return (parts[0] in TEST_PACKAGES or parts[:2] == ("google", "genai")) and (
        bool({"tests", "test", "benchmarks"}.intersection(parts)) or name.name.startswith("test_")
    )


def prune_resources(site: Path) -> dict[str, int]:
    """Use installed RECORDs as the file inventory; never traverse a developer venv."""
    site = site.resolve()
    selected: set[Path] = set()
    records: dict[Path, list[list[str]]] = {}
    for record in sorted(site.glob("*.dist-info/RECORD")):
        with record.open(encoding="utf-8", newline="") as stream:
            rows = list(csv.reader(stream))
        retained = []
        for row in rows:
            relative = PurePosixPath(row[0])
            if not _development_resource(relative):
                retained.append(row)
                continue
            path = site.joinpath(*relative.parts)
            # RECORD is upstream file data. Check every deletion target before
            # mutating anything, including paths reached through symlinks.
            if not path.resolve().is_relative_to(site):
                raise ValueError(f"发行依赖资源路径越界：{relative}")
            selected.add(path)
        if len(retained) != len(rows):
            if not record.resolve().is_relative_to(site):
                raise ValueError(f"发行依赖清单路径越界：{record}")
            records[record] = retained

    sizes = {path: path.stat().st_size for path in selected}
    directories = set()
    for path in selected:
        path.unlink()
        parent = path.parent
        while parent != site:
            directories.add(parent)
            parent = parent.parent
    for record, rows in records.items():
        # RECORD's own hash/size stay blank, as required by the wheel format.
        # Replace instead of editing in place: uv may hardlink installed files
        # to its cache, which must retain the original upstream RECORD.
        pending = record.with_name("RECORD.portable")
        with pending.open("x", encoding="utf-8", newline="") as stream:
            csv.writer(stream, lineterminator="\n").writerows(rows)
        pending.replace(record)
    for directory in sorted(directories, key=lambda path: len(path.parts), reverse=True):
        if not any(directory.iterdir()):
            directory.rmdir()
    return {"files": len(selected), "bytes": sum(sizes.values())}
