"""Prepare an opening in isolation; callers own the database transaction."""

from tavern.character.cards import regex_scripts


async def prepare_opening(runtime, dependencies, session, card, greeting):
    snapshot = greeting["snapshot"]
    # Mounted books and manual conversation-card edits are the effective source.
    document = (snapshot.get("content") or {}).get("document", card["document"])
    assets = {**card, "document": document}
    if card["permission"]:
        await dependencies.resolve(assets)

    async def host(method, args):
        if method == "log":
            return None
        raise ValueError("开场初始化时不允许发起模型请求")

    script = await runtime.open(
        document,
        [greeting],
        snapshot,
        bool(card["permission"]),
        host,
        dependencies.scripts(assets),
        lambda html: dependencies.markup(assets, html),
        persona=session["persona"],
        opening=True,
    )
    try:
        mirror = await script.call("snapshot")
        snapshot.update(state=mirror["state"], scopes=mirror["scopes"])
        rules = regex_scripts(document)
        # Preserve each engine's existing evaluation order for scripted regexes.
        channels = ("model", "display") if session["agent_mode"] == "full_agent" else ("display", "model")
        for channel in channels:
            value = await script.call("transform", greeting["raw"], rules, 2, channel, 0)
            if channel == "display" and card["permission"] and "<" in value:
                value = dependencies.markup(assets, value)
            greeting[channel] = value
        return greeting
    finally:
        await script.close()
