"""Calibre's library layout, reproduced exactly so existing and new books look the same.

    Author/Title truncated (id)/Title truncated - Author.epub
    Author/Title truncated (id)/cover.jpg
    Author/Title truncated (id)/metadata.opf

Measured on the real library: folder titles and authors are cut at 36 characters, file-name titles
and authors at 31. Only the first author names the folder.
"""

from __future__ import annotations

import os
import re
import unicodedata
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from xml.sax.saxutils import escape, quoteattr

from kutil.text import normalize

FOLDER_LIMIT = 36
FILE_LIMIT = 31
OPF_NS = {"dc": "http://purl.org/dc/elements/1.1/", "opf": "http://www.idpf.org/2007/opf"}
OPF = "{http://www.idpf.org/2007/opf}"
RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}

# Calibre transliterates to ASCII. Latin accents come from Unicode decomposition; this adds Cyrillic and
# common punctuation. Other scripts have no table in the standard library and become "_".
_CYRILLIC = dict(zip("абвдезиклмнопрстуфц", "abvdeziklmnoprstufc")) | {
    "г": "gh", "ж": "zh", "й": "i", "х": "kh", "ч": "ch", "ш": "sh", "щ": "shch", "ъ": "", "ы": "y", "ь": "", "э": "e",
    "ю": "iu", "я": "ia", "ё": "e"}
_PUNCT = {"’": "'", "‘": "'", "“": '"', "”": '"', "–": "-", "—": "-", "…": "...", " ": " "}


def transliterate(s: str) -> str:
    out = []
    for c in s:
        low = c.lower()
        if c in _PUNCT:
            out.append(_PUNCT[c])
        elif low in _CYRILLIC:
            t = _CYRILLIC[low]
            out.append(t.capitalize() if c.isupper() else t)
        else:
            d = unicodedata.normalize("NFKD", c).encode("ascii", "ignore").decode()
            out.append(d if d or ord(c) < 128 else "_")
    return "".join(out)


def ascii_filename(s: str) -> str:
    s = transliterate(s)
    s = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:-1] + "_" if s.endswith(".") else s


def _title_part(title: str, limit: int) -> str:
    return ascii_filename(title.lstrip())[:limit].rstrip() or "Unknown"


def author_folder(author: str) -> str:
    a = ascii_filename(author)[:FOLDER_LIMIT].rstrip(" .") or "Unknown"
    return a + "w" if a.upper() in RESERVED else a


def book_folder(title: str, book_id: int) -> str:
    return f"{_title_part(title, FOLDER_LIMIT)} ({book_id})"


def book_path(title: str, author: str, book_id: int) -> str:
    return os.path.join(author_folder(author), book_folder(title, book_id))


def file_stem(title: str, author: str) -> str:
    name = f"{_title_part(title, FILE_LIMIT)} - {ascii_filename(author)[:FILE_LIMIT].rstrip() or 'Unknown'}"
    return name.rstrip(".")


# --------------------------------------------------------------------------
# Author names
# --------------------------------------------------------------------------

def first_last(name: str) -> str:
    """'Ring, Seth' -> 'Seth Ring'. Names with no comma, or several, are left alone."""
    name = re.sub(r"\s+", " ", (name or "").strip())
    parts = [p.strip() for p in name.split(",")]
    if len(parts) == 2 and parts[0] and parts[1]:
        return f"{parts[1]} {parts[0]}"
    return name


def author_sort(name: str) -> str:
    """'William Gibson' -> 'Gibson, William'."""
    parts = name.split()
    return f"{parts[-1]}, {' '.join(parts[:-1])}" if len(parts) > 1 else name


def author_key(name: str) -> str:
    """Same person regardless of order or punctuation: 'Ring, Seth' == 'Seth Ring', 'Mathews, J.R_' == 'J.R. Mathews'."""
    name = re.sub(r"_+$", ".", name)
    return " ".join(sorted(normalize(transliterate(name).replace(".", " ")).split()))


# --------------------------------------------------------------------------
# metadata.opf
# --------------------------------------------------------------------------

@dataclass
class BookInfo:
    title: str = ""
    authors: list[str] = field(default_factory=list)
    series: str = ""
    series_index: float | None = None
    language: str = ""
    description: str = ""
    isbn: str = ""
    publisher: str = ""
    pubdate: str = ""
    subjects: list[str] = field(default_factory=list)
    uuid: str = ""
    timestamp: str = ""
    title_sort: str = ""


