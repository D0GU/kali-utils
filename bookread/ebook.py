"""Read an EPUB into paragraphs of plain text with bold and italic marked, plus its contents list.

Positions are counted in words through the whole book, because a word stays in place when the window changes size
and a page doesn't. Standard library only, so the parser tolerates what books really contain rather than demanding
valid XML.
"""

from __future__ import annotations

import html
import posixpath
import re
import unicodedata
import urllib.parse
import xml.etree.ElementTree as ET
import zipfile
from array import array
from bisect import bisect_right
from collections import Counter
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import NamedTuple

from kutil.text import clean_text

BOLD, ITALIC = 1, 2
MAX_PACKAGE = 4 * 1024 ** 2          # the package document and contents list
MAX_DOCUMENT = 64 * 1024 ** 2       # one chapter, unpacked
MAX_BOOK = 1024 ** 3                # all the text, unpacked
SCENE_BREAK = "* * *"
ORNAMENT_USES = 3                   # one picture used this often is decoration, not an illustration
TITLE_WORDS = 12                    # the longest bold line taken for a chapter title

BLOCK_TAGS = {"p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li", "ul", "ol", "dl", "dt", "dd", "blockquote", "pre", "table", "tr",
              "section", "article", "aside", "header", "footer", "figure", "figcaption", "nav", "main", "center", "address", "body"}
HIDDEN_TAGS = {"head", "title", "script", "style", "template"}
VOID_TAGS = {"br", "hr", "img", "image", "meta", "link", "input", "wbr", "col", "area", "base", "embed", "source", "track"}
TAG_STYLES = {"b": BOLD, "strong": BOLD, "i": ITALIC, "em": ITALIC, "cite": ITALIC, "dfn": ITALIC, "var": ITALIC}

_SPACE = re.compile(r"\s+")
_CJK = "⺀-⿿、-꓏가-힣豈-﫿＀-￯\U00020000-\U0003ffff"
# Each CJK character can end a line, which is how those scripts wrap, so each counts as one here.
_WORD = re.compile(rf"[{_CJK}]|[^\s{_CJK}]+")
_PLAIN_WORD = re.compile(r"\S+")
_HAS_CJK = re.compile(rf"[{_CJK}]")
_IMAGE_TAG = re.compile(r"<(?:img|image)\b[^>]*>", re.I)
_IMAGE_SRC = re.compile(r"""\b(?:src|xlink:href|href)\s*=\s*["']([^"']+)["']""", re.I)
_META_CHARSET = re.compile(rb"""<meta[^>]*charset\s*=\s*["']?([\w.-]+)""", re.I)
_XML_DECLARATION = re.compile(r"^\s*<\?xml.*?\?>", re.S)
_JUST_AN_IMAGE = re.compile(r"\[image(: .*)?\]")
_ENCODING = re.compile(rb"""encoding\s*=\s*["']([\w.-]+)["']""")
_CSS_COMMENT = re.compile(r"/\*.*?\*/", re.S)
_CSS_MEDIA = re.compile(r"@media[^{]*\{(?:[^{}]*\{[^{}]*\})*[^{}]*\}")
_CSS_RULE = re.compile(r"([^{}]+)\{([^{}]*)\}")
_CSS_CLASS = re.compile(r"^[\w-]*\.([\w-]+)$")
_CSS_STYLE = re.compile(r"font-style\s*:\s*([\w-]+)", re.I)
_CSS_WEIGHT = re.compile(r"font-weight\s*:\s*([\w-]+)", re.I)
_CSS_DISPLAY = re.compile(r"display\s*:\s*([\w-]+)", re.I)


_NOT_TEXT = re.compile(r"<(head|script|style|template)\b.*?</\1\s*>", re.S | re.I)
_BLOCK_TAG = re.compile(rf"</?(?:{'|'.join(sorted(BLOCK_TAGS | {'br', 'hr', 'td', 'th'}))})\b[^>]*>", re.I)
_INLINE_TAG = re.compile(r"<[^>]*>")


class EbookError(Exception):
    pass



def parse_xml(data: bytes) -> ET.Element:
    """Python's XML parser refuses multi-byte encodings like gb2312 and shift_jis, so those are decoded here first."""
    try:
        return ET.fromstring(data)
    except (ValueError, LookupError):
        return ET.fromstring(_XML_DECLARATION.sub("", _decode(data), count=1))


