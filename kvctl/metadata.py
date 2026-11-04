"""Read title/author/series and the cover from book files. Files come from the internet, so sizes are bounded
and nothing in them is executed; a damaged file just gives less metadata."""

from __future__ import annotations

import lzma
import os
import posixpath
import re
import struct
import xml.etree.ElementTree as ET
import zipfile
import zlib

from kutil.text import clean_text

from .calibre import BookInfo

BOOK_EXT = {".epub", ".kepub", ".mobi", ".azw", ".azw3", ".fb2", ".pdf", ".lit", ".djvu", ".txt.zip", ".fb2.zip"}
PRIMARY_EXT = BOOK_EXT - {".txt.zip"}
MAX_XML = 4 * 1024 * 1024
AUTHOR_ROLES = {"", "aut", "author", "creator", "cre"}      # Kavita's list of roles that mean "wrote it"
# How a damaged zip can fail while being read, beyond what zipfile wraps in BadZipFile.
ZIP_ERRORS = (zipfile.BadZipFile, zlib.error, lzma.LZMAError, EOFError, RuntimeError, NotImplementedError, UnicodeDecodeError)
MAX_COVER = 30 * 1024 * 1024


def book_ext(path: str) -> str:
    low = path.lower()
    for double in (".fb2.zip", ".txt.zip"):
        if low.endswith(double):
            return double
    return os.path.splitext(low)[1]


def is_book(path: str) -> bool:
    return book_ext(path) in BOOK_EXT


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _parse(data: bytes):
    # ElementTree (expat) refuses external entities, and we cap the size of what it sees
    return ET.fromstring(data[:MAX_XML] if len(data) <= MAX_XML else b"")


def _clean(text: str | None) -> str:
    return re.sub(r"\s+", " ", clean_text(text or "").strip())


def _year_ok(date: str) -> str:
    return "" if date.startswith("0101") else date


def read_epub(path: str) -> tuple[BookInfo, bytes | None, str]:
    info = BookInfo()
    with zipfile.ZipFile(path) as z:
        container = _parse(z.read("META-INF/container.xml"))
        rootfile = next(e.get("full-path") for e in container.iter() if _local(e.tag) == "rootfile")
        opf = _parse(z.read(rootfile))
        meta = next((e for e in opf if _local(e.tag) == "metadata"), None)
        if meta is None:
            return info, None, ""
        cover_id = ""
        # EPUB 3 gives a creator's role in a separate element pointing at its id
        roles = {(e.get("refines") or "").lstrip("#"): _clean(e.text) for e in meta.iter()
                 if _local(e.tag) == "meta" and e.get("property") == "role"}
        for e in meta.iter():
            tag, text = _local(e.tag), _clean(e.text)
            attrs = {k.rsplit("}", 1)[-1].lower(): v for k, v in e.attrib.items()}
            if tag == "title" and not info.title:
                info.title = text
            elif tag == "creator" and text and (roles.get(attrs.get("id", "")) or attrs.get("role", "aut")).lower() in AUTHOR_ROLES:
                info.authors.append(text)
            elif tag == "language" and not info.language:
                info.language = text
            elif tag == "description" and not info.description:
                info.description = re.sub(r"<[^>]+>", " ", e.text or "").strip()[:4000]
            elif tag == "publisher" and not info.publisher:
                info.publisher = text
            elif tag == "date" and not info.pubdate:
                info.pubdate = _year_ok(text)
            elif tag == "subject" and text:
                info.subjects.append(text)
            elif tag == "identifier" and "isbn" in (attrs.get("scheme", "") + text[:5]).lower() and not info.isbn:
                info.isbn = re.sub(r"(?i)^(urn:)?isbn:?\s*", "", text)
            elif tag == "meta":
                name, content = attrs.get("name"), attrs.get("content", "")
                if name == "calibre:series":
                    info.series = content
                elif name == "calibre:series_index":
                    try:
                        info.series_index = float(content)
                    except ValueError:
                        pass
                elif name == "cover":
                    cover_id = content
                elif attrs.get("property") == "belongs-to-collection" and not info.series:
                    info.series = text
                elif attrs.get("property") == "group-position" and info.series_index is None:
                    try:
                        info.series_index = float(text)
                    except ValueError:
                        pass
        manifest = next((e for e in opf if _local(e.tag) == "manifest"), None)
        href = ""
        if manifest is not None:
            items = [i for i in manifest if _local(i.tag) == "item"]
            for it in items:
                if "cover-image" in (it.get("properties") or "") or (cover_id and it.get("id") == cover_id):
                    href = it.get("href") or ""
                    break
            if not href:
                href = next((i.get("href") or "" for i in items if (i.get("media-type") or "").startswith("image/")
                             and "cover" in ((i.get("id") or "") + (i.get("href") or "")).lower()), "")
        if href:
            name = posixpath.normpath(posixpath.join(posixpath.dirname(rootfile), href))
            try:
                if z.getinfo(name).file_size <= MAX_COVER:
                    data = z.read(name)
                    return info, data, image_ext(data)
            except KeyError:
                pass
    return info, None, ""


