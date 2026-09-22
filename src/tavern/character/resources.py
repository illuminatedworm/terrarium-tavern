"""Delete card-owned files only when no surviving card or story needs them."""

import json
import re


def references(card, manifest):
    blobs = {card.get("original"), *card.get("resources", {}).values()}
    blobs.update(resource.get("hash") for resource in manifest.get("resources", {}).values())
    blobs.update(manifest.get("scripts", {}).values())
    return blobs, set(manifest.get("scripts", {}))


def prune_card_assets(store, card, manifest):
    candidates, sources = references(card, manifest)
    path = store.card_manifest_path(card)
    if path.is_file():
        path.unlink()
    with store.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        survivors = [dict(row) for row in db.execute("SELECT original,resources,id FROM cards")]
        for survivor in survivors:
            survivor["resources"] = json.loads(survivor["resources"])
        survivors.extend(json.loads(row[0]) for row in db.execute("SELECT card_data FROM sessions WHERE card_data IS NOT NULL"))
        survivors.extend(json.loads(row[0]) for row in db.execute("SELECT source FROM card_edits"))
        retained, retained_sources = set(), set()
        for survivor in survivors:
            for current in (store.card_manifest(survivor), survivor.get("dependencies", {})):
                blobs, scripts = references(survivor, current)
                retained.update(blobs)
                retained_sources.update(scripts)
        blob_root = (store.root / "blobs").resolve()
        for digest in candidates - retained:
            if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
                continue
            blob = blob_root / digest
            if blob.resolve().parent == blob_root and blob.is_file():
                blob.unlink()
        dependency_root = (store.root / "dependencies").resolve()
        for digest in sources - retained_sources:
            if not re.fullmatch(r"[0-9a-f]{64}", digest):
                continue
            for name in (f"{digest}.js", f"{digest}-build.json"):
                artifact = dependency_root / name
                if artifact.resolve().parent == dependency_root and artifact.is_file():
                    artifact.unlink()
