"""The user's out-of-character (co-author) text inside one user message.

Story text goes through card regex scripts first; the Runtime then appends the
out-of-character part in a fixed tag, so card layers never see or rewrite it
and every model-facing reader of a user message gets the same form.
"""


def ooc_block(ooc: str) -> str:
    return f"<ooc>\n{ooc}\n</ooc>"


def with_ooc(text: str, ooc: str) -> str:
    if not ooc:
        return text
    return f"{text}\n\n{ooc_block(ooc)}" if text.strip() else ooc_block(ooc)


def story_of(text: str, ooc: str) -> str:
    """Undo with_ooc for the same ooc value; the suffix is known, nothing is parsed."""
    if not ooc:
        return text
    block = ooc_block(ooc)
    return "" if text == block else text.removesuffix("\n\n" + block)
