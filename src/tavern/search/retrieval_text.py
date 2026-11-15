"""Assemble the user query and the latest retrieval clues."""


def continues(text):
    return text.strip().lower() in ("", "继续", "继续剧情", "continue", "続けて")


def query_input(story, ooc):
    return "\n".join(filter(None, [story, ooc]))


def query_text(history, user_input, config, story=None):
    """`user_input` may append the user's ooc text; only the story part decides the fallback."""
    clues = [m.get("retrieval_query", "") for m in history if m.get("retrieval_query")]
    clues = clues[-config.query_lookback :] if config.query_lookback else []
    current = user_input
    fallback = ""
    if not clues and continues(user_input if story is None else story) and history:
        from tavern.search.retrieval import tokenize

        fallback = " ".join(dict.fromkeys(tokenize(history[-1].get("model", history[-1].get("raw", "")))))
    return "\n".join(filter(None, [*clues, fallback, current])).strip()
