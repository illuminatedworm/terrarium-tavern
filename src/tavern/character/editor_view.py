"""Read-only projection for the Card Author workspace in the browser."""


def same_json(left, right):
    if left is right:
        return True
    if type(left) is not type(right):
        return False  # A JSON boolean and a number can compare equal in Python.
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(
            same_json(value, right[key]) for key, value in left.items()
        )
    if isinstance(left, list):
        return len(left) == len(right) and all(same_json(a, b) for a, b in zip(left, right))
    return left == right


def reader_cards(cards):
    for card in cards:
        # The form edits document, and reviews against base_document.
        # Source metadata is still needed for portraits, resources and export.
        card["source"].pop("document", None)
        if same_json(card["base_document"], card["document"]):
            del card["base_document"]


def workspace_view(workspace, reader):
    if not reader:
        return workspace
    pending = workspace["pending"]
    reader_cards(workspace["cards"])
    if pending:
        reader_cards(pending["cards"])
    runs = []
    for run in workspace["runs"]:
        item = {key: run[key] for key in ("id", "status", "error")}
        if pending and run["id"] == pending["run"]:
            item["request"] = {
                key: run["request"][key] for key in ("target_refs", "skill") if key in run["request"]
            }
        runs.append(item)
    workspace["runs"] = runs
    return workspace