def package_path(z: zipfile.ZipFile) -> str:
    """Where the zip keeps its package document."""
    for e in parse_xml(z.read("META-INF/container.xml")).iter():
        if e.tag.rsplit("}", 1)[-1].lower() == "rootfile" and e.get("full-path"):
            return e.get("full-path")
    raise EbookError("this file has no package document")


def cell_width(text: str) -> int:
    """Terminal cells this text takes up."""
    if text.isascii():
        return len(text)
    width = 0
    for c in text:
        if unicodedata.category(c) in ("Mn", "Me"):     # marks drawn on the character before
            continue
        width += 2 if unicodedata.east_asian_width(c) in "WF" else 1
    return width


@dataclass(slots=True)
class Block:
    text: str
    kind: str = "p"             # p, h (heading) or break (scene break)
    level: int = 0              # 1-6 for a heading
    indent: int = 0
    tight: bool = False         # follows the block before with no blank line
    new_page: bool = False      # first block of a file
    word: int = 0               # number of this block's first word in the book
    spans: list = field(default_factory=list)   # [(start, end, BOLD | ITALIC)]
    starts: array | None = None     # offset of each word in text
    ends: array | None = None
    widths: array | None = None     # cells each word takes


@dataclass(slots=True)
class Chapter:
    title: str
    word: int
    depth: int = 0


@dataclass
class Book:
    blocks: list[Block]
    chapters: list[Chapter]
    words: int

    def __post_init__(self):
        self._chapter_words = [c.word for c in self.chapters]

    def chapter_at(self, word: int) -> Chapter | None:
        i = bisect_right(self._chapter_words, word) - 1
        return self.chapters[i] if i >= 0 else None

    def chapter_end(self, word: int) -> int:
        """Where the chapter holding this word stops."""
        i = bisect_right(self._chapter_words, word)
        return self._chapter_words[i] if i < len(self._chapter_words) else self.words


def _style_of(declarations: str, current: int = 0) -> int:
    m = _CSS_STYLE.search(declarations)
    if m:
        current = current | ITALIC if m.group(1).lower() in ("italic", "oblique") else current & ~ITALIC
    m = _CSS_WEIGHT.search(declarations)
    if m:
        value = m.group(1).lower()
        current = current | BOLD if value in ("bold", "bolder", "600", "700", "800", "900") else current & ~BOLD
    return current


def _is_hidden(declarations: str, current: bool = False) -> bool:
    m = _CSS_DISPLAY.search(declarations)
    return m.group(1).lower() == "none" if m else current


def parse_css(text: str, classes: dict[str, tuple[int, bool]]) -> None:
    """Add what single-class rules say about bold, italic and hiding. Rules for print or small screens don't apply."""
    text = _CSS_MEDIA.sub("", _CSS_COMMENT.sub("", text))
    for selectors, body in _CSS_RULE.findall(text):
        for selector in selectors.split(","):
            m = _CSS_CLASS.match(selector.strip())
            if m:
                style, hidden = classes.get(m.group(1), (0, False))
                classes[m.group(1)] = (_style_of(body, style), _is_hidden(body, hidden))


def _list_start(attrs: dict) -> int:
    """The count before an ordered list's first item."""
    try:
        return int(attrs.get("start", "1")) - 1
    except ValueError:
        return 0


class _Open(NamedTuple):
    """A tag that hasn't closed yet."""
    tag: str
    style: int = 0
    hidden: bool = False
    level: int = 0                # heading level
    indents: bool = False
    numbers: list | None = None   # [items so far], for an ordered list