def write_opf(info: BookInfo, book_id: int, cover: str | None = "cover.jpg") -> str:
    """metadata.opf text in the same shape Calibre writes."""
    uid = info.uuid or str(uuid.uuid4())
    L = ["<?xml version='1.0' encoding='utf-8'?>",
         '<package xmlns="http://www.idpf.org/2007/opf" unique-identifier="uuid_id" version="2.0">',
         '    <metadata xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:opf="http://www.idpf.org/2007/opf">',
         f'        <dc:identifier opf:scheme="calibre" id="calibre_id">{book_id}</dc:identifier>',
         f'        <dc:identifier opf:scheme="uuid" id="uuid_id">{escape(uid)}</dc:identifier>',
         f"        <dc:title>{escape(info.title)}</dc:title>"]
    for a in info.authors or ["Unknown"]:
        L.append(f'        <dc:creator opf:file-as={quoteattr(author_sort(a))} opf:role="aut">{escape(a)}</dc:creator>')
    L.append('        <dc:contributor opf:file-as="kvctl" opf:role="bkp">kvctl (kali-utils)</dc:contributor>')
    if info.pubdate:
        L.append(f"        <dc:date>{escape(info.pubdate)}</dc:date>")
    if info.description:
        L.append(f"        <dc:description>{escape(info.description)}</dc:description>")
    if info.publisher:
        L.append(f"        <dc:publisher>{escape(info.publisher)}</dc:publisher>")
    if info.isbn:
        L.append(f'        <dc:identifier opf:scheme="ISBN">{escape(info.isbn)}</dc:identifier>')
    if info.language:
        L.append(f"        <dc:language>{escape(info.language)}</dc:language>")
    for sub in info.subjects:
        L.append(f"        <dc:subject>{escape(sub)}</dc:subject>")
    if info.series:
        L.append(f'        <meta name="calibre:series" content={quoteattr(info.series)}/>')
        L.append(f'        <meta name="calibre:series_index" content="{info.series_index if info.series_index is not None else 1:g}"/>')
    if info.timestamp:
        L.append(f'        <meta name="calibre:timestamp" content={quoteattr(info.timestamp)}/>')
    L.append(f'        <meta name="calibre:title_sort" content={quoteattr(info.title_sort or info.title)}/>')
    L.append("    </metadata>")
    if cover:
        L += ["    <guide>", f'        <reference type="cover" title="Cover" href={quoteattr(cover)}/>', "    </guide>"]
    L.append("</package>\n")
    return "\n".join(L)


def read_opf(path: str) -> tuple[BookInfo, int | None]:
    """Parse a metadata.opf file. Returns (info, calibre id)."""
    root = ET.parse(path).getroot()
    m = root.find("opf:metadata", OPF_NS)
    info, book_id = BookInfo(), None
    if m is None:
        return info, None
    info.title = (m.findtext("dc:title", "", OPF_NS) or "").strip()
    info.authors = [c.text.strip() for c in m.findall("dc:creator", OPF_NS) if c.text and c.text.strip()]
    info.description = (m.findtext("dc:description", "", OPF_NS) or "").strip()
    info.publisher = (m.findtext("dc:publisher", "", OPF_NS) or "").strip()
    info.language = (m.findtext("dc:language", "", OPF_NS) or "").strip()
    info.pubdate = (m.findtext("dc:date", "", OPF_NS) or "").strip()
    info.subjects = [s.text.strip() for s in m.findall("dc:subject", OPF_NS) if s.text]
    for ident in m.findall("dc:identifier", OPF_NS):
        scheme = (ident.get(f"{OPF}scheme") or "").lower()
        text = (ident.text or "").strip()
        if scheme == "calibre" and text.isdigit():
            book_id = int(text)
        elif scheme == "uuid":
            info.uuid = text
        elif scheme == "isbn":
            info.isbn = re.sub(r"(?i)^isbn:?\s*", "", text)
    for meta in m.findall("opf:meta", OPF_NS):
        name, content = meta.get("name"), meta.get("content") or ""
        if name == "calibre:series":
            info.series = content
        elif name == "calibre:series_index":
            try:
                info.series_index = float(content)
            except ValueError:
                pass
        elif name == "calibre:timestamp":
            info.timestamp = content
        elif name == "calibre:title_sort":
            info.title_sort = content
    return info, book_id
