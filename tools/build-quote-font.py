"""Build original corner-shaped display glyphs for U+201C/U+201D only.

Optional build tool: fonttools 4.61.1. The checked-in TTF needs no runtime dependency.
These geometric outlines are original project assets, not extracted from a font.
Text values and clipboard content retain their original Unicode characters.
"""
from pathlib import Path

from fontTools.fontBuilder import FontBuilder
from fontTools.pens.ttGlyphPen import TTGlyphPen


def outline(points):
    pen = TTGlyphPen(None)
    if points:
        pen.moveTo(points[0])
        for point in points[1:]:
            pen.lineTo(point)
        pen.closePath()
    return pen.glyph()


builder = FontBuilder(1000, isTTF=True)
builder.setupGlyphOrder([".notdef", "open", "close"])
builder.setupCharacterMap({0x201C: "open", 0x201D: "close"})
builder.setupGlyf({
    ".notdef": outline([]),
    "open": outline([(180, 430), (250, 430), (250, 750), (530, 750), (530, 820), (180, 820)]),
    "close": outline([(70, -80), (420, -80), (420, 310), (350, 310), (350, -10), (70, -10)]),
})
builder.setupHorizontalMetrics({name: (600, 0 if name == ".notdef" else 180 if name == "open" else 70)
                                for name in [".notdef", "open", "close"]})
builder.setupHorizontalHeader(ascent=880, descent=-120)
builder.setupNameTable({"familyName": "Tavern Quote Marks", "styleName": "Regular",
                        "uniqueFontIdentifier": "TavernQuoteMarks-1", "fullName": "Tavern Quote Marks",
                        "psName": "TavernQuoteMarks", "version": "Version 1.0"})
builder.setupOS2(sTypoAscender=880, sTypoDescender=-120, usWinAscent=880, usWinDescent=120)
builder.setupPost()
builder.setupMaxp()
target = Path(__file__).resolve().parents[1] / "web/assets/quote-marks.ttf"
target.parent.mkdir(parents=True, exist_ok=True)
builder.save(target)
