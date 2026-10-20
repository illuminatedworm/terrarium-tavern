"""Image-only declarations and lossless HTML attribute rewriting, never script rewriting."""

import re
from html import escape, unescape
from html.parser import HTMLParser
from urllib.parse import unquote, urlsplit

TEMPLATE = re.compile(r"\$(?:[1-9]\d?|<[^>]+>|&|\{[^}]+\})|\{\{[^}]+\}\}")
ATTR = re.compile(r"""(?<![\w:-])([a-z_:][\w:.-]*)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""", re.I)
CSS_IMAGE = re.compile(r"""url\(\s*(["']?)(.*?)\1\s*\)""", re.I)


def declared_image(url, declarations):
    """Allow exact declarations, expanded templates, or children of declared URL bases.

    Decode before matching so escaped slashes/dot segments cannot escape a declared
    path. A base is a directory-like URL, not an arbitrary prefix of a host/file.
    """
    decoded = unquote(url)
    parsed = urlsplit(decoded)
    if (
        parsed.scheme != "https"
        or parsed.username
        or parsed.password
        or parsed.fragment
        or TEMPLATE.search(decoded)
    ):
        return False
    if (
        any(part in (".", "..") for part in parsed.path.split("/"))
        or "\\" in decoded
        or re.search(r"%(?:2e|2f|5c)", decoded, re.I)
    ):
        return False
    for declaration in declarations:
        declaration = unquote(declaration)
        if TEMPLATE.search(declaration):
            # Captures can contain a complete relative image path, not just one
            # segment. The host stays literal and dot segments remain forbidden.
            if urlsplit(declaration).netloc != parsed.netloc:
                continue
            pattern = "".join(
                "[^?#]+" if TEMPLATE.fullmatch(part) else re.escape(part)
                for part in re.split(f"({TEMPLATE.pattern})", declaration)
                if part
            )
            if re.fullmatch(pattern, decoded):
                return True
        elif decoded == declaration:
            return True
        else:
            base = urlsplit(declaration)
            if (
                base.scheme == parsed.scheme
                and base.netloc == parsed.netloc
                and not base.query
                and not base.fragment
                and not re.search(
                    r"\.(?:js|mjs|css|png|jpe?g|webp|gif|svg|ico|woff2?|ttf|otf|eot)$", base.path, re.I
                )
                and parsed.path.startswith(base.path.rstrip("/") + "/")
            ):
                return True
    return False


def rewrite_markup(html, replace, *, images_only=False):
    """Replace resource attributes in real tags while retaining all other source bytes.

    HTMLParser skips script contents, comments, and strings containing sample HTML.
    Replacements run synchronously; callers may first collect URLs, then fetch them.
    """
    offsets = [0]
    offsets.extend(match.end() for match in re.finditer("\n", html))
    edits = []

    def css_value(match):
        value = replace(match[2])
        return match[0] if value == match[2] else f'url("{value}")'

    class Parser(HTMLParser):
        in_style = False

        def handle_starttag(self, tag, attrs):
            if tag == "style":
                self.in_style = True
            line, column = self.getpos()
            start = offsets[line - 1] + column
            raw = self.get_starttag_text()

            def attribute(match):
                name = match[1].lower()
                value = unescape(next(v for v in match.groups()[1:] if v is not None))
                image = (
                    tag == "img"
                    and name in ("src", "srcset")
                    or tag == "source"
                    and name == "srcset"
                    or tag == "image"
                    and name in ("href", "xlink:href")
                    or name == "poster"
                )
                if name == "style":
                    result = CSS_IMAGE.sub(css_value, value)
                elif image and name == "srcset":
                    # Data URLs contain commas and are already self-contained.
                    if value.lstrip().startswith("data:"):
                        return match[0]
                    result = ", ".join(
                        " ".join([replace(parts[0]), *parts[1:]])
                        for item in value.split(",")
                        if (parts := item.split())
                    )
                elif image or not images_only and name in ("src", "poster"):
                    result = replace(value)
                else:
                    return match[0]
                return match[0] if result == value else f'{match[1]}="{escape(result, quote=True)}"'

            updated = ATTR.sub(attribute, raw)
            if updated != raw:
                edits.append((start, start + len(raw), updated))

        handle_startendtag = handle_starttag

        def handle_endtag(self, tag):
            if tag == "style":
                self.in_style = False

        def handle_data(self, data):
            if self.in_style:
                updated = CSS_IMAGE.sub(css_value, data)
                if updated != data:
                    line, column = self.getpos()
                    start = offsets[line - 1] + column
                    edits.append((start, start + len(data), updated))

    Parser(convert_charrefs=False).feed(html)
    for start, end, value in reversed(edits):
        html = html[:start] + value + html[end:]
    return html
