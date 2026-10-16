"""Documented Gemini model controls, independent of the chosen client library."""
import re


def gemini_controls(api: str, model: str) -> tuple[bool, bool]:
    """Return (sampling, explicit_cache); unknown aliases retain legacy controls.

    Google deprecated sampling starting with 3.6 Flash / 3.5 Flash-Lite and
    explicit cachedContents starting with Gemini 4. Keep this in sync with
    web/lib/geminiCapabilities.ts and the protocol regression cases.
    """
    if api not in ("gemini", "chat_completions"):
        return True, True
    name = model.lower()
    match = re.search(r"(?:^|/)gemini-(\d+)(?:\.(\d+))?(?=-|$)", name)
    if not match:
        return True, True
    major, minor = int(match[1]), int(match[2] or 0)
    sampling = not (major >= 4 or major == 3 and (minor >= 6 or
                    minor == 5 and re.search(r"gemini-3\.5-flash-lite(?=-|$)", name)))
    return sampling, major < 4
