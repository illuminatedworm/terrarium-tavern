"""Expand story macros for memory requests without modifying saved messages."""

import json
import re

from tavern.character.cards import normalize
from tavern.conversation.context import macros


def parse_json(text):
    return json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip()))


def archive_history(history, document=None, persona=None):
    """Expand a request copy while keeping persisted text and branch evidence intact."""
    if document is None or persona is None:
        return history
    result = []
    for message in history:
        snapshot = message.get("snapshot", {})
        source_document = (snapshot.get("content") or {}).get("document", document)
        text = macros(
            message.get("model", message["raw"]),
            normalize(source_document),
            persona,
            snapshot.get("state", {}),
            snapshot.get("scopes"),
        )
        result.append({**message, "model": text})
    return result
