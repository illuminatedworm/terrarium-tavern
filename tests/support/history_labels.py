"""Strip an optional runtime ``[H52]`` prefix from captured test fixtures.

Full Agent still adds these prefixes; Writer no longer does. This fixture-only
helper must not be used to process user or assistant prose in production.
"""

import re

_LABEL = re.compile(r"\A\[H[1-9]\d*\]\n")


def strip_label(text: str) -> str:
    return _LABEL.sub("", text, count=1)
