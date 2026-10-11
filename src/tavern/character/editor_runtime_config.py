"""The same operation-local choices drive validation, preview and execution."""
from tavern.core.message_adapter import bind_adapter


def editor_runtime_config(store, wid, options):
    cfg = store.settings().for_role("card_editor")
    for key in ("model", "reasoning_effort"):
        if options.get(key) is not None:
            setattr(cfg, key, options[key])
    return bind_adapter(cfg, store, "editor", wid)