def read_fb2(path: str) -> tuple[BookInfo, bytes | None, str]:
    info = BookInfo()
    with open(path, "rb") as f:
        head = f.read(MAX_XML)
    title = re.search(rb"<book-title>(.*?)</book-title>", head, re.S)
    if title:
        info.title = _clean(title.group(1).decode("utf-8", "replace"))
    block = re.search(rb"<title-info>(.*?)</title-info>", head, re.S)
    if block:
        for m in re.finditer(rb"<author>(.*?)</author>", block.group(1), re.S):
            parts = [_clean(x.decode("utf-8", "replace")) for x in
                     re.findall(rb"<(?:first-name|middle-name|last-name)>(.*?)<", m.group(1))]
            if any(parts):
                info.authors.append(" ".join(p for p in parts if p))
        seq = re.search(rb'<sequence\s+name="([^"]*)"(?:\s+number="([\d.]+)")?', block.group(1))
        if seq:
            info.series = seq.group(1).decode("utf-8", "replace")
            info.series_index = float(seq.group(2)) if seq.group(2) else None
        lang = re.search(rb"<lang>(.*?)</lang>", block.group(1))
        if lang:
            info.language = lang.group(1).decode("utf-8", "replace").strip()
    return info, None, ""


def read_mobi(path: str) -> tuple[BookInfo, bytes | None, str]:
    """MOBI/AZW/AZW3: full title and the EXTH record table in the first PalmDB record."""
    info = BookInfo()
    with open(path, "rb") as f:
        head = f.read(2 * 1024 * 1024)
    if head[60:68] not in (b"BOOKMOBI", b"TEXtREAd"):
        return info, None, ""
    first = struct.unpack(">I", head[78:82])[0]
    rec = head[first:]
    if rec[16:20] != b"MOBI":
        return info, None, ""
    mobi_len, _, _, encoding = struct.unpack(">IIII", rec[20:36])
    codec = "utf-8" if encoding == 65001 else "cp1252"
    name_off, name_len = struct.unpack(">II", rec[84:92])
    info.title = _clean(rec[name_off:name_off + name_len].decode(codec, "replace"))
    if struct.unpack(">I", rec[128:132])[0] & 0x40:
        pos = 16 + mobi_len
        if rec[pos:pos + 4] == b"EXTH":
            count = struct.unpack(">I", rec[pos + 8:pos + 12])[0]
            p = pos + 12
            for _ in range(min(count, 500)):
                kind, size = struct.unpack(">II", rec[p:p + 8])
                if size < 8:
                    break
                value = rec[p + 8:p + size].decode(codec, "replace").strip()
                if kind == 100 and value:
                    info.authors.append(value)
                elif kind == 101:
                    info.publisher = value
                elif kind == 103:
                    info.description = re.sub(r"<[^>]+>", " ", value)[:4000]
                elif kind == 104:
                    info.isbn = value
                elif kind == 105 and value:
                    info.subjects.append(value)
                elif kind == 106:
                    info.pubdate = value
                elif kind == 503 and value:
                    info.title = value
                p += size
    return info, None, ""


def from_filename(path: str) -> BookInfo:
    """'Title - Author.epub' (Calibre's own pattern), else the whole name as the title."""
    stem = clean_text(os.path.basename(path))
    stem = stem[:-len(book_ext(path))] if book_ext(path) else stem
    stem = re.sub(r"\s*[\[(](?:epub|mobi|pdf|azw3|retail|z-lib[^\])]*)[\])]\s*$", "", stem, flags=re.I)
    stem = re.sub(r"[._]+", lambda m: " " if " " not in stem else m.group(0), stem).strip()
    info = BookInfo()
    if " - " in stem:
        title, author = stem.rsplit(" - ", 1)
        info.title, info.authors = title.strip(), [author.strip()]
    else:
        info.title = stem
    return info


def image_ext(data: bytes) -> str:
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    return ""


def read_book(path: str) -> tuple[BookInfo, bytes | None, str, str]:
    """(info, cover bytes, cover extension, where the data came from). Never raises on a damaged file."""
    ext = book_ext(path)
    reader = {".epub": read_epub, ".kepub": read_epub, ".fb2": read_fb2, ".mobi": read_mobi, ".azw": read_mobi,
              ".azw3": read_mobi}.get(ext)
    info, cover, cover_ext = BookInfo(), None, ""
    source = "file name"
    if reader:
        try:
            info, cover, cover_ext = reader(path)
            if info.title:
                source = "book metadata"
        except (OSError, *ZIP_ERRORS, ET.ParseError, KeyError, StopIteration, struct.error, ValueError, IndexError):
            source = "file name (book metadata unreadable)"
    fallback = from_filename(path)
    if not info.title or info.title.lower() in ("unknown", "untitled"):
        info.title = fallback.title
    if not info.authors or info.authors[0].lower() == "unknown":
        info.authors = fallback.authors
    return info, cover, cover_ext, source
