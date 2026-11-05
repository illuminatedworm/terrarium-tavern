"""Whole-message display rules take precedence over delivery ornaments."""

from tavern.core.story_segments import join_segments, split_segments


async def transform_display(script, text, rules, *, breaks=(), placement=2, markup=None):
    display = await script.call("transform", text, rules, placement, "display", 0)
    # Wrappers and cross-segment replacements own the whole reply. Splitting
    # their result at old offsets would break markup or repeat card interfaces.
    boundaries = breaks if display == text else ()
    parts = []
    for part in split_segments(display, boundaries):
        if markup is not None and "<" in part:
            part = markup(part)
        parts.append(part)
    return join_segments(parts)
