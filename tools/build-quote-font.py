"""Build original curved display glyphs for U+2018/U+2019/U+201C/U+201D.

Optional build tool: fonttools 4.61.1. The checked-in TTF needs no runtime dependency.
These geometric outlines are original project assets, not extracted from a font.
Text values and clipboard content retain their original Unicode characters.
"""

from pathlib import Path

from fontTools.fontBuilder import FontBuilder
from fontTools.pens.ttGlyphPen import TTGlyphPen


def quotation(offsets, *, closing=False):
    """Draw one or two rounded comma heads with visibly curved opening tails."""
    pen = TTGlyphPen(None)

    for offset in offsets:

        def point(x, y):
            # Rotate closing marks so their heads sit above the descending tails.
            if closing:
                x, y = 340 - x, 1370 - y
            return x + offset, y

        pen.moveTo(point(270, 840))
        pen.qCurveTo(point(70, 810), point(70, 620))
        pen.qCurveTo(point(70, 530), point(155, 530))
        pen.qCurveTo(point(240, 530), point(240, 615))
        pen.qCurveTo(point(240, 700), point(155, 700))
        pen.qCurveTo(point(134, 700), point(120, 692))
        pen.qCurveTo(point(140, 765), point(270, 780))
        pen.closePath()
    return pen.glyph()


glyphs = {
    ".notdef": quotation([]),
    "openSingle": quotation([0]),
    "closeSingle": quotation([0], closing=True),
    "openDouble": quotation([0, 260]),
    "closeDouble": quotation([0, 260], closing=True),
}
builder = FontBuilder(1000, isTTF=True)
builder.setupGlyphOrder(list(glyphs))
builder.setupCharacterMap(
    {
        0x2018: "openSingle",
        0x2019: "closeSingle",
        0x201C: "openDouble",
        0x201D: "closeDouble",
    }
)
builder.setupGlyf(glyphs)
builder.setupHorizontalMetrics(
    {name: (340 if name.endswith("Single") else 600, 0 if name == ".notdef" else 70) for name in glyphs}
)
builder.setupHorizontalHeader(ascent=880, descent=-120)
builder.setupNameTable(
    {
        "familyName": "Tavern Quote Marks",
        "styleName": "Regular",
        "uniqueFontIdentifier": "TavernQuoteMarks-2",
        "fullName": "Tavern Quote Marks",
        "psName": "TavernQuoteMarks",
        "version": "Version 2.0",
    }
)
builder.setupOS2(sTypoAscender=880, sTypoDescender=-120, usWinAscent=880, usWinDescent=120)
builder.setupPost()
builder.setupMaxp()
target = Path(__file__).resolve().parents[1] / "web/assets/quote-marks.ttf"
target.parent.mkdir(parents=True, exist_ok=True)
builder.save(target)
