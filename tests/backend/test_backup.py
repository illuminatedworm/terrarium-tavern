from pathlib import Path

import pytest

from tavern.core.models import SessionCreate
from tavern.storage.backup import backup, restore
from tavern.storage.store import Store


def test_backup_restore_preserves_cards_and_rejects_overwrite(tmp_path):
    store = Store(tmp_path / "source")
    card = store.import_card(Path("samples/雾港来信.json").read_bytes(), "card.json")
    session = store.new_session(SessionCreate(card_id=card["id"]))
    archive = tmp_path / "backup.zip"
    backup(store, archive)
    restored = restore(archive, tmp_path / "restored")
    assert restored.card(card["id"]) == store.card(card["id"])
    assert restored.session(session["id"]) == store.session(session["id"])
    with pytest.raises(ValueError):
        restore(archive, tmp_path / "restored")


def test_backup_restore_preserves_story_assets_after_card_deletion(tmp_path):
    store = Store(tmp_path / "source")
    card = store.import_card(Path("samples/雾港来信.json").read_bytes(), "card.json")
    session = store.new_session(SessionCreate(card_id=card["id"]))
    store.delete_card(card["id"])
    archive = tmp_path / "backup.zip"
    backup(store, archive)
    restored = restore(archive, tmp_path / "restored")
    assert restored.cards() == []
    assert restored.session_card(session["id"]) == store.session_card(session["id"])
    assert (restored.root / "blobs" / card["original"]).read_bytes() == (store.root / "blobs" / card["original"]).read_bytes()
