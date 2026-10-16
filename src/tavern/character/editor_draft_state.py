"""Private draft comparison values and public, semantic change notifications."""
from tavern.core.cache_policy import fingerprint
from tavern.core.model_view import hidden_field


def draft_versions(cards):
    result = {}
    for card in cards:
        document = card["document"]
        wrapped = bool(document.get("spec")) and isinstance(document.get("data"), dict)
        fields = document["data"] if wrapped else document
        prefix = "/data/" if wrapped else "/"
        result[card["ref"]] = {"document": fingerprint(document), "fields": {
            prefix + str(key).replace("~", "~0").replace("/", "~1"): fingerprint(value)
            for key, value in fields.items() if not hidden_field(str(key))}}
    return result


def draft_changes(previous, current):
    """Hashes stay in the ledger; only card handles and JSON pointers reach the model."""
    if previous is None:
        # Older ledgers have the overview but no per-card versions. Conservatively
        # announce refreshed references once, without dropping their old messages.
        return {"updated": [{"card_ref": ref, "fields": [""]} for ref in current], "added": [], "removed": []}
    updated = []
    for ref, value in current.items():
        old = previous.get(ref)
        if old and old["document"] != value["document"]:
            paths = sorted(path for path in old["fields"].keys() | value["fields"].keys()
                           if old["fields"].get(path) != value["fields"].get(path))
            updated.append({"card_ref": ref, "fields": paths or [""]})
    return {"updated": updated, "added": [ref for ref in current if ref not in previous],
            "removed": [ref for ref in previous if ref not in current]}
