"""Strip the runtime ``[H52]`` position line from captured request text.

Production compares the exact prefix it recorded (``history_label``), so a
user's literal leading ``[H5]`` line stays content. Tests only need to read
labeled user messages back as their original text.
"""

import re

_LABEL = re.compile(r"\A\[H[1-9]\d*\]\n")


def strip_label(text: str) -> str:
    return _LABEL.sub("", text, count=1)