class _Parser(HTMLParser):
    def __init__(self, classes: dict[str, tuple[int, bool]], ornaments: set[str]):
        super().__init__(convert_charrefs=True)
        self.classes = classes
        self.ornaments = ornaments
        self.blocks: list[Block] = []
        self.word = 0
        self.anchors: dict[tuple[str, str], int] = {}
        self.file_words: dict[str, int] = {}
        self._reset_file("")

    def _reset_file(self, path: str) -> None:
        self.path = path
        self.stack: list[_Open] = []
        self.pieces: list[tuple[str, int]] = []
        self.style = 0
        self.hidden = 0
        self.level = 0
        self.tight = False
        self.fresh = True
        self.marker = ""
        self.ornament_waiting = False   # an ornament with no text so far in its block
        self.ornament_break: Block | None = None

    def read(self, path: str, text: str) -> None:
        self._reset_file(path)
        self.file_words.setdefault(path, self.word)
        self.reset()
        self.feed(text)
        self.close()
        self.flush()
        if self.blocks and self.blocks[-1].kind == "break":     # at the end of a file it separates nothing
            self.blocks.pop()

    # -- blocks ----------------------------------------------------------------

    def _indent(self) -> int:
        return 2 * sum(1 for e in self.stack if e.indents)

    def _in_pre(self) -> bool:
        return any(e.tag == "pre" for e in self.stack)

    def _joined(self) -> tuple[str, list]:
        """The buffered pieces as one line of text with single spaces, and where the styles fall in it."""
        out = ""
        spans: list = []
        for raw, style in self.pieces:
            piece = _SPACE.sub(" ", clean_text(raw))
            if not out or out.endswith(" "):
                piece = piece.lstrip(" ")
            if not piece:
                continue
            if style:
                if spans and spans[-1][1] == len(out) and spans[-1][2] == style:
                    spans[-1] = (spans[-1][0], len(out) + len(piece), style)
                else:
                    spans.append((len(out), len(out) + len(piece), style))
            out += piece
        text = out.rstrip(" ")
        return text, [(s, min(e, len(text)), st) for s, e, st in spans if s < len(text)]

    def flush(self) -> bool:
        """End the current block. True if it held any text."""
        ornament, self.ornament_waiting = self.ornament_waiting, False
        if self._in_pre():
            raw = clean_text("".join(p for p, _ in self.pieces)).expandtabs(4).rstrip()
            text = raw.lstrip(" ")
            spans: list = []
            extra = len(raw) - len(text)
        else:
            text, spans = self._joined()
            extra = 0
        self.pieces = []
        if not text:
            if ornament:
                self._ornament_scene_break()
            return False
        if self.marker:
            spans = [(s + len(self.marker), e + len(self.marker), st) for s, e, st in spans]
            text = self.marker + text
            self.marker = ""
        self._emit(Block(text, "h" if self.level else "p", self.level, self._indent() + extra, self.tight, spans=spans))
        return True

    def _ornament_scene_break(self) -> None:
        """Decoration between paragraphs marks a scene break. At the start of a file or after a heading it marks nothing."""
        if self.fresh or not self.blocks or self.blocks[-1].kind in ("h", "break"):
            return
        self.ornament_break = Block(SCENE_BREAK, "break")
        self._emit(self.ornament_break)

    def _drop_ornament_break(self) -> None:
        if self.blocks and self.blocks[-1] is self.ornament_break:
            self.blocks.pop()

    def _emit(self, block: Block) -> None:
        if block.kind == "h":
            self._drop_ornament_break()
        block.new_page, self.fresh = self.fresh, False
        block.word = self.word
        text = block.text
        if block.kind == "break":
            found = []
        else:
            found = [m.span() for m in (_WORD if _HAS_CJK.search(text) else _PLAIN_WORD).finditer(text)]
        block.starts = array("I", [s for s, _ in found])
        block.ends = array("I", [e for _, e in found])
        block.widths = array("I", [e - s for s, e in found] if text.isascii() else [cell_width(text[s:e]) for s, e in found])
        self.word += len(found)
        self.tight = False
        self.blocks.append(block)

    # -- tags ------------------------------------------------------------------

    def _recompute(self) -> None:
        self.style = 0
        self.level = 0
        self.hidden = 0
        for e in self.stack:
            self.style |= e.style
            self.hidden += e.hidden
            self.level = e.level or self.level

    def _close_open(self, targets: tuple, scope: tuple) -> None:
        """Close the nearest open tag in `targets`, unless a tag in `scope` stands in between."""
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i].tag in targets:
                del self.stack[i:]
                self._recompute()
                return
            if self.stack[i].tag in scope:
                return

    def _close_implied(self, tag: str) -> None:
        """HTML lets some end tags go unwritten, and without this an unclosed <p> or <li> nests for the rest of the file."""
        if tag == "body":
            self._close_open(("head",), ())
        if self.hidden:
            return
        if tag in BLOCK_TAGS:
            self._close_open(("p",), ("table", "td", "th"))
        if tag == "p":
            self._close_open(("h1", "h2", "h3", "h4", "h5", "h6"), ("div", "section", "article", "blockquote", "li", "td", "th", "table", "body"))
        elif tag == "li":
            self._close_open(("li",), ("ul", "ol"))
        elif tag in ("dt", "dd"):
            self._close_open(("dt", "dd"), ("dl",))
        elif tag == "tr":
            self._close_open(("tr",), ("table",))
        elif tag in ("td", "th"):
            self._close_open(("td", "th"), ("tr", "table"))

    def handle_starttag(self, tag, attrs):
        tag = tag.rsplit(":", 1)[-1]
        attrs = {k: v or "" for k, v in attrs}
        block = tag in BLOCK_TAGS
        if block and not self.hidden:
            self.flush()
            self.tight = False
        self._close_implied(tag)
        anchor = attrs.get("id") or (attrs.get("name") if tag == "a" else "")
        if anchor:
            pending = sum(len(_WORD.findall(p)) for p, _ in self.pieces)
            self.anchors.setdefault((self.path, anchor), self.word + pending)
        if self.hidden:
            if tag not in VOID_TAGS:
                self.stack.append(_Open(tag))
            return
        if tag in VOID_TAGS:
            return self._void(tag, attrs)
        style, hidden = TAG_STYLES.get(tag, 0), tag in HIDDEN_TAGS
        for name in attrs.get("class", "").split():
            cls_style, cls_hidden = self.classes.get(name, (0, False))
            style, hidden = style | cls_style, hidden or cls_hidden
        if attrs.get("style"):
            style, hidden = _style_of(attrs["style"], style), _is_hidden(attrs["style"], hidden)
        level = int(tag[1]) if len(tag) == 2 and tag[0] == "h" and tag[1] in "123456" else 0
        if tag == "li":
            parent = next((e for e in reversed(self.stack) if e.tag in ("ol", "ul")), None)
            if parent and parent.numbers is not None:
                parent.numbers[0] += 1
                self.marker = f"{parent.numbers[0]}. "
            else:
                self.marker = "• "
        if tag in ("td", "th") and self.pieces:
            self.pieces.append((" · ", 0))
        self.stack.append(_Open(tag, style, hidden, level, tag in ("blockquote", "li"), [_list_start(attrs)] if tag == "ol" else None))
        self._recompute()

    def _void(self, tag: str, attrs: dict) -> None:
        if tag == "br":
            if self.level:      # a title set on two lines is still one title
                self.pieces.append((" ", self.style))
            else:
                self.tight = self.flush()   # so a second <br> in a row, with nothing to end, leaves a gap
        elif tag == "hr":
            self.flush()
            self._emit(Block(SCENE_BREAK, "break"))
        elif tag in ("img", "image"):
            src = attrs.get("src") or attrs.get("xlink:href") or attrs.get("href") or ""
            if src and _resolve(self.path, src)[0] in self.ornaments:
                self.ornament_waiting = self.ornament_waiting or not "".join(p for p, _ in self.pieces).strip()
                return
            alt = _SPACE.sub(" ", attrs.get("alt", "")).strip()
            self.pieces.append((f"[image: {alt}]" if alt else "[image]", 0))

    def handle_endtag(self, tag):
        tag = tag.rsplit(":", 1)[-1]
        if tag not in (e.tag for e in self.stack):
            return
        if not self.hidden and tag in BLOCK_TAGS:
            self.flush()
            self.tight = False
        if tag in ("li", "ul", "ol"):
            self.marker = ""    # an empty item's bullet mustn't land on whatever comes next
        while self.stack and self.stack.pop().tag != tag:
            pass
        self._recompute()

    def handle_data(self, data):
        if self.stack and self.stack[-1].tag == "style":
            parse_css(data, self.classes)
        if self.hidden:
            return
        if not self._in_pre():
            self.pieces.append((data, self.style))
            return
        lines = data.split("\n")
        for i, line in enumerate(lines):
            if i:
                self.tight = self.flush()
            self.pieces.append((line, 0))

    def unknown_decl(self, data):
        if data.startswith("CDATA[") and not self.hidden:
            self.handle_data(data[6:])


