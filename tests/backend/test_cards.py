import base64
import copy
import io
import json
import zipfile

import pytest
from PIL import Image, PngImagePlugin

from tavern.character.cards import export_card, lore_entries, parse_card


def document(name="角色"):
    return {
        "spec": "chara_card_v3",
        "spec_version": "3.0",
        "data": {"name": name, "extensions": {"unknown": [1, {"中文": True}]}},
    }


def test_json_unknown_roundtrip():
    d = document()
    raw = json.dumps(d).encode()
    parsed, kind, assets, _ = parse_card(raw, "card.json")
    out, _, _ = export_card(parsed, kind, raw, assets)
    assert json.loads(out) == d


def test_export_canonicalizes_lore_flags_without_changing_saved_source():
    original = document()
    original["data"]["character_book"] = {
        "name": "世界书",
        "entries": {
            "mixed": {
                "content": "完整设定",
                "matchWholeWords": True,
                "match_whole_words": False,
                "excludeRecursion": False,
                "exclude_recursion": True,
                "prevent_recursion": True,
                "case_sensitive": True,
                "extensions": {
                    "matchWholeWords": False,
                    "match_whole_words": True,
                    "exclude_recursion": True,
                    "prevent_recursion": False,
                    "caseSensitive": None,
                    "case_sensitive": True,
                    "unknown": {"keep": [1, 2]},
                },
            },
            "top": {"content": "顶层旧字段", "matchWholeWords": True, "case_sensitive": None},
            "plain": {"content": "未配置开关"},
        },
    }
    raw = json.dumps(original).encode()
    parsed, kind, assets, _ = parse_card(raw, "card.json")
    out, _, _ = export_card(parsed, kind, raw, assets)
    exported = json.loads(out)
    expected = copy.deepcopy(original)
    expected["data"]["character_book"]["entries"] = {
        "mixed": {
            "content": "完整设定",
            "extensions": {
                "match_whole_words": False,
                "exclude_recursion": False,
                "prevent_recursion": False,
                "case_sensitive": None,
                "unknown": {"keep": [1, 2]},
            },
        },
        "top": {"content": "顶层旧字段", "extensions": {"match_whole_words": True, "case_sensitive": None}},
        "plain": {"content": "未配置开关"},
    }
    assert exported == expected
    assert parsed == original
    for before, after in zip(lore_entries(parsed), lore_entries(exported), strict=True):
        for field in ("matchWholeWords", "excludeRecursion", "preventRecursion", "caseSensitive"):
            assert (field in before, before.get(field)) == (field in after, after.get(field))
    assert export_card(exported, kind, out, assets)[0] == out


def test_png_prefers_v3_and_preserves_other_chunks():
    info = PngImagePlugin.PngInfo()
    info.add_text("chara", base64.b64encode(json.dumps({"name": "old"}).encode()).decode())
    info.add_text("ccv3", base64.b64encode(json.dumps(document()).encode()).decode())
    info.add_text("unrelated", "preserved")
    buf = io.BytesIO()
    Image.new("RGB", (2, 2)).save(buf, "PNG", pnginfo=info)
    raw = buf.getvalue()
    parsed, kind, assets, _ = parse_card(raw, "card.png")
    assert parsed["data"]["name"] == "角色"
    parsed["data"]["name"] = "edited"
    out, _, _ = export_card(parsed, kind, raw, assets)
    assert Image.open(io.BytesIO(out)).text["unrelated"] == "preserved"
    assert parse_card(out, "card.png")[0] == parsed


def test_charx_assets_and_path_escape():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("card.json", json.dumps(document()))
        z.writestr("assets/a.txt", "hello")
    raw = buf.getvalue()
    parsed, kind, assets, _ = parse_card(raw, "card.charx")
    assert assets["assets/a.txt"] == b"hello"
    out, _, _ = export_card(parsed, kind, raw, assets)
    assert parse_card(out, "card.charx")[2] == assets
    with zipfile.ZipFile(buf, "a") as z:
        z.writestr("../evil", "bad")
    with pytest.raises(ValueError, match="非法"):
        parse_card(buf.getvalue(), "evil.charx")
