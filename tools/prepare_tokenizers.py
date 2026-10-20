"""Bundle verified tiktoken vocabularies so counting never downloads at runtime."""

import base64
import gzip
import hashlib
import json
import os
from pathlib import Path

import tiktoken


def main():
    root = Path(__file__).resolve().parents[1]
    os.environ["TIKTOKEN_CACHE_DIR"] = str(root / ".cache/tokenizer-download")
    target = root / "src/tavern/tokenizer_data"
    target.mkdir(exist_ok=True)
    manifest = {}
    for name in ("o200k_base", "cl100k_base"):
        encoding = tiktoken.get_encoding(name)  # Upstream checks the vocabulary SHA-256.
        ranks = b"".join(
            base64.b64encode(token) + b" " + str(rank).encode() + b"\n"
            for token, rank in encoding._mergeable_ranks.items()
        )
        (target / f"{name}.tiktoken.gz").write_bytes(gzip.compress(ranks, mtime=0))
        manifest[name] = {
            "name": name,
            "pat_str": encoding._pat_str,
            "special_tokens": encoding._special_tokens,
            "sha256": hashlib.sha256(ranks).hexdigest(),
        }
    (target / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print("Bundled o200k_base and cl100k_base; runtime counting is offline.")


if __name__ == "__main__":
    main()