class _NavParser(HTMLParser):
    """Entries of an EPUB 3 contents page."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.lists: list[list] = []
        self.current: list | None = None
        self.depth = 0
        self.link: str | None = None
        self.label: list[str] = []
        self.toc: list | None = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "nav":
            self.current = []
            self.lists.append(self.current)
            if attrs.get("epub:type") == "toc" or attrs.get("role") == "doc-toc":
                self.toc = self.current
            self.depth = 0
        elif tag == "ol" and self.current is not None:
            self.depth += 1
        elif tag == "a" and self.current is not None and attrs.get("href"):
            self.link, self.label = attrs["href"], []

    def handle_endtag(self, tag):
        if tag == "nav":
            self.current = None
        elif tag == "ol" and self.current is not None:
            self.depth -= 1
        elif tag == "a" and self.link is not None and self.current is not None:
            self.current.append((max(0, self.depth - 1), "".join(self.label), self.link))
            self.link = None

    def handle_data(self, data):
        if self.link is not None:
            self.label.append(data)

    def entries(self) -> list:
        return self.toc if self.toc is not None else (self.lists[0] if self.lists else [])


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _ncx_entries(data: bytes) -> list:
    out: list = []

    def walk(element, depth):
        for child in element:
            if _local(child.tag) != "navPoint":
                continue
            title, src = "", ""
            for part in child:
                if _local(part.tag) == "navLabel":
                    title = "".join(part.itertext())
                elif _local(part.tag) == "content":
                    src = part.get("src") or ""
            out.append((depth, title, src))
            walk(child, depth + 1)

    for element in parse_xml(data).iter():
        if _local(element.tag) == "navMap":
            walk(element, 0)
            break
    return out


def _decode_bytes(data: bytes) -> str:
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", "replace")
    if not data.startswith(b"\xef\xbb\xbf"):
        head = data[:1024]
        declared = _ENCODING.search(head) or _META_CHARSET.search(head)
        name = declared.group(1).decode("ascii", "replace").lower() if declared else ""
        if name and not name.startswith(("utf-16", "utf-32", "utf16", "utf32")):    # with no byte order mark, it's mislabelled
            try:
                return data.decode(name)
            except (LookupError, UnicodeDecodeError):
                pass
    return data.decode("utf-8-sig", "replace")


def _decode(data: bytes) -> str:
    return _decode_bytes(data).replace("\r\n", "\n").replace("\r", "\n")


class _Zip:
    """Names inside the zip, tolerating a contents file that spells one with different capitals."""

    def __init__(self, z: zipfile.ZipFile):
        self.z = z
        self.names = set(z.namelist())
        self.folded = {n.lower(): n for n in reversed(z.namelist())}

    def find(self, name: str) -> str | None:
        return name if name in self.names else self.folded.get(name.lower())

    def read(self, name: str, limit: int | None = None) -> bytes:
        if self.z.getinfo(name).file_size > (limit or MAX_DOCUMENT):
            raise EbookError(f"{name} is too large to open")
        return self.z.read(name)


def _resolve(base: str, href: str) -> tuple[str, str]:
    """A link written in file `base` as (path in the zip, anchor)."""
    target, _, fragment = href.partition("#")
    path = posixpath.normpath(posixpath.join(posixpath.dirname(base), urllib.parse.unquote(target))) if target else base
    return path, urllib.parse.unquote(fragment)


def _check_drm(z: _Zip, documents: list[str]) -> None:
    if any(i.flag_bits & 1 for i in z.z.infolist()):
        raise EbookError("this book is DRM-protected")
    name = z.find("META-INF/encryption.xml")
    if not name:
        return
    # Fonts are often listed here too, which is harmless: only the text being locked stops the book.
    locked = {posixpath.normpath(urllib.parse.unquote(e.get("URI") or ""))
              for e in parse_xml(z.read(name)).iter() if _local(e.tag) == "CipherReference"}
    if locked & set(documents):
        raise EbookError("this book is DRM-protected")


def _contents(z: _Zip, items: dict, spine_toc: str, parser: _Parser) -> list[Chapter]:
    sources: list[tuple[str, list]] = []
    for path, _, properties in items.values():
        if "nav" in properties.split() and z.find(path):
            nav = _NavParser()
            try:
                nav.feed(_decode(z.read(z.find(path))))
                sources.append((path, nav.entries()))
            except EbookError:
                pass
    ncx = items.get(spine_toc) or next((i for i in items.values() if i[1] == "application/x-dtbncx+xml"), None)
    if ncx and z.find(ncx[0]):
        try:
            sources.append((ncx[0], _ncx_entries(z.read(z.find(ncx[0]), MAX_PACKAGE))))
        except (ET.ParseError, EbookError, ValueError, LookupError):
            pass
    for base, entries in sources:
        chapters = []
        for depth, title, href in entries:
            path, fragment = _resolve(base, href)
            title = _SPACE.sub(" ", clean_text(title)).strip()
            if not title or path not in parser.file_words:
                continue
            word = parser.anchors.get((path, fragment), parser.file_words[path]) if fragment else parser.file_words[path]
            chapters.append(Chapter(title, word, depth))
        if len(chapters) >= 2:
            return sorted(chapters, key=lambda c: c.word)
    headings = [b for b in parser.blocks if b.kind == "h"]
    if headings:
        large = [b for b in headings if b.level <= 2]
        return [Chapter(b.text, b.word) for b in large or headings]
    bold = [Chapter(b.text, b.word) for b in parser.blocks if _is_bold_title(b)]
    return bold if len(bold) >= 2 else []


def _is_bold_title(block: Block) -> bool:
    """A short, wholly bold first line of a file. Bold lines further in are summaries, stat blocks and the like."""
    return (block.new_page and block.kind == "p" and len(block.starts) <= TITLE_WORDS and len(block.spans) == 1
            and block.spans[0][:2] == (0, len(block.text)) and bool(block.spans[0][2] & BOLD))


def _ornaments(documents: list[tuple[str, str]]) -> set[str]:
    uses: Counter = Counter()
    for path, text in documents:
        for tag in _IMAGE_TAG.findall(text):
            m = _IMAGE_SRC.search(tag)
            if m:
                uses[_resolve(path, m.group(1))[0]] += 1
    return {name for name, count in uses.items() if count >= ORNAMENT_USES}


class _Package(NamedTuple):
    z: _Zip
    items: dict             # id -> (path, media type, properties)
    spine_toc: str
    order: list[str]        # the files to read, in reading order, that really are in the zip


def _open_package(raw: zipfile.ZipFile) -> _Package:
    z = _Zip(raw)
    opf = package_path(raw)
    items: dict[str, tuple[str, str, str]] = {}
    spine: list[tuple[str, bool]] = []
    spine_toc = ""
    for e in parse_xml(z.read(z.find(opf) or opf, MAX_PACKAGE)).iter():
        name = _local(e.tag)
        if name == "item" and e.get("id") and e.get("href"):
            items[e.get("id")] = (_resolve(opf, e.get("href"))[0], e.get("media-type") or "", e.get("properties") or "")
        elif name == "spine":
            spine_toc = e.get("toc") or ""
        elif name == "itemref" and e.get("idref"):
            spine.append((e.get("idref"), (e.get("linear") or "yes").lower() != "no"))
    order = [items[i][0] for i, linear in spine if linear and i in items] or [items[i][0] for i, _ in spine if i in items]
    pictures = {path for path, media, _ in items.values() if media.startswith(("image/", "audio/", "video/", "font/"))}
    order = [p for p in order if z.find(p) and p not in pictures]
    _check_drm(z, [z.find(p) for p in order])
    if sum(raw.getinfo(z.find(p)).file_size for p in order) > MAX_BOOK:
        raise EbookError("this book is too large to open")
    return _Package(z, items, spine_toc, order)


def load(path: str) -> Book:
    """Raises EbookError with a sentence for the reader if the book can't be opened."""
    try:
        with zipfile.ZipFile(path) as raw:
            pkg = _open_package(raw)
            classes: dict[str, tuple[int, bool]] = {}
            for item_path, media, _ in pkg.items.values():
                if media == "text/css" and pkg.z.find(item_path):
                    parse_css(_decode(pkg.z.read(pkg.z.find(item_path))), classes)
            texts = [(p, _decode(pkg.z.read(pkg.z.find(p)))) for p in pkg.order]
            parser = _Parser(classes, _ornaments(texts))
            for spine_path, text in texts:
                parser.read(spine_path, text)
            if not any(b.kind != "break" and not _JUST_AN_IMAGE.fullmatch(b.text) for b in parser.blocks):
                raise EbookError("this book has no text to show (it may be all images)")
            return Book(parser.blocks, _contents(pkg.z, pkg.items, pkg.spine_toc, parser), parser.word)
    except EbookError:
        raise
    except Exception as e:      # a damaged file fails in many ways inside zipfile and xml; none may take the program down
        raise EbookError(f"can't read this book: {e}") from None


def count_words(path: str) -> int | None:
    """About how many words a book has, without laying it out, for a book that hasn't been opened. None if it can't say."""
    try:
        with zipfile.ZipFile(path) as raw:
            pkg = _open_package(raw)
            total = 0
            for p in pkg.order:
                text = _NOT_TEXT.sub(" ", _decode(pkg.z.read(pkg.z.find(p))))
                text = _INLINE_TAG.sub("", _BLOCK_TAG.sub(" ", text))
                total += len(_WORD.findall(clean_text(html.unescape(text))))
            return total
    except Exception:           # as in load: the book just goes without an estimate
        return None
